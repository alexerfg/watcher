#!/usr/bin/env python3
"""Central social watcher: checks your sources, sends NEW items to Telegram.

Anyone who sends /start to the bot becomes a subscriber (and /stop leaves).
You (TELEGRAM_CHAT_ID) always receive everything, plus failure warnings.

Usage:
  python watcher.py            normal run (used by GitHub Actions)
  python watcher.py --test     send a test message to you
  python watcher.py --dry-run  print what each source returns; send/save nothing
"""
import argparse
import base64
import hashlib
import html
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
import yaml

from plugins import load_plugins

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
MAX_SEEN = 300        # remembered ids per source
MAX_PER_RUN = 5       # max alerts per source per run (avoids floods)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("watcher")


# ---------- config / state ----------
def load_config():
    raw = os.environ.get("WATCH_CONFIG", "").strip()  # optional private override
    text = raw or (ROOT / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("state.json is corrupt; starting fresh")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# ---------- subscribers (stored encrypted: the repo may be public) ----------
def _fernet(token):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()))


def load_subscribers(state, token):
    enc = state.get("subscribers_enc")
    if not enc:
        return []
    try:
        return [str(c) for c in json.loads(_fernet(token).decrypt(enc.encode()))]
    except Exception:
        log.error("Could not decrypt subscriber list (bot token changed?)")
        return []


def save_subscribers(state, token, subs):
    data = json.dumps(sorted(set(subs))).encode()
    state["subscribers_enc"] = _fernet(token).encrypt(data).decode()


# ---------- Telegram ----------
class Telegram:
    def __init__(self, token, owner_chat_id):
        self.token = token
        self.owner = str(owner_chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, chat_id, text):
        """Returns 'ok', 'gone' (user blocked/deleted the bot) or 'fail'."""
        for _ in range(3):
            try:
                r = requests.post(f"{self.base}/sendMessage", timeout=20, json={
                    "chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            except requests.RequestException as e:
                log.warning("Telegram network error: %s", type(e).__name__)
                time.sleep(3)
                continue
            if r.status_code == 200:
                return "ok"
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(wait, 30) + 1)
                continue
            if r.status_code in (400, 403) and any(
                    w in r.text.lower() for w in ("blocked", "chat not found", "deactivated", "kicked")):
                return "gone"
            log.error("Telegram rejected message: HTTP %s %s", r.status_code, r.text[:200])
            return "fail"
        return "fail"

    def get_updates(self, offset):
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(f"{self.base}/getUpdates", params=params, timeout=20)
            if r.status_code == 200:
                return r.json().get("result", [])
            log.warning("getUpdates HTTP %s", r.status_code)
        except requests.RequestException as e:
            log.warning("getUpdates network error: %s", type(e).__name__)
        return []


class Notifier:
    """Sends alerts to you + all subscribers; admin messages go to you only."""

    def __init__(self, tg, subs):
        self.tg, self.subs, self.changed = tg, list(subs), False

    def post(self, text):
        owner_ok = self.tg.send(self.tg.owner, text) == "ok"
        for cid in list(self.subs):
            if cid == self.tg.owner:
                continue
            if self.tg.send(cid, text) == "gone":
                self.subs.remove(cid)
                self.changed = True
                log.info("Removed a subscriber who blocked the bot")
            time.sleep(0.05)
        return owner_ok

    def admin(self, text):
        return self.tg.send(self.tg.owner, text) == "ok"


def sync_subscribers(notifier, state, settings):
    """Read /start and /stop messages sent to the bot since the last run."""
    tg = notifier.tg
    updates = tg.get_updates(state.get("tg_offset"))
    max_subs = int(settings.get("max_subscribers", 50))
    for u in updates:
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        words = (msg.get("text") or "").strip().lower().split()
        if not chat_id or not words:
            continue
        cmd = words[0].split("@")[0]
        if cmd == "/start":
            if chat_id == tg.owner:
                tg.send(chat_id, "👋 You're the admin: you always get all alerts and failure warnings.")
            elif chat_id in notifier.subs:
                tg.send(chat_id, "✅ You're already subscribed. Send /stop to unsubscribe.")
            elif len(notifier.subs) >= max_subs:
                tg.send(chat_id, "Sorry, the subscriber list is full.")
            else:
                notifier.subs.append(chat_id)
                notifier.changed = True
                tg.send(chat_id, "✅ Subscribed. You'll get a message when a new video or post "
                                 "appears. Send /stop to unsubscribe.")
        elif cmd == "/stop":
            if chat_id in notifier.subs:
                notifier.subs.remove(chat_id)
                notifier.changed = True
                tg.send(chat_id, "🛑 Unsubscribed. Send /start to subscribe again.")
    if updates:
        state["tg_offset"] = max(u["update_id"] for u in updates) + 1


def esc(s):
    return html.escape(s or "", quote=False)


def format_message(plugin, src, item):
    title = esc(item.get("title", ""))
    return f'{plugin.EMOJI} <b>{esc(src.get("name", src["platform"]))}</b>\n{title}\n{item["url"]}'


# ---------- main logic ----------
def process_source(src, plugin, entry, settings, notifier, dry_run):
    items = plugin.fetch(src, entry, settings)           # newest first
    ids = [i["id"] for i in items]

    if "seen" not in entry:                                # first time: baseline only
        entry["seen"] = ids
        log.info("%s: baselined %d existing items (no alerts)", src.get("name"), len(ids))
        return

    seen = set(entry["seen"])
    new = [i for i in reversed(items) if i["id"] not in seen]   # oldest first
    if len(new) > MAX_PER_RUN:
        skipped = new[:-MAX_PER_RUN]
        entry["seen"] += [i["id"] for i in skipped]
        new = new[-MAX_PER_RUN:]
    for item in new:
        if dry_run:
            log.info("[dry-run] would send: %s", item["url"])
            continue
        if not notifier.post(format_message(plugin, src, item)):
            log.error("send failed; will retry next run")
            break                                          # keep unsent items unseen
        entry["seen"].append(item["id"])
    entry["seen"] = entry["seen"][-MAX_SEEN:]
    log.info("%s: %d new", src.get("name"), len(new))


def run(args):
    cfg = load_config()
    settings = cfg.get("settings") or {}
    warn_after = int(settings.get("warn_after_failures", 12))
    state = load_state()
    state.setdefault("sources", {})

    notifier, token = None, None
    if not args.dry_run:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        notifier = Notifier(Telegram(token, chat), load_subscribers(state, token))

    sources = cfg.get("sources") or []
    if args.test:
        ok = notifier.admin(f"✅ Watcher is working. Watching {len(sources)} source(s), "
                            f"{len(notifier.subs)} subscriber(s).")
        sys.exit(0 if ok else 1)

    if notifier:
        sync_subscribers(notifier, state, settings)

    plugins = load_plugins()
    for src in sources:
        platform = src.get("platform")
        plugin = plugins.get(platform)
        if not plugin:
            log.error("Unknown platform %r (available: %s)", platform, ", ".join(plugins))
            continue
        try:
            skey = f"{platform}:{plugin.key(src)}"
        except Exception as e:
            log.error("Bad source config %s: %s", src, e)
            continue
        entry = state["sources"].setdefault(skey, {})
        try:
            process_source(src, plugin, entry, settings, notifier, args.dry_run)
            entry["fails"], entry["warned"] = 0, False
        except Exception as e:
            entry["fails"] = entry.get("fails", 0) + 1
            log.error("%s failed (%d in a row): %s", skey, entry["fails"], e)
            if notifier and entry["fails"] >= warn_after and not entry.get("warned"):
                if notifier.admin(f"⚠️ Watcher can't read <b>{esc(src.get('name', skey))}</b> "
                                  f"({platform}) for {entry['fails']} runs in a row.\n"
                                  f"<code>{esc(str(e)[:200])}</code>"):
                    entry["warned"] = True

    if not args.dry_run:
        if notifier.changed:                               # only rewrite when the list changed
            save_subscribers(state, token, notifier.subs)
        state["heartbeat"] = date.today().isoformat()      # keeps the repo "active"
        save_state(state)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    run(p.parse_args())
