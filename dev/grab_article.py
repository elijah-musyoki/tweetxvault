#!/usr/bin/env python3
"""Grab an X Article (or article-linking tweet) as Markdown, using tweetxvault's
own extractor. Lightweight: only needs httpx + pydantic (+ platformdirs,
browser-cookie3 for the cookie helpers) and the tweetxvault source tree --
NO lancedb / pyarrow / numpy.

Usage:
  python dev/grab_article.py <tweet-url-or-id> [--cookies FILE] [--out PATH]

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
from tweetxvault.extractor import _article_entry, _article_result  # noqa: E402
from tweetxvault.query_ids import QueryIdStore, refresh_query_ids  # noqa: E402
from tweetxvault.query_ids.constants import FALLBACK_QUERY_IDS  # noqa: E402

TWEET_ID_RE = re.compile(r"(?:status|x)\.com/[^/]+/status/(\d+)")


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


def resolve_cookies(args: argparse.Namespace):
    if not args.cookies:
        env = dict(os.environ)
    else:
        try:
            c = parse_cookies_file(args.cookies)
            env = dict(os.environ)
            env["TWEETXVAULT_AUTH_TOKEN"] = c["auth_token"]
            env["TWEETXVAULT_CT0"] = c["ct0"]
            env["TWEETXVAULT_USER_ID"] = c["user_id"]
        except Exception as e:
            raise SystemExit(f"could not read cookies file {args.cookies}: {e}") from e
    # set so resolve_auth_bundle picks them up
    for k in ("TWEETXVAULT_AUTH_TOKEN", "TWEETXVAULT_CT0", "TWEETXVAULT_USER_ID"):
        os.environ[k] = env.get(k, "")
    config, paths = load_config()
    return resolve_auth_bundle(config), config, paths


def normalize_tweet_id(arg: str) -> str:
    m = TWEET_ID_RE.search(arg)
    if m:
        return m.group(1)
    if arg.isdigit():
        return arg
    raise SystemExit(f"could not parse a tweet id from: {arg!r}")


def unfurl_url(url: str) -> dict:
    """Best-effort metadata fetch for an *external* article link (mirrors unfurl.py)."""
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
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_match.group(1) if title_match else None
        desc_match = re.search(
            r"""<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']""",
            html,
            re.I,
        )
        desc = desc_match.group(1) if desc_match else None
        return {
            "title": (title or "").strip(),
            "description": (desc or "").strip(),
            "final_url": str(r.url),
        }
    except Exception as e:
        return {"error": str(e)}


def to_markdown(
    tid: str,
    art,
    author: str,
    tweet_text: str | None = None,
    external_url: str | None = None,
    unfurled: dict | None = None,
) -> str:
    md = []
    md.append(f"# {art.title or 'X Article'}")
    md.append("")
    if tweet_text:
        md.append(f"> _{tweet_text.strip()}_")
        md.append("")
    if art.summary_text:
        md.append(f"> {art.summary_text}")
        md.append("")
    md.append("| field | value |")
    md.append("| --- | --- |")
    md.append(f"| Author | @{author} |")
    md.append(f"| Published | {art.published_at or ''} |")
    md.append(f"| X Article | https://x.com/i/article/{art.article_id} |")
    md.append(f"| Tweet | https://x.com/{author}/status/{tid} |")
    if external_url:
        md.append(f"| Canonical / link | {external_url} |")
    md.append(f"| Body length | {len(art.content_text or '')} chars |")
    md.append("")
    md.append("## Article body")
    md.append("")
    body = art.content_text or ""
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    if paras:
        md.extend(p.strip() for p in paras)
        md.append("")
    else:
        md.append(body)
        md.append("")
    if external_url and unfurled:
        md.append("## Linked page preview (external URL)")
        md.append("")
        if unfurled.get("error"):
            md.append(f"_unfurl error: {unfurled['error']}_")
        else:
            if unfurled.get("title"):
                md.append(f"**Title:** {unfurled['title']}")
            if unfurled.get("description"):
                md.append(f"\n{unfurled['description']}")
        md.append("")
    return "\n".join(md)


async def run(tid: str, cookies: str | None = None, out: str | None = None) -> int:
    ab, config, paths = resolve_cookies(argparse.Namespace(cookies=cookies))
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
            print("focal tweet not found in response", file=sys.stderr)
            return 1
        raw = tweet.raw_json
        ar = _article_result(raw)
        art = _article_entry(raw, ar)
        if art is None:
            print("no article found on this tweet", file=sys.stderr)
            return 1
        # author
        author = tweet.author_username or "anonymous"
        external_url = None
        unfurled = None
        if not art.content_text:
            # fall back: tweet may link to an external article
            for u in (raw.get("legacy") or {}).get("entities", {}).get("urls", []) or []:
                external_url = u.get("expanded_url") or u.get("unwound_url")
                if external_url:
                    break
            if external_url:
                unfurled = unfurl_url(external_url)
        tweet_text = tweet.text
        md = to_markdown(
            tid,
            art,
            author,
            tweet_text=tweet_text,
            external_url=external_url,
            unfurled=unfurled,
        )
        out_path = out or f"/tmp/xarticle_{tid}.md"

        def _write():
            with open(out_path, "w") as f:
                f.write(md)

        await asyncio.to_thread(_write)
        print(f"saved -> {out_path}")
        print(f"title: {art.title}")
        print(f"body: {len(art.content_text or '')} chars | status: {art.status}")
        print(f"canonical: {art.canonical_url}")
        if external_url:
            print(f"linked: {external_url}")
        return 0
    finally:
        await client.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Grab an X Article (or article-linking tweet) as Markdown.",
    )
    ap.add_argument("tweet", help="tweet URL or numeric id")
    ap.add_argument("--cookies", help="Netscape cookies.txt path")
    ap.add_argument("--out", help="output markdown path (default /tmp/xarticle_<id>.md)")
    args = ap.parse_args()
    tid = normalize_tweet_id(args.tweet)
    rc = asyncio.run(run(tid, cookies=args.cookies, out=args.out))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
