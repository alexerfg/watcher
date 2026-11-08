"""YouTube plugin: uses each channel's free public RSS feed (no API key)."""
import re
import xml.etree.ElementTree as ET

import requests

NAME = "youtube"
EMOJI = "🎬"

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
NS = {"a": "http://www.w3.org/2005/Atom",
      "yt": "http://www.youtube.com/xml/schemas/2015"}


def key(src):
    k = src.get("channel_id") or src.get("handle")
    if not k:
        raise ValueError("youtube source needs 'handle' or 'channel_id'")
    return k.lower()


def _resolve_handle(handle):
    handle = handle if handle.startswith("@") else "@" + handle
    r = requests.get(f"https://www.youtube.com/{handle}", headers=UA, timeout=20,
                     cookies={"CONSENT": "YES+1", "SOCS": "CAI"})
    r.raise_for_status()
    for pat in (r"channel_id=(UC[\w-]{22})", r'"channelId":"(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"'):
        m = re.search(pat, r.text)
        if m:
            return m.group(1)
    raise RuntimeError(f"could not resolve channel id for {handle}; "
                       "put 'channel_id' in config.yaml instead")


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        if not vid:
            continue
        items.append({"id": vid,
                      "title": e.findtext("a:title", namespaces=NS) or "New video",
                      "url": f"https://www.youtube.com/watch?v={vid}"})
    return items  # newest first


def _scrape_channel_page(cid):
    """Last-resort backup: read video ids from the channel's Videos page."""
    r = requests.get(f"https://www.youtube.com/channel/{cid}/videos", headers=UA,
                     cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=20)
    r.raise_for_status()
    ids = []
    for m in re.finditer(r'"videoId":"([\w-]{11})"', r.text):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        raise RuntimeError("no videos found on channel page")
    # keep only the newest ~15 so ids match what the RSS feed would list
    return [{"id": v, "title": "New video",
             "url": f"https://www.youtube.com/watch?v={v}"} for v in ids[:15]]


def fetch(src, entry, settings):
    meta = entry.setdefault("meta", {})
    cid = src.get("channel_id") or meta.get("channel_id")
    if not cid:
        cid = _resolve_handle(src["handle"])
        meta["channel_id"] = cid
    feed = "https://www.youtube.com/feeds/videos.xml"
    tail = cid[2:]
    errors = []
    # YouTube's feed server sometimes returns 404 for valid channels, so try backups.
    for params in ({"channel_id": cid},
                   {"playlist_id": "UULF" + tail},
                   {"playlist_id": "UU" + tail}):
        try:
            r = requests.get(feed, params=params, headers=UA, timeout=20)
            r.raise_for_status()
            return parse_feed(r.content)
        except Exception as e:
            errors.append(f"{list(params)[0]}: {type(e).__name__}")
    try:
        return _scrape_channel_page(cid)
    except Exception as e:
        errors.append(f"channel page: {type(e).__name__}")
    raise RuntimeError("all YouTube methods failed (" + "; ".join(errors) + ")")
