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


def fetch(src, entry, settings):
    meta = entry.setdefault("meta", {})
    cid = src.get("channel_id") or meta.get("channel_id")
    if not cid:
        cid = _resolve_handle(src["handle"])
        meta["channel_id"] = cid
    r = requests.get("https://www.youtube.com/feeds/videos.xml",
                     params={"channel_id": cid}, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_feed(r.content)
