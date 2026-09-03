"""Shared helpers for dev/ scripts — tweet id parsing, cookies, client bootstrap.

keeps dev/ DRY: both fetch_tweet.py and grab_article.py reuse the same
TweetDetail fetch and auth logic, no copy-paste.
"""

from __future__ import annotations

import asyncio
import os
import re
import urllib.parse
from pathlib import Path

TWEET_ID_RE = re.compile(r"(?:status|x)\.com/[^/]+/status/(\d+)")


def normalize_tweet_id(arg: str) -> str:
    if m := TWEET_ID_RE.search(arg):
        return m.group(1)
    if arg.isdigit():
        return arg
    raise SystemExit(f"could not parse a tweet id from: {arg!r}")


def parse_cookies_file(path: str) -> dict[str, str]:
    """Parse Netscape cookies.txt -> {auth_token, ct0, twid, user_id}."""
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            name, value = parts[5].strip(), parts[6].strip()
            if name in ("auth_token", "ct0", "twid"):
                out[name] = value
    if "twid" in out:
        if m := re.search(r"u=(\d+)", urllib.parse.unquote(out["twid"])):
            out["user_id"] = m.group(1)
    return out


def apply_cookies_file(cookies_path: str | None) -> None:
    """If cookies_path given, load it and inject into os.environ (validated)."""
    if not cookies_path:
        return
    data = parse_cookies_file(cookies_path)
    missing = [k for k in ("auth_token", "ct0") if k not in data]
    if missing:
        raise SystemExit(f"cookies file {cookies_path!r} missing: {', '.join(missing)}")
    os.environ["TWEETXVAULT_AUTH_TOKEN"] = data["auth_token"]
    os.environ["TWEETXVAULT_CT0"] = data["ct0"]
    if "user_id" in data:
        os.environ["TWEETXVAULT_USER_ID"] = data["user_id"]
    elif "twid" in data:
        # twid present but user_id not extractable — leave env as-is
        pass


async def fetch_tweet_detail(tid: str):
    """Fetch TweetDetail GraphQL for tid, return (parsed_tweet, raw_json, all_tweets)."""
    # local imports — keep module importable without heavy deps at top-level
    from tweetxvault.auth import resolve_auth_bundle
    from tweetxvault.client.base import build_async_client
    from tweetxvault.client.timelines import (
        build_tweet_detail_url,
        fetch_page,
        parse_tweet_detail_response,
        parse_tweet_detail_tweets,
    )
    from tweetxvault.config import load_config
    from tweetxvault.query_ids import QueryIdStore, refresh_query_ids
    from tweetxvault.query_ids.constants import FALLBACK_QUERY_IDS

    config, paths = load_config()
    auth = resolve_auth_bundle(config)
    qid = FALLBACK_QUERY_IDS["TweetDetail"]
    url = build_tweet_detail_url(qid, tid)
    client = build_async_client(auth, timeout=config.sync.timeout)

    async def refresh_once() -> str:
        qs = QueryIdStore(paths)
        refreshed = await refresh_query_ids(qs, operations=["TweetDetail"], client=client)
        return build_tweet_detail_url(refreshed["TweetDetail"], tid)

    try:
        resp = await asyncio.wait_for(
            fetch_page(client, url, config.sync, refresh_once=refresh_once), timeout=90
        )
        j = resp.json()
        tweet = parse_tweet_detail_response(j, tid)
        all_tweets = parse_tweet_detail_tweets(j)
        return tweet, j, all_tweets
    finally:
        await client.aclose()


def build_thread_depths(all_tweets, focal_id: str) -> dict[str, int]:
    """Map tweet_id -> reply-chain depth from focal (iterative, no recursion)."""
    by_id = {t.tweet_id: t for t in all_tweets}
    depths: dict[str, int] = {}
    for t in all_tweets:
        if t.tweet_id == focal_id:
            continue
        depth = 0
        cur = t.tweet_id
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            parent = (by_id.get(cur).raw_json.get("legacy") or {}).get(  # type: ignore
                "in_reply_to_status_id_str"
            )
            if not parent or parent == cur or parent not in by_id:
                break
            # stop when we hit focal's direct parent chain? we count distance
            depth += 1
            if parent == focal_id:
                break
            cur = parent
            # guard against cycles / long chains
            if depth > 50:
                break
        depths[t.tweet_id] = depth
    return depths


def unfurl_url(url: str) -> dict:
    """Best-effort metadata for external URL (title/description)."""
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        )
    }
    try:
        r = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
        html = r.text
        title = None
        desc = None
        if m := re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S):
            title = m.group(1).strip()
        if m := re.search(
            r"""<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']""", html, re.I
        ):
            desc = m.group(1).strip()
        return {"title": title or "", "description": desc or "", "final_url": str(r.url)}
    except Exception as e:
        return {"error": str(e)}


def ensure_project_on_path() -> None:
    """Add repo root to sys.path if running as `python dev/*.py` directly."""
    import sys

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
