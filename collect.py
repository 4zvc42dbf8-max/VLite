#!/usr/bin/env python3
"""VLite collector: reads the public YouTube RSS feed of each channel in channels.json
and writes videos.json (+ state.json, its memory). Runs on GitHub Actions: no key needed.
A channel can be forced with "id": "UC..." in channels.json."""
import json, os, re, sys, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
COOKIE = "CONSENT=YES+1; SOCS=CAI"
NOW = datetime.now(timezone.utc)
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
ID_RXS = (r'<meta itemprop="(?:identifier|channelId)" content="(UC[0-9A-Za-z_-]{22})"',
          r'"externalId":"(UC[0-9A-Za-z_-]{22})"',
          r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
          r'"browseId":"(UC[0-9A-Za-z_-]{22})"',
          r'channel_id=(UC[0-9A-Za-z_-]{22})')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def status_noredirect(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE})
    try:
        return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code


def load(name, default):
    try:
        return json.load(open(os.path.join(HERE, name), encoding="utf-8"))
    except Exception:
        return default


def resolve_id(ch):
    """Channel id from its @handle. Never guesses: a wrong channel is worse than none."""
    if re.fullmatch(r"UC[0-9A-Za-z_-]{22}", ch.get("handle", "")):
        return ch["handle"]
    handle = (ch.get("handle") or ch["name"]).lstrip("@")
    last = None
    for url in ("https://www.youtube.com/@%s" % urllib.parse.quote(handle),
                "https://www.youtube.com/@%s/about" % urllib.parse.quote(handle),
                "https://m.youtube.com/@%s" % urllib.parse.quote(handle)):
        try:
            html = get(url)
            for rx in ID_RXS:
                m = re.search(rx, html)
                if m:
                    return m.group(1)
            last = "page lue mais sans identifiant"
        except Exception as e:
            last = str(e)[:80]
    raise RuntimeError("@%s introuvable (%s)" % (handle, last))


def parse_feed(xml):
    root = ET.fromstring(xml)
    title = (root.findtext("a:title", "", NS) or "").strip()
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        pub = e.findtext("a:published", "", NS)
        if not vid or not pub:
            continue
        out.append({"id": vid, "title": (e.findtext("a:title", "", NS) or "").strip(),
                    "published": datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()})
    return title, out


def is_short(vid, title, state):
    if re.search(r"#shorts?\b", title.lower()):
        return True
    if vid in state["short"]:
        return state["short"][vid]
    code = status_noredirect("https://www.youtube.com/shorts/" + vid)
    if code in (200, 303, 302, 301):   # 200 = it really is a Short, a redirect = normal video
        state["short"][vid] = (code == 200)
        return code == 200
    return False                       # unknown: keep the video


def process(args):
    ch, state = args
    info = {"name": ch["name"], "handle": ch.get("handle") or ch["name"], "ok": False}
    try:
        cid = ch.get("id") or state["ids"].get(ch["name"] + "|" + info["handle"].lower())
        if not cid:
            cid = resolve_id(ch)
        state["ids"][ch["name"] + "|" + info["handle"].lower()] = cid
        info["id"] = cid
        title, entries = None, None
        for attempt in range(3):
            try:
                title, entries = parse_feed(get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
                break
            except Exception as e:
                err = e
        if entries is None:
            raise RuntimeError("flux illisible (%s)" % str(err)[:60])
        info.update(ok=True, found=title)
        entries.sort(key=lambda v: v["published"], reverse=True)
        vids = [v for v in entries if not is_short(v["id"], v["title"], state)]
        for v in vids:
            v["channel"] = ch["name"]
        info["count"] = len(vids)
        return info, vids
    except Exception as e:
        info["error"] = str(e)[:120]
        print("  FAILED", ch["name"], e, file=sys.stderr)
        return info, None


def main():
    cfg = load("channels.json", {"channels": []})
    state = load("state.json", {})
    for k in ("ids", "short"):
        state.setdefault(k, {})
    prev = load("videos.json", {"videos": []})
    days = min(30, max(1, cfg.get("max_age_days", 30)))
    keep_from = (NOW - timedelta(days=days)).isoformat()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(process, [(c, state) for c in cfg["channels"]]))
    by = {}
    for v in prev.get("videos", []):
        by.setdefault(v.get("channel"), {})[v["id"]] = v
    chans, videos = [], []
    for ch, (info, vids) in zip(cfg["channels"], results):
        chans.append(info)
        old = by.get(ch["name"], {})
        for v in vids or []:
            old[v["id"]] = {**old.get(v["id"], {}), **v}      # merge: videos stay even when they leave the 15-item feed
        items = sorted(old.values(), key=lambda v: v["published"], reverse=True)
        recent = [v for v in items if v["published"] >= keep_from]
        if items and not recent:
            recent = items[:1]                                  # the latest video of a channel is always kept
        if ch.get("rules"):
            info["rules"] = ch["rules"]
            picked = []
            for r in ch["rules"]:
                for v in items:
                    t = v["title"].lower()
                    if re.search(r["match"], t) and not (r.get("exclude") and re.search(r["exclude"], t)):
                        picked.append({**v, "tag": r["label"]})
                        break
            recent = picked
        videos.extend(recent)
    ok = sum(1 for c in chans if c["ok"])
    print("channels ok: %d/%d  videos: %d" % (ok, len(chans), len(videos)))
    for c in chans:
        print("  ", "OK " if c["ok"] else "ERR", c["name"], "->", c.get("found") or c.get("error"), "(%s videos)" % c.get("count", 0))
    if ok == 0 and prev.get("videos"):
        print("Nothing reachable: keeping the previous videos.json")
        return
    videos.sort(key=lambda v: v["published"], reverse=True)
    keep = {v["id"] for v in videos}
    state["short"] = {k: s for k, s in state["short"].items() if k in keep or len(state["short"]) < 800}
    json.dump({"updated": NOW.isoformat(), "days": days, "channels": chans, "videos": videos},
              open(os.path.join(HERE, "videos.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(state, open(os.path.join(HERE, "state.json"), "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
