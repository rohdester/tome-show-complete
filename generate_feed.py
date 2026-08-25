#!/usr/bin/env python3
"""Build a complete, unofficial RSS feed for The Tome Show.

The first run crawls the public Podbean archive. Later runs retain that cache,
refresh the newest archive pages, and merge in every item from the official RSS.
No audio is copied; enclosure URLs continue to point to the publisher's files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import ssl
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

SITE = "https://www.thetomeshow.com"
OFFICIAL_FEED = f"{SITE}/feed.xml"
USER_AGENT = "TomeShowCompleteFeed/1.0 (+personal podcast archive index)"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
CONTENT = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)
ET.register_namespace("content", CONTENT)


def fetch(url: str, attempts: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # The custom domain currently serves a certificate that does not include its
    # hostname. Limit the workaround strictly to this known public site.
    context = ssl._create_unverified_context() if urlsplit(url).hostname == "www.thetomeshow.com" else None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45, context=context) as response:
                return response.read()
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/") + "/", "", ""))


def text_of(node: ET.Element | None, default: str = "") -> str:
    return (node.text or default).strip() if node is not None else default


def parse_official(raw: bytes) -> tuple[dict, list[dict]]:
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Official feed has no channel")
    metadata = {
        "title": text_of(channel.find("title"), "The Tome Show"),
        "description": text_of(channel.find("description"), "A D&D podcast."),
        "image": (channel.find(f"{{{ITUNES}}}image").get("href", "") if channel.find(f"{{{ITUNES}}}image") is not None else ""),
        "author": text_of(channel.find(f"{{{ITUNES}}}author"), "Tome Show Productions"),
    }
    items = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        link = text_of(item.find("link"))
        items.append({
            "title": text_of(item.find("title")),
            "url": canonical_url(link),
            "guid": text_of(item.find("guid")) or canonical_url(link),
            "date": text_of(item.find("pubDate")),
            "timestamp": int(email.utils.parsedate_to_datetime(text_of(item.find("pubDate"))).timestamp()),
            "description": text_of(item.find(f"{{{CONTENT}}}encoded")) or text_of(item.find("description")),
            "media_url": enclosure.get("url", ""),
            "media_type": enclosure.get("type", "audio/mpeg"),
            "length": enclosure.get("length", "0"),
            "duration": text_of(item.find(f"{{{ITUNES}}}duration")),
            "image": (item.find(f"{{{ITUNES}}}image").get("href", "") if item.find(f"{{{ITUNES}}}image") is not None else ""),
        })
    return metadata, items


def json_ld_episodes(raw: bytes) -> list[dict]:
    page = raw.decode("utf-8", "replace")
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    episodes = []
    for block in blocks:
        try:
            data = json.loads(html.unescape(block))
        except json.JSONDecodeError:
            continue
        for entry in data if isinstance(data, list) else [data]:
            if entry.get("@type") != "PodcastEpisode":
                continue
            media = entry.get("associatedMedia") or {}
            media_url = media.get("contentUrl", "")
            if not media_url:
                continue
            published = dt.datetime.fromisoformat(entry["datePublished"]).replace(tzinfo=dt.timezone.utc)
            url = canonical_url(urljoin(SITE, entry["url"]))
            episodes.append({
                "title": html.unescape(entry.get("name", "")),
                "url": url,
                "guid": "tome-show-archive:" + hashlib.sha256(url.encode()).hexdigest()[:24],
                "date": email.utils.format_datetime(published),
                "timestamp": int(published.timestamp()),
                "description": html.unescape(entry.get("description", "")),
                "media_url": media_url,
                "media_type": "audio/mpeg",
                "length": "0",
                "duration": "",
                "image": "",
            })
    return episodes


def archive_page(page: int) -> list[dict]:
    url = SITE + ("/" if page == 1 else f"/page/{page}/")
    return json_ld_episodes(fetch(url))


def discover_page_count(raw_home: bytes) -> int:
    page = raw_home.decode("utf-8", "replace")
    match = re.search(r'(?:listTotalPage|listTotalPage\\u0022):(?:\\u0022)?(\d+)', page)
    if not match:
        match = re.search(r'listTotalPage\\?"\s*:\s*(\d+)', page)
    if not match:
        raise ValueError("Could not discover archive page count")
    return int(match.group(1))


def load_cache(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def save_cache(path: Path, episodes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(episodes, ensure_ascii=False, indent=2) + "\n")


def deduplicate(*groups: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for group in groups:
        for episode in group:
            keys = [canonical_url(episode["url"]), episode["media_url"].split("?", 1)[0]]
            existing = next((by_key[k] for k in keys if k in by_key), None)
            if existing:
                # Official RSS data is richer and is passed last.
                existing.update({key: value for key, value in episode.items() if value not in ("", "0", None)})
                for key in keys:
                    by_key[key] = existing
            else:
                for key in keys:
                    by_key[key] = episode
    unique = {id(value): value for value in by_key.values()}.values()
    return sorted(unique, key=lambda episode: episode["timestamp"], reverse=True)


def add_text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.text = value
    return node


def build_feed(path: Path, public_url: str, metadata: dict, episodes: list[dict]) -> None:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    add_text(channel, "title", metadata["title"] + " — Complete Archive")
    add_text(channel, "link", SITE)
    add_text(channel, "description", metadata["description"] + " Unofficial complete archive index; audio remains hosted by the publisher.")
    add_text(channel, "language", "en-us")
    add_text(channel, f"{{{ITUNES}}}author", metadata["author"])
    add_text(channel, f"{{{ITUNES}}}explicit", "false")
    ET.SubElement(channel, f"{{{ATOM}}}link", {"href": public_url, "rel": "self", "type": "application/rss+xml"})
    if metadata.get("image"):
        ET.SubElement(channel, f"{{{ITUNES}}}image", {"href": metadata["image"]})
    add_text(channel, "lastBuildDate", email.utils.format_datetime(dt.datetime.now(dt.timezone.utc)))
    for episode in episodes:
        item = ET.SubElement(channel, "item")
        add_text(item, "title", episode["title"])
        add_text(item, "link", episode["url"])
        guid = add_text(item, "guid", episode["guid"])
        guid.set("isPermaLink", "false")
        add_text(item, "pubDate", episode["date"])
        add_text(item, "description", episode["description"])
        enclosure = ET.SubElement(item, "enclosure", {
            "url": episode["media_url"], "type": episode["media_type"], "length": episode["length"]
        })
        if episode.get("duration"):
            add_text(item, f"{{{ITUNES}}}duration", episode["duration"])
        if episode.get("image"):
            ET.SubElement(item, f"{{{ITUNES}}}image", {"href": episode["image"]})
    ET.indent(rss, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="recrawl every archive page")
    parser.add_argument("--cache", type=Path, default=Path("data/archive.json"))
    parser.add_argument("--output", type=Path, default=Path("public/feed.xml"))
    parser.add_argument("--public-url", default="https://YOUR-USERNAME.github.io/tome-show-complete/feed.xml")
    args = parser.parse_args()

    official_raw = fetch(OFFICIAL_FEED)
    metadata, official = parse_official(official_raw)
    cached = load_cache(args.cache)
    home_raw = fetch(SITE + "/")
    page_count = discover_page_count(home_raw)
    pages = range(1, page_count + 1) if args.full or not cached else range(1, min(4, page_count) + 1)
    print(f"Crawling {len(pages)} of {page_count} archive pages...")
    crawled: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(archive_page, pages):
            crawled.extend(found)
    episodes = deduplicate(cached, crawled, official)
    save_cache(args.cache, episodes)
    build_feed(args.output, args.public_url, metadata, episodes)
    print(f"Wrote {len(episodes)} episodes to {args.output}")


if __name__ == "__main__":
    main()
