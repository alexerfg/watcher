"""X (Twitter) plugin. X has no free official API, so this is BEST EFFORT.

Method 1 - Nitter RSS: used if settings.nitter_instances is set.
Method 2 - Twikit: used if secrets X_AUTH_TOKEN and X_CT0 are set (throwaway
           X account recommended). Cloud/datacenter IPs may get blocked by X.
"""
import asyncio
import os
import re
import xml.etree.ElementTree as ET

import requests

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


async def _via_twikit(user, entry, include_replies, include_reposts):
    from twikit import Client  # imported lazily so YouTube works without it
    client = Client("en-US")
    client.set_cookies({"auth_token": os.environ["X_AUTH_TOKEN"],
                        "ct0": os.environ["X_CT0"]})
    meta = entry.setdefault("meta", {})
    uid = meta.get("user_id")
    if not uid:
        uid = (await client.get_user_by_screen_name(user)).id
        meta["user_id"] = uid
    tweets = await client.get_user_tweets(uid, "Tweets", count=20)
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
            errors.append(f"twikit -> {type(e).__name__}")
    if not errors:
        errors.append("no X method configured (set nitter_instances or X_AUTH_TOKEN/X_CT0)")
    raise RuntimeError("; ".join(errors))
