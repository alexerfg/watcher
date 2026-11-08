#!/usr/bin/env python3
"""Central social watcher: checks your sources, sends NEW items to Telegram.

Usage:
  python watcher.py            normal run (used by GitHub Actions)
  python watcher.py --test     send a test message to Telegram
  python watcher.py --dry-run  print what each source returns; send/save nothing
"""
import argparse
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


# ---------- Telegram ----------
class Telegram:
    def __init__(self, token, chat_id):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text):
        for _ in range(3):
            try:
                r = requests.post(self.url, timeout=20, json={
                    "chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})
            except requests.RequestException as e:
                log.warning("Telegram network error: %s", type(e).__name__)
                time.sleep(3)
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(wait, 30) + 1)
                continue
            log.error("Telegram rejected message: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return False


def esc(s):
    return html.escape(s or "", quote=False)


def format_message(plugin, src, item):
    title = esc(item.get("title", ""))
    return f'{plugin.EMOJI} <b>{esc(src.get("name", src["platform"]))}</b>\n{title}\n{item["url"]}'


# ---------- main logic ----------
def process_source(src, plugin, entry, settings, tg, dry_run):
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
        if not tg.send(format_message(plugin, src, item)):
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

    tg = None
    if not args.dry_run:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        tg = Telegram(token, chat)

    sources = cfg.get("sources") or []
    if args.test:
        ok = tg.send(f"✅ Watcher is working. Watching {len(sources)} source(s).")
        sys.exit(0 if ok else 1)

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
            process_source(src, plugin, entry, settings, tg, args.dry_run)
            entry["fails"], entry["warned"] = 0, False
        except Exception as e:
            entry["fails"] = entry.get("fails", 0) + 1
            log.error("%s failed (%d in a row): %s", skey, entry["fails"], e)
            if tg and entry["fails"] >= warn_after and not entry.get("warned"):
                if tg.send(f"⚠️ Watcher can't read <b>{esc(src.get('name', skey))}</b> "
                           f"({platform}) for {entry['fails']} runs in a row.\n"
                           f"<code>{esc(str(e)[:200])}</code>"):
                    entry["warned"] = True

    if not args.dry_run:
        state["heartbeat"] = date.today().isoformat()      # keeps the repo "active"
        save_state(state)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    run(p.parse_args())
