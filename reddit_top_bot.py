#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit Top Daily -> Telegram channel automation.

Fetches the day's top posts from science subreddits, translates them into
engaging Persian with an LLM (see llm_translator.py), and posts the best
POSTS_COUNT of them to the Telegram channel, with the preview image when
available.

Reddit data sources are tried in order (datacenter IPs are often blocked by
Reddit's public JSON API, so a mirror is the reliable fallback):
  1. the public JSON endpoints (www/old/api.reddit.com) — live scores
  2. the official OAuth API, if REDDIT_CLIENT_ID/SECRET are set — live scores
  3. the Arctic-Shift archive mirror (no auth) — scores updated periodically

De-duplication: STATE_FILE remembers recently posted Reddit post ids, so a
post is never published twice (the backup workflow run is a no-op when the
main run already succeeded).

Command line:
    python reddit_top_bot.py                post today's top posts
    python reddit_top_bot.py --dry-run      fetch + translate + print, no sending
    python reddit_top_bot.py --force        ignore the de-duplication state
    python reddit_top_bot.py --diag         connectivity report, then exit

Required environment variables (GitHub Secrets):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY

Optional environment variables:
    SUBREDDITS          default "science+space+astronomy" (+ separated)
    POSTS_COUNT         default 5
    MIN_SCORE           default 50
    MIRROR_HOURS        default 54 — mirror look-back window. The mirror
                        refreshes post scores roughly 30h after creation, so
                        a wider window is required to see real (updated)
                        scores; the de-duplication state makes it safe.
    STATE_FILE          default state_reddit.json
    FORCE_POST          "true" — same as --force
    REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET — official OAuth API (optional)
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from llm_translator import translate_fields, provider_report

# reuse the battle-tested Telegram helpers from the NASA bot
from nasa_apod_bot import (
    MAX_CAPTION_LEN,
    shrink_for_telegram,
    split_text,
    send_media,
    send_message,
)

# --------------------------------------------------------------------------- #
#  Configuration                                                               #
# --------------------------------------------------------------------------- #

SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "science+space+astronomy").replace(",", "+").split("+") if s.strip()]
POSTS_COUNT = int(os.environ.get("POSTS_COUNT", "5"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "50"))
MIRROR_HOURS = int(os.environ.get("MIRROR_HOURS", "54"))
STATE_FILE = os.environ.get("STATE_FILE", "state_reddit.json").strip()
CHANNEL_SIGNATURE = os.environ.get("CHANNEL_SIGNATURE", "").strip()

REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()

USER_AGENT = (
    "DailyScienceSpaceBot/1.0 (GitHub Actions cron; "
    "channel @daily_sciences)"
)

REDDIT_HOSTS = [
    "https://www.reddit.com",
    "https://old.reddit.com",
    "https://api.reddit.com",
]

ARCTIC_SHIFT = "https://arctic-shift.photon-reddit.com/api/posts/search"

MAX_IMAGE_SIZE = 48 * 1024 * 1024
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:[?#]|$)", re.I)

log = logging.getLogger("reddit-bot")


# --------------------------------------------------------------------------- #
#  Reddit data sources                                                          #
# --------------------------------------------------------------------------- #

def _from_reddit_children(children: list) -> list:
    """Normalize Reddit's {'data': {'children': [...]}} shape to flat dicts."""
    posts = []
    for child in children:
        d = child.get("data", {}) if isinstance(child, dict) else {}
        if d:
            posts.append(d)
    return posts


def fetch_via_public_json() -> list:
    """Try the public .json endpoints (live data, but often blocked for IPs)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"t": "day", "limit": "25"}
    multisub = "+".join(SUBREDDITS)
    for base in REDDIT_HOSTS:
        suffix = "" if base.startswith("https://api.reddit.com") else ".json"
        try:
            resp = requests.get(f"{base}/r/{multisub}/top{suffix}",
                                headers=headers, params=params, timeout=(20, 60))
            if resp.status_code == 200:
                children = (resp.json().get("data") or {}).get("children")
                if isinstance(children, list):
                    log.info("Fetched via public JSON from %s (%d posts)", base, len(children))
                    return _from_reddit_children(children)
            log.warning("Public JSON host %s returned HTTP %s", base, resp.status_code)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Public JSON host %s failed: %s", base, exc)
    return []


def fetch_via_oauth() -> list:
    """Use the official OAuth API when app credentials are configured."""
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return []
    try:
        tok = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            headers={"User-Agent": USER_AGENT},
            data={"grant_type": "client_credentials"},
            timeout=(20, 40),
        )
        tok.raise_for_status()
        bearer = tok.json().get("access_token")
        resp = requests.get(
            f"https://oauth.reddit.com/r/{'+'.join(SUBREDDITS)}/top",
            headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {bearer}"},
            params={"t": "day", "limit": "25"},
            timeout=(20, 60),
        )
        children = (resp.json().get("data") or {}).get("children")
        if isinstance(children, list):
            log.info("Fetched via official OAuth API (%d posts)", len(children))
            return _from_reddit_children(children)
        log.warning("OAuth listing returned HTTP %s", resp.status_code)
    except (requests.RequestException, ValueError) as exc:
        log.warning("OAuth fetch failed: %s", exc)
    return []


def fetch_via_arctic_shift() -> list:
    """
    Fallback: the Arctic-Shift archive mirror (no auth needed).

    Scores are re-crawled roughly 30h after post creation, so recent posts
    carry stale (near-zero) scores while posts from ~1-2 days ago have real,
    updated scores. The MIRROR_HOURS look-back window (default 54h) therefore
    spans far enough back to rank yesterday's true top posts, and the
    de-duplication state guarantees no post is ever published twice.
    Net effect: the channel carries each day's genuine top posts with about
    a one-day delay. Setting REDDIT_CLIENT_ID/SECRET (official OAuth API)
    removes that delay entirely.
    """
    after = f"{MIRROR_HOURS}h"
    posts = []
    for sub in SUBREDDITS:
        try:
            resp = requests.get(
                ARCTIC_SHIFT,
                params={"subreddit": sub, "after": after, "limit": "100",
                        "sort": "desc", "sort_type": "created_utc"},
                timeout=(20, 90),
            )
            if resp.status_code != 200:
                log.warning("Arctic-Shift returned HTTP %s for r/%s", resp.status_code, sub)
                continue
            data = resp.json().get("data") or []
            posts.extend(data)
            log.info("Arctic-Shift r/%s: %d posts", sub, len(data))
        except (requests.RequestException, ValueError) as exc:
            log.warning("Arctic-Shift failed for r/%s: %s", sub, exc)
        time.sleep(1)
    if not posts:
        log.warning("Arctic-Shift returned no posts at all")
    return posts


def fetch_reddit_posts() -> list:
    """Fetch posts from the first source that works (best quality first)."""
    for fetcher, label in (
        (fetch_via_public_json, "public JSON"),
        (fetch_via_oauth, "OAuth"),
        (fetch_via_arctic_shift, "Arctic-Shift mirror"),
    ):
        if fetcher is fetch_via_oauth and not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
            continue
        posts = fetcher()
        if posts:
            log.info("Reddit data source: %s", label)
            return posts
    raise RuntimeError(
        "All Reddit data sources failed. Public JSON endpoints are blocked "
        "from datacenter IPs; consider setting REDDIT_CLIENT_ID and "
        "REDDIT_CLIENT_SECRET (free app from reddit.com/prefs/apps) for the "
        "official OAuth API, or try again later if the mirror is down."
    )


# --------------------------------------------------------------------------- #
#  Post selection                                                               #
# --------------------------------------------------------------------------- #

def pick_posts(posts: list, posted_ids: set) -> list:
    """Filter, de-duplicate and rank raw post dicts; return the best posts."""
    picked, seen = [], set()
    for d in posts:
        if not isinstance(d, dict):
            continue
        if d.get("stickied") or d.get("pinned"):
            continue
        if d.get("over_18"):
            continue
        score = d.get("score") or 0
        if score < MIN_SCORE:
            continue
        pid = d.get("id")
        if not pid or pid in posted_ids or pid in seen:
            continue
        seen.add(pid)

        image_url = None
        try:
            source = d["preview"]["images"][0]["source"]
            url = html_mod.unescape(source.get("url", ""))
            if url.startswith("http") and (source.get("width") or 0) >= 320:
                image_url = url
        except (KeyError, TypeError, IndexError):
            pass
        if not image_url:
            url = d.get("url") or ""
            if url.startswith("https://i.redd.it/") and _IMAGE_EXT_RE.search(url):
                image_url = url

        picked.append({
            "id": pid,
            "title": (d.get("title") or "").strip(),
            "score": int(score),
            "comments": int(d.get("num_comments") or 0),
            "subreddit": d.get("subreddit") or "",
            "selftext": (d.get("selftext") or "").strip()[:1500],
            "image": image_url,
        })
    picked.sort(key=lambda p: p["score"], reverse=True)
    return picked


# --------------------------------------------------------------------------- #
#  Translation                                                                  #
# --------------------------------------------------------------------------- #

def translate_post(post: dict):
    """Translate one Reddit post to Persian; None on failure (English fallback)."""
    prompt = (
        "Translate this Reddit science post into engaging, natural Persian "
        "(Farsi) for a Telegram science channel.\n\n"
        f"Subreddit: r/{post['subreddit']}\n"
        f"Title: {post['title']}\n"
    )
    if post["selftext"]:
        prompt += f"Post text: {post['selftext'][:1200]}\n"
    prompt += (
        "\nRules:\n"
        "- title_fa: an attractive, faithful Persian translation of the title\n"
        "- summary_fa: 1-3 short Persian sentences summarizing the post; friendly "
        "scientific tone; use the post text when it adds real information, but "
        "if the post text is only a question to readers, a call for comments, "
        "or meta content (edits, thanks, links), base the summary on the title "
        "alone and ignore that text\n"
        "- No links, no hashtags, no markdown symbols; keep proper names in Latin "
        "where that is more natural\n"
        'Respond ONLY as JSON: {"title_fa": "...", "summary_fa": "..."}'
    )
    return translate_fields(prompt, ("title_fa", "summary_fa"))


# --------------------------------------------------------------------------- #
#  Media + text building                                                        #
# --------------------------------------------------------------------------- #

def humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)


def build_post_text(post: dict, translation: dict) -> str:
    stats = (f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
             f"• r/{post['subreddit']}")
    lines = [
        "🔥 برترهای ردیت | Reddit Daily Top",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if translation:
        lines.append(f"🇮🇷 {translation['title_fa']}")
        if translation.get("summary_fa"):
            lines.append("")
            lines.append(translation["summary_fa"])
        lines.append("")
        lines.append(stats)
        lines.append(f"🇬🇧 {post['title']}")
    else:
        lines.append(f"🇬🇧 {post['title']}")
        lines.append("")
        lines.append(stats)
    if CHANNEL_SIGNATURE:
        lines.append("")
        lines.append(f"— {CHANNEL_SIGNATURE}")
    return "\n".join(lines)


def build_caption(post: dict, translation: dict) -> str:
    """Media caption: the full text if it fits, otherwise a compact version."""
    full = build_post_text(post, translation)
    if len(full) <= MAX_CAPTION_LEN:
        return full
    stats = (f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
             f"• r/{post['subreddit']}")
    if translation:
        compact = "\n".join([
            "🔥 برترهای ردیت | Reddit Daily Top",
            f"🇮🇷 {translation['title_fa']}",
            "",
            stats,
        ])
    else:
        compact = "\n".join([
            "🔥 برترهای ردیت | Reddit Daily Top",
            f"🇬🇧 {post['title']}",
            "",
            stats,
        ])
    return compact[: MAX_CAPTION_LEN - 1] + "…" if len(compact) > MAX_CAPTION_LEN else compact


def download_preview(url: str, workdir: Path):
    """Download a Reddit/i.redd.it image. Returns a path or None."""
    try:
        resp = requests.get(url, timeout=(20, 120),
                            headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith("image/"):
            return None
        data = resp.content
        if len(data) < 15_000 or len(data) > MAX_IMAGE_SIZE:
            return None
        ext = Path(urlparse(url).path).suffix or mimetypes.guess_extension(content_type) or ".jpg"
        path = workdir / f"reddit_image{ext}"
        path.write_bytes(data)
        return path
    except requests.RequestException as exc:
        log.warning("Preview image download failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
#  State (duplicate protection)                                                 #
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_posted_id(pid: str) -> None:
    state = load_state()
    posted = [p for p in state.get("posted_ids", []) if isinstance(p, str)]
    posted.append(pid)
    posted = posted[-150:]  # remember the last 150 posts
    Path(STATE_FILE).write_text(
        json.dumps(
            {"posted_ids": posted,
             "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
#  Diagnostics                                                                  #
# --------------------------------------------------------------------------- #

def reddit_report() -> str:
    lines = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    multisub = "+".join(SUBREDDITS)
    for base in REDDIT_HOSTS:
        suffix = "" if base.startswith("https://api.reddit.com") else ".json"
        try:
            resp = requests.get(f"{base}/r/{multisub}/top{suffix}", headers=headers,
                                params={"t": "day", "limit": "5"}, timeout=(15, 40))
            if resp.status_code == 200:
                n = len((resp.json().get("data") or {}).get("children") or [])
                lines.append(f"{base}: HTTP {resp.status_code}, children={n} — OK")
            else:
                lines.append(f"{base}: HTTP {resp.status_code} — BLOCKED/ERROR")
        except Exception as exc:
            lines.append(f"{base}: {exc}")
    try:
        resp = requests.get(ARCTIC_SHIFT,
                            params={"subreddit": SUBREDDITS[0], "after": "24h",
                                    "limit": "5", "sort": "desc",
                                    "sort_type": "created_utc"},
                            timeout=(15, 60))
        if resp.status_code == 200:
            n = len(resp.json().get("data") or [])
            lines.append(f"arctic-shift ({SUBREDDITS[0]}): HTTP 200, posts={n} — OK")
        else:
            lines.append(f"arctic-shift: HTTP {resp.status_code} — ERROR")
    except Exception as exc:
        lines.append(f"arctic-shift: {exc}")
    lines.append(
        "OAuth credentials: " + ("present" if REDDIT_CLIENT_ID else "not set (optional)")
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post the day's top Reddit science posts to a Telegram channel."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + translate + print, without sending anything")
    parser.add_argument("--force", action="store_true",
                        help="ignore the de-duplication state")
    parser.add_argument("--diag", action="store_true",
                        help="connectivity diagnostics, then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.diag:
        print("=== LLM ===")
        print(provider_report())
        print("=== Reddit ===")
        print(reddit_report())
        return 0

    log.info("Subreddits: %s | top %d | min score %d | mirror window %dh",
             "+".join(SUBREDDITS), POSTS_COUNT, MIN_SCORE, MIRROR_HOURS)

    force = args.force or os.environ.get("FORCE_POST", "").strip().lower() in ("1", "true", "yes", "on")
    posted_ids = set(load_state().get("posted_ids", [])) if not force else set()
    if posted_ids:
        log.info("Duplicate protection active: %d recently posted ids", len(posted_ids))

    posts = pick_posts(fetch_reddit_posts(), posted_ids)[:POSTS_COUNT]
    if not posts:
        log.info("No new qualifying Reddit posts today — nothing to do.")
        return 0
    log.info("Selected %d posts: %s", len(posts),
             " | ".join(f"[{p['score']}]{p['title'][:35]}…" for p in posts))

    workdir = Path(tempfile.mkdtemp(prefix="reddit_"))
    for index, post in enumerate(posts, 1):
        translation = translate_post(post)
        if translation:
            log.info("[%d/%d] translated: %s", index, len(posts), translation["title_fa"][:60])
        else:
            log.info("[%d/%d] translation unavailable — English fallback", index, len(posts))

        if args.dry_run:
            print("\n" + "=" * 62)
            print(f"REDDIT POST PREVIEW {index}/{len(posts)} (dry run)")
            print("=" * 62)
            print(build_post_text(post, translation))
            print(f"[image: {post['image'] or 'none'}]")
            continue

        image = None
        if post["image"]:
            raw = download_preview(post["image"], workdir)
            if raw:
                image = shrink_for_telegram(raw) or raw

        if image is not None:
            send_media(image, build_caption(post, translation))
        else:
            for chunk in split_text(build_post_text(post, translation)):
                send_message(chunk)
        save_posted_id(post["id"])
        time.sleep(2)  # gentle pacing between posts

    if args.dry_run:
        print("\n(dry run — nothing was sent, state untouched)")
        return 0

    log.info("Posted %d Reddit top posts successfully.", len(posts))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        log.error("FAILED: %s", exc)
        sys.exit(1)
