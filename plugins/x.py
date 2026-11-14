"""X (Twitter) plugin. X has no free official API, so this is BEST EFFORT.

Method 1 - Nitter RSS: used if settings.nitter_instances is set.
Method 2 - Twikit: used if secrets X_AUTH_TOKEN and X_CT0 are set (throwaway
           X account recommended). Cloud/datacenter IPs may get blocked by X.
"""
import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("watcher")
NAME = "x"
EMOJI = "🐦"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def key(src):
    u = src.get("username")
    if not u:
        raise ValueError("x source needs 'username'")
    return u.lstrip("@").lower()


def _sort_newest_first(items):
    return sorted(items, key=lambda i: int(i["id"]), reverse=True)


def parse_nitter(xml_bytes, user, include_replies, include_reposts):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or it.findtext("guid") or ""
        m = re.search(r"/status/(\d+)", link)
        if not m:
            continue
        if title.startswith("RT by") and not include_reposts:
            continue
        if title.startswith("R to") and not include_replies:
            continue
        items.append({"id": m.group(1), "title": title[:400],
                      "url": f"https://x.com/{user}/status/{m.group(1)}"})
    return _sort_newest_first(items)


def _via_nitter(user, instances, include_replies, include_reposts):
    last = "no instances"
    for base in instances:
        try:
            r = requests.get(f"{base.rstrip('/')}/{user}/rss", headers=UA, timeout=20)
            if r.status_code != 200 or b"<rss" not in r.content[:800]:
                last = f"{base} -> HTTP {r.status_code}"
                continue
            return parse_nitter(r.content, user, include_replies, include_reposts)
        except Exception as e:
            last = f"{base} -> {type(e).__name__}"
    raise RuntimeError(f"all Nitter instances failed ({last})")


# ---- Twikit 2.3.3 patch -------------------------------------------------
# x.com changed how its home page lists the "ondemand.s" script, so the
# published Twikit fails with "Couldn't get KEY_BYTE indices". This replaces
# that one method with a tolerant version (old and new page layouts).
_OLD_HASH_RE = re.compile(r"""['"]ondemand\.s['"]\s*:\s*['"](\w*)['"]""")
_CHUNK_NAME_RE = re.compile(r"""['"]?(\d+)['"]?\s*:\s*['"]ondemand\.s['"]""")
_INDICES_RE = re.compile(r"\w\[(\d{1,2})\],\s*16")


def _find_hash(page):
    m = _OLD_HASH_RE.search(page)
    if m and m.group(1):
        return m.group(1)
    c = _CHUNK_NAME_RE.search(page)
    if c:
        cid = re.escape(c.group(1))
        for h in re.finditer(rf"""['"]?{cid}['"]?\s*:\s*['"]([0-9A-Za-z]{{5,}})['"]""", page):
            if h.group(1) != "ondemand":
                return h.group(1)
    return None


def _patch_twikit():
    from twikit.x_client_transaction.transaction import ClientTransaction

    if getattr(ClientTransaction, "_patched_by_watcher", False):
        return

    async def get_indices(self, home_page_response, session, headers):
        response = self.validate_response(home_page_response) or self.home_page_response
        page = str(response)
        file_hash = _find_hash(page)
        if not file_hash:
            i = page.find("ondemand.s")
            snippet = page[max(0, i - 40): i + 80].replace("\n", " ") if i >= 0 else "none"
            raise RuntimeError(f"ondemand.s hash not found (page {len(page)} chars; "
                               f"mentions={page.count('ondemand')}; near: {snippet})")
        url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{file_hash}a.js"
        r = await session.request(method="GET", url=url, headers=headers)
        found = [int(m.group(1)) for m in _INDICES_RE.finditer(str(r.text))]
        if len(found) < 2:
            raise RuntimeError(f"no key indices in ondemand file (hash {file_hash}, "
                               f"{len(str(r.text))} chars, HTTP {getattr(r, 'status_code', '?')})")
        return found[0], found[1:]

    ClientTransaction.get_indices = get_indices
    ClientTransaction._patched_by_watcher = True

    # X rotates the ct0 cookie when it rejects a request. Twikit kept the OLD
    # one; keep the NEWEST instead so a retry can succeed.
    from twikit.client.client import Client as _Client

    def keep_latest_ct0(self):
        cookies = {}
        for cookie in self.http.cookies.jar:
            cookies[cookie.name] = cookie.value      # later entries win
        self.http.cookies = list(cookies.items())

    _Client._remove_duplicate_ct0_cookie = keep_latest_ct0


async def _retry_csrf(fn, *args, **kwargs):
    """X answers a stale ct0 with 403/353 and a fresh ct0; retry once."""
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        if "353" in str(e) or "csrf" in str(e).lower():
            return await fn(*args, **kwargs)
        raise


def _clean(value):
    return (value or "").strip().strip("'\"").strip()


async def _via_twikit(user, entry, include_replies, include_reposts):
    from twikit import Client  # imported lazily so YouTube works without it
    _patch_twikit()
    auth, ct0 = _clean(os.environ["X_AUTH_TOKEN"]), _clean(os.environ["X_CT0"])
    log.info("X cookies: auth_token length=%d, ct0 length=%d", len(auth), len(ct0))
    client = Client("en-US")
    client.set_cookies({"auth_token": auth, "ct0": ct0})
    meta = entry.setdefault("meta", {})
    uid = meta.get("user_id")
    if not uid:
        uid = (await _retry_csrf(client.get_user_by_screen_name, user)).id
        meta["user_id"] = uid
    tweets = await _retry_csrf(client.get_user_tweets, uid, "Tweets", count=20)
    items = []
    for t in tweets:
        if getattr(t, "retweeted_tweet", None) and not include_reposts:
            continue
        if getattr(t, "in_reply_to", None) and not include_replies:
            continue
        text = getattr(t, "full_text", None) or getattr(t, "text", "") or "New post"
        items.append({"id": str(t.id), "title": text[:400],
                      "url": f"https://x.com/{user}/status/{t.id}"})
    return _sort_newest_first(items)


def _safe(msg, limit=300):
    """Shorten an error message and hide secret values before logging it."""
    for name in ("X_AUTH_TOKEN", "X_CT0"):
        val = os.environ.get(name)
        if val:
            msg = msg.replace(val, "***")
    return msg[:limit]


def fetch(src, entry, settings):
    user = key(src)
    inc_r = bool(src.get("include_replies", False))
    inc_rt = bool(src.get("include_reposts", False))
    errors = []
    instances = settings.get("nitter_instances") or []
    if instances:
        try:
            return _via_nitter(user, instances, inc_r, inc_rt)
        except Exception as e:
            errors.append(str(e))
    if os.environ.get("X_AUTH_TOKEN") and os.environ.get("X_CT0"):
        try:
            return asyncio.run(_via_twikit(user, entry, inc_r, inc_rt))
        except Exception as e:
            errors.append(f"twikit -> {type(e).__name__}: {_safe(str(e))}")
    if not errors:
        errors.append("no X method configured (set nitter_instances or X_AUTH_TOKEN/X_CT0)")
    raise RuntimeError("; ".join(errors))
