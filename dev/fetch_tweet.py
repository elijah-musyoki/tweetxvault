#!/usr/bin/env python3
"""Quick tweet fetcher using tweetxvault's own client/extractor stack.

Fetches a single tweet by URL or numeric ID via the X TweetDetail GraphQL
endpoint and prints its contents (text, author, media, URLs, note-tweet text).
Lightweight: only needs httpx + pydantic (+ platformdirs, browser-cookie3 for
the cookie helpers) and the tweetxvault source tree -- no lancedb/pyarrow/numpy.

Usage:
  python dev/fetch_tweet.py <tweet-url-or-id> [--cookies FILE]

Cookies: defaults to env TWEETXVAULT_AUTH_TOKEN / TWEETXVAULT_CT0 /
         TWEETXVAULT_USER_ID; or pass a Netscape-format `cookies-x-com.txt`
         via --cookies.
"""

import argparse
import asyncio
import os
import re
import sys
import urllib.parse
from pathlib import Path

# make the sibling `tweetxvault/` package importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tweetxvault.auth import resolve_auth_bundle  # noqa: E402
from tweetxvault.client.base import build_async_client  # noqa: E402
from tweetxvault.client.timelines import (  # noqa: E402
    build_tweet_detail_url,
    fetch_page,
    parse_tweet_detail_response,
)
from tweetxvault.config import load_config  # noqa: E402
from tweetxvault.query_ids import QueryIdStore, refresh_query_ids  # noqa: E402
from tweetxvault.query_ids.constants import FALLBACK_QUERY_IDS  # noqa: E402

TWEET_ID_RE = re.compile(r"(?:status|x)\.com/[^/]+/status/(\d+)")


def normalize_tweet_id(arg: str) -> str:
    m = TWEET_ID_RE.search(arg)
    if m:
        return m.group(1)
    if arg.isdigit():
        return arg
    raise SystemExit(f"could not parse a tweet id from: {arg!r}")


def parse_cookies_file(path: str) -> dict[str, str]:
    """Parse a Netscape cookies.txt -> {auth_token, ct0, twid, user_id}."""
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 7:
                continue
            name, value = p[5].strip(), p[6].strip()
            if name in ("auth_token", "ct0", "twid"):
                out[name] = value
    if "twid" in out:
        m = re.search(r"u=(\d+)", urllib.parse.unquote(out["twid"]))
        if m:
            out["user_id"] = m.group(1)
    return out


def resolve_cookies(args: argparse.Namespace) -> None:
    if not args.cookies:
        return
    try:
        c = parse_cookies_file(args.cookies)
        os.environ["TWEETXVAULT_AUTH_TOKEN"] = c["auth_token"]
        os.environ["TWEETXVAULT_CT0"] = c["ct0"]
        if "user_id" in c:
            os.environ["TWEETXVAULT_USER_ID"] = c["user_id"]
    except Exception as e:
        raise SystemExit(f"could not read cookies file {args.cookies}: {e}") from e


async def run(tid: str, cookies_path: str | None) -> int:
    resolve_cookies(argparse.Namespace(cookies=cookies_path))
    config, paths = load_config()
    ab = resolve_auth_bundle(config)
    qid = FALLBACK_QUERY_IDS["TweetDetail"]
    url = build_tweet_detail_url(qid, tid)
    client = build_async_client(ab, timeout=config.sync.timeout)

    async def refresh_once() -> str:
        qs = QueryIdStore(paths)
        refreshed = await refresh_query_ids(qs, operations=["TweetDetail"], client=client)
        return build_tweet_detail_url(refreshed["TweetDetail"], tid)

    try:
        resp = await asyncio.wait_for(
            fetch_page(client, url, config.sync, refresh_once=refresh_once),
            timeout=90,
        )
        j = resp.json()
        tweet = parse_tweet_detail_response(j, tid)
        if tweet is None:
            print("focal tweet not found in response")
            return 1
        raw = tweet.raw_json
        legacy = raw.get("legacy") or {}
        print(f"=== Tweet {tweet.tweet_id} ===")
        print(f"Author: @{tweet.author_username} ({tweet.author_display_name})")
        print(f"Created: {tweet.created_at}")
        print("Text:")
        print(tweet.text)
        print()

        note = raw.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
        note_text = note.get("text")
        if note_text:
            print(f"Note tweet text ({len(note_text)} chars):")
            print(note_text[:500])
            if len(note_text) > 500:
                print("...(truncated)")
            print()

        media = legacy.get("extended_entities", {}).get("media", []) or []
        if media:
            print(f"Media ({len(media)} items):")
            for m in media:
                media_type = m.get("type")
                media_url = m.get("media_url_https") or m.get("media_url")
                print(f"  - type={media_type}, url={media_url}")
                if m.get("video_info"):
                    variants = m["video_info"].get("variants", [])
                    best = max(variants, key=lambda v: v.get("bitrate", 0)) if variants else None
                    if best:
                        print(f"    video: {best.get('url')}")

        urls = legacy.get("entities", {}).get("urls", []) or []
        if urls:
            print(f"URLs ({len(urls)}):")
            for u in urls:
                expanded = u.get("expanded_url") or u.get("unwound_url") or u.get("display_url")
                print(f"  - {expanded}")
        return 0
    finally:
        await client.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch a single X tweet by URL or ID using tweetxvault's client.",
    )
    ap.add_argument("tweet", help="tweet URL or numeric id")
    ap.add_argument("--cookies", help="Netscape cookies.txt path (default: env vars)")
    args = ap.parse_args()
    tid = normalize_tweet_id(args.tweet)
    rc = asyncio.run(run(tid, args.cookies))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
