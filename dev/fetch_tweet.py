#!/usr/bin/env python3
"""Quick tweet fetcher using tweetxvault's own client/extractor stack.

Fetches a single tweet by URL or numeric ID via the X TweetDetail GraphQL
endpoint and prints its contents (text, author, media, URLs, note-tweet text).

Usage:
  python dev/fetch_tweet.py <tweet-url-or-id> [--cookies FILE]
"""

from __future__ import annotations

import argparse
import asyncio

from _common import (
    apply_cookies_file,
    build_thread_depths,
    ensure_project_on_path,
    fetch_tweet_detail,
    normalize_tweet_id,
)

ensure_project_on_path()


async def run(tid: str, cookies_path: str | None) -> int:
    if cookies_path:
        apply_cookies_file(cookies_path)

    tweet, raw_json, all_tweets = await fetch_tweet_detail(tid)
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

    thread_tweets = [t for t in all_tweets if t.tweet_id != tweet.tweet_id]
    if thread_tweets:
        depths = build_thread_depths(all_tweets, tweet.tweet_id)
        sorted_ctx = sorted(
            thread_tweets,
            key=lambda t: (depths.get(t.tweet_id, 0), t.created_at or ""),
        )
        print(f"=== Thread ({len(thread_tweets)} additional tweets) ===")
        for ct in sorted_ctx:
            d = depths.get(ct.tweet_id, 0)
            indent = "  " * d
            parent_id = (ct.raw_json.get("legacy") or {}).get("in_reply_to_status_id_str")
            tag = "PARENT" if parent_id == tweet.tweet_id else "reply"
            print(f"{indent}├── [{tag}] @{ct.author_username} {ct.created_at}")
            for line in (ct.text or "").split("\n"):
                print(f"{indent}│   {line}")
            print()
    else:
        print("=== (no thread context in response) ===\n")

    note = raw.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    if note_text := note.get("text"):
        print(f"Note tweet text ({len(note_text)} chars):")
        print(note_text[:500] + ("...(truncated)" if len(note_text) > 500 else ""))
        print()

    if media := legacy.get("extended_entities", {}).get("media", []) or []:
        print(f"Media ({len(media)} items):")
        for m in media:
            print(f"  - type={m.get('type')}, url={m.get('media_url_https') or m.get('media_url')}")
            if m.get("video_info"):
                variants = m["video_info"].get("variants", []) or []
                if best := (max(variants, key=lambda v: v.get("bitrate", 0)) if variants else None):
                    print(f"    video: {best.get('url')}")

    if urls := legacy.get("entities", {}).get("urls", []) or []:
        print(f"URLs ({len(urls)}):")
        for u in urls:
            expanded = u.get("expanded_url") or u.get("unwound_url") or u.get("display_url")
            print(f"  - {expanded}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch a single X tweet by URL or ID using tweetxvault client.",
    )
    ap.add_argument("tweet", help="tweet URL or numeric id")
    ap.add_argument("--cookies", help="Netscape cookies.txt path (default: env vars)")
    args = ap.parse_args()
    tid = normalize_tweet_id(args.tweet)
    rc = asyncio.run(run(tid, args.cookies))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
