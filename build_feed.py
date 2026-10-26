#!/usr/bin/env python3
"""Merge many RSS/Atom feeds into one filtered, de-duplicated feed set.

Reads sources.yml and keywords.yml, fetches every enabled source, labels each
item (Drop / Collab / Sale / Outlet / Review), drops noise, and writes into docs/:

  feed.xml            all kept items
  feed-drop.xml       only items labelled Drop
  feed-collab.xml     ... Collab
  feed-sale.xml       ... Sale
  feed-outlet.xml     ... Outlet
  feed-review.xml     ... Review
  items.json          the same items as JSON (for automations and the dashboard)
  health.json         per-source status of the last run

Items from previous runs are kept (until they are older than max_age_days),
so the feed is a rolling window, not just the latest fetch.
"""
import hashlib
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import feedparser
import requests
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("CONFIG_DIR", ROOT)
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(ROOT, "docs"))
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
UA = "Mozilla/5.0 (compatible; FootwearFeedBuilder/1.0)"
TIMEOUT = 25

# When an item has several labels, the first of these is shown in the title.
PRIMARY_ORDER = ["Collab", "Drop", "Outlet", "Sale", "Review", "Other"]
SIGNAL_FEEDS = ["Drop", "Collab", "Sale", "Outlet", "Review"]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
TRACKING = ("utm_", "fbclid", "gclid", "mc_", "cmpid")


# ----------------------------------------------------------------- helpers
def utcnow():
    forced = os.environ.get("FAKE_NOW")
    if forced:
        return datetime.fromisoformat(forced).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def load_yaml(name):
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def clean(text):
    return CTRL.sub("", text or "")


def strip_html(raw):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def norm_link(url):
    parts = urlsplit((url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(TRACKING)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def item_id(link):
    return hashlib.sha1(norm_link(link).encode("utf-8")).hexdigest()[:16]


def term_regex(term):
    term = str(term)
    if term.startswith("re:"):
        return term[3:]
    words = term.lower().split()
    return r"\b" + r"\s+".join(re.escape(w) for w in words)


def compile_terms(terms):
    regs = [term_regex(t) for t in terms or []]
    if not regs:
        return None
    return re.compile("|".join("(?:%s)" % r for r in regs), re.I)


def parse_dt(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def write_if_changed(path, text):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return True


# ---------------------------------------------------------------- fetching
def fetch(url):
    if url.startswith("file:"):
        with open(url[5:], "rb") as fh:
            return fh.read()
    resp = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def find_image(entry):
    for key in ("media_thumbnail", "media_content"):
        value = entry.get(key)
        if isinstance(value, list) and value and value[0].get("url", "").startswith("http"):
            return value[0]["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            if str(link.get("href", "")).startswith("http"):
                return link["href"]
    blob = (entry.get("summary") or "")
    if entry.get("content"):
        blob += entry["content"][0].get("value", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', blob, re.I)
    if match and match.group(1).startswith("http"):
        return html.unescape(match.group(1))
    return None


def find_prices(text):
    seen = []
    for m in re.findall(r"[$€£]\s?\d[\d,]*(?:\.\d{1,2})?", text):
        m = m.replace(" ", "")
        if m not in seen:
            seen.append(m)
    return seen[:3]


# -------------------------------------------------------------- processing
def process_source(src, matchers, now, max_age, per_source_limit):
    """Fetch one source. Returns (health_row, candidate_items)."""
    health = {"name": src["name"], "tier": src.get("tier", ""), "ok": False,
              "entries": 0, "kept": 0, "error": ""}
    items = []
    try:
        parsed = feedparser.parse(fetch(src["url"]))
        entries = parsed.entries or []
        if not entries:
            reason = str(getattr(parsed, "bozo_exception", "") or "no entries found")
            raise ValueError(reason)
        health["ok"] = True
        health["entries"] = len(entries)
        for entry in entries[:per_source_limit]:
            item = build_item(src, entry, matchers, now, max_age)
            if item:
                items.append(item)
        health["kept"] = len(items)
    except Exception as exc:  # noqa: BLE001 - one broken source must not stop the run
        health["error"] = clean(str(exc))[:160]
    return health, items


def build_item(src, entry, matchers, now, max_age):
    title = clean(strip_html(entry.get("title", "")))
    link = (entry.get("link") or "").strip()
    if not title or not link.startswith("http"):
        return None

    published = entry_date(entry)
    if published and published < now - timedelta(days=max_age):
        return None
    if published and published > now + timedelta(hours=1):
        published = now

    raw_summary = entry.get("summary") or ""
    if not raw_summary and entry.get("content"):
        raw_summary = entry["content"][0].get("value", "")
    summary = clean(strip_html(raw_summary))
    text = title + " " + summary[:300]

    if matchers["exclude"] and matchers["exclude"].search(title):
        return None
    if src.get("footwear_only") and matchers["footwear"] and not matchers["footwear"].search(text):
        return None

    signals = [name for name, rx in matchers["signals"].items() if rx.search(text)]
    if not signals:
        if src.get("default_signal"):
            signals = [src["default_signal"]]
        elif src.get("keep_unmatched"):
            signals = ["Other"]
        else:
            return None

    return {
        "id": item_id(link),
        "title": title,
        "link": link,
        "source": src["name"],
        "site": src.get("site", ""),
        "tier": src.get("tier", ""),
        "published": (published or now).isoformat(),
        "first_seen": now.isoformat(),
        "signals": signals,
        "summary": summary[:400],
        "image": find_image(entry),
        "prices": find_prices(text),
        "also_in": [],
        "coverage": 1,
        "_has_date": bool(published),
    }


def merge(previous, candidates, now, max_age, max_items):
    by_id, by_title = {}, {}
    for old in previous:
        old.pop("_has_date", None)
        by_id[old["id"]] = old
        by_title.setdefault(title_key(old["title"]), old["id"])

    added = 0
    for item in candidates:  # candidates arrive in tier order, so tier A is canonical
        item.pop("_has_date", None)
        if item["id"] in by_id:
            continue
        tkey = title_key(item["title"])
        twin = by_title.get(tkey)
        if twin and twin in by_id:
            host = by_id[twin]
            if host["source"] != item["source"] and all(a["source"] != item["source"] for a in host["also_in"]):
                host["also_in"].append({"source": item["source"], "link": item["link"]})
                host["coverage"] = 1 + len(host["also_in"])
            continue
        by_id[item["id"]] = item
        by_title[tkey] = item["id"]
        added += 1

    cutoff = now - timedelta(days=max_age)
    kept = [i for i in by_id.values() if parse_dt(i["published"]) >= cutoff]
    kept.sort(key=lambda i: (i["published"], i["id"]), reverse=True)
    return kept[:max_items], added


# ------------------------------------------------------------------ output
def primary_signal(item):
    for name in PRIMARY_ORDER:
        if name in item["signals"]:
            return name
    return "Other"


def build_rss(items, title, description, filename):
    last = max((parse_dt(i["published"]) for i in items), default=EPOCH)
    self_url = "%s/%s" % (SITE_URL, filename) if SITE_URL else ""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>",
           "<title>%s</title>" % escape(title),
           "<link>%s</link>" % escape(SITE_URL or "https://example.invalid/"),
           "<description>%s</description>" % escape(description),
           "<language>en</language>",
           "<lastBuildDate>%s</lastBuildDate>" % format_datetime(last, usegmt=True)]
    if self_url:
        out.append('<atom:link href="%s" rel="self" type="application/rss+xml"/>' % escape(self_url))
    for item in items:
        desc = item["summary"]
        if item["prices"]:
            desc += (" " if desc else "") + "Prices: " + ", ".join(item["prices"]) + "."
        desc += (" " if desc else "") + "Source: " + item["source"]
        if item["coverage"] > 1:
            desc += " (also covered by %d other source%s)" % (item["coverage"] - 1, "" if item["coverage"] == 2 else "s")
        out.append("<item>")
        out.append("<title>%s</title>" % escape("[%s] %s" % (primary_signal(item), item["title"])))
        out.append("<link>%s</link>" % escape(item["link"]))
        out.append('<guid isPermaLink="false">%s</guid>' % item["id"])
        out.append("<pubDate>%s</pubDate>" % format_datetime(parse_dt(item["published"]), usegmt=True))
        out.append('<source url="%s">%s</source>' % (escape(item["site"] or item["link"]), escape(item["source"])))
        for sig in item["signals"]:
            out.append("<category>%s</category>" % escape(sig))
        out.append("<description>%s</description>" % escape(desc))
        img = item.get("image")
        if img:
            ext = img.split("?")[0].rsplit(".", 1)[-1].lower()
            mime = {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
            out.append('<enclosure url="%s" length="0" type="%s"/>' % (escape(img), mime))
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def write_outputs(items, health, added, now):
    os.makedirs(OUT_DIR, exist_ok=True)
    write_if_changed(os.path.join(OUT_DIR, "feed.xml"),
                     build_rss(items, "Footwear radar: all", "Drops, collabs, sales, outlets and reviews", "feed.xml"))
    for sig in SIGNAL_FEEDS:
        subset = [i for i in items if sig in i["signals"]]
        name = "feed-%s.xml" % sig.lower()
        write_if_changed(os.path.join(OUT_DIR, name),
                         build_rss(subset, "Footwear radar: %s" % sig.lower(), "Only items labelled %s" % sig, name))

    # items.json keeps its timestamp unless the items themselves changed
    path = os.path.join(OUT_DIR, "items.json")
    previous = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh)
    if previous.get("items") != items:
        payload = {"updated_at": now.isoformat(), "count": len(items), "items": items}
        write_if_changed(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

    hpath = os.path.join(OUT_DIR, "health.json")
    old_h = {}
    if os.path.exists(hpath):
        with open(hpath, encoding="utf-8") as fh:
            old_h = json.load(fh)
    if old_h.get("sources") != health:
        payload = {"checked_at": now.isoformat(), "sources": health}
        write_if_changed(hpath, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def main():
    cfg = load_yaml("sources.yml")
    kw = load_yaml("keywords.yml")
    defaults = cfg.get("defaults", {})
    max_age = int(defaults.get("max_age_days", 14))
    max_items = int(defaults.get("max_items", 600))
    per_source = int(defaults.get("per_source_limit", 40))

    matchers = {
        "signals": {name: rx for name, rx in
                    ((n, compile_terms(t)) for n, t in (kw.get("signals") or {}).items()) if rx},
        "exclude": compile_terms(kw.get("exclude")),
        "footwear": compile_terms(kw.get("footwear")),
    }
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    sources.sort(key=lambda s: s.get("tier", "Z"))  # stable: A first, then B, then C
    now = utcnow()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: process_source(s, matchers, now, max_age, per_source), sources))

    health = [h for h, _ in results]
    if not any(h["ok"] for h in health):
        print("Every source failed; leaving the published files untouched.", file=sys.stderr)
        for h in health:
            print(" - %s: %s" % (h["name"], h["error"]), file=sys.stderr)
        return 1

    candidates = [item for _, items in results for item in items]
    previous = []
    path = os.path.join(OUT_DIR, "items.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh).get("items", [])

    items, added = merge(previous, candidates, now, max_age, max_items)
    write_outputs(items, health, added, now)

    failing = [h for h in health if not h["ok"]]
    print("Sources ok: %d/%d | new items: %d | total kept: %d" %
          (len(health) - len(failing), len(health), added, len(items)))
    for h in failing:
        print("  failed: %s - %s" % (h["name"], h["error"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
