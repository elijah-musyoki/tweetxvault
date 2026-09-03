#!/usr/bin/env python3
"""Grab an X Article (or article-linking tweet) as Markdown, using tweetxvault's own extractor.

Usage:
  python dev/grab_article.py <tweet-url-or-id> [--cookies FILE] [--out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from _common import (
    apply_cookies_file,
    ensure_project_on_path,
    fetch_tweet_detail,
    normalize_tweet_id,
    unfurl_url,
)

ensure_project_on_path()

from tweetxvault.extractor import _article_entry, _article_result  # noqa: E402


def to_markdown(
    tid: str,
    art,
    author: str,
    tweet_text: str | None = None,
    external_url: str | None = None,
    unfurled: dict | None = None,
) -> str:
    md: list[str] = []
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
    if cookies:
        apply_cookies_file(cookies)

    tweet, _raw_json, _all = await fetch_tweet_detail(tid)
    if tweet is None:
        print("focal tweet not found in response", file=sys.stderr)
        return 1
    raw = tweet.raw_json
    ar = _article_result(raw)
    art = _article_entry(raw, ar)
    if art is None:
        print("no article found on this tweet", file=sys.stderr)
        return 1

    author = tweet.author_username or "anonymous"
    external_url = None
    unfurled = None
    if not art.content_text:
        for u in (raw.get("legacy") or {}).get("entities", {}).get("urls", []) or []:
            if external_url := u.get("expanded_url") or u.get("unwound_url"):
                break
        if external_url:
            unfurled = unfurl_url(external_url)

    md = to_markdown(
        tid, art, author, tweet_text=tweet.text, external_url=external_url, unfurled=unfurled
    )
    out_path = out or f"/tmp/xarticle_{tid}.md"
    await asyncio.to_thread(Path(out_path).write_text, md, encoding="utf-8")
    print(f"saved -> {out_path}")
    print(f"title: {art.title}")
    print(f"body: {len(art.content_text or '')} chars | status: {art.status}")
    print(f"canonical: {art.canonical_url}")
    if external_url:
        print(f"linked: {external_url}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Grab an X Article (or article-linking tweet) as Markdown."
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
