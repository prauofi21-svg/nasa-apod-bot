#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit Top Daily -> Telegram channel automation.

Fetches the day's top posts from science subreddits (via Reddit's public JSON
API), translates them into engaging Persian with an LLM (Grok/Groq — see
llm_translator.py), and posts the best POSTS_COUNT of them to the Telegram
channel, with the preview image when available.

De-duplication: state_reddit.json remembers recently posted Reddit post ids,
so a post is never published twice (the backup workflow run is a no-op when
the main run already succeeded).

Command line:
    python reddit_top_bot.py                post today's top posts
    python reddit_top_bot.py --dry-run      fetch + translate + print, no sending
    python reddit_top_bot.py --force        ignore the de-duplication state
    python reddit_top_bot.py --diag         connectivity report, then exit

Required environment variables (GitHub Secrets):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROK_API_KEY

Optional environment variables:
    SUBREDDITS         default "science+space+astronomy"
    POSTS_COUNT        default 5
    MIN_SCORE          default 50 (posts below this score are skipped)
    REDDIT_TIME_RANGE  default "day" (hour|day|week|month|year)
    STATE_FILE         default state_reddit.json
    FORCE_POST         "true" — same as --force

Note on Reddit blocking: datacenter IPs (including GitHub Actions runners)
are sometimes blocked from the public JSON endpoints. The script tries three
hosts in turn and, if REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set, also
the official OAuth API. If everything fails the run fails loudly so the
backup run can retry.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import logging
import mimetypes
import os
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

SUBREDDITS = os.environ.get("SUBREDDITS", "science+space+astronomy").strip()
POSTS_COUNT = int(os.environ.get("POSTS_COUNT", "5"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "50"))
TIME_RANGE = os.environ.get("REDDIT_TIME_RANGE", "day").strip()
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

MAX_IMAGE_SIZE = 48 * 1024 * 1024

log = logging.getLogger("reddit-bot")


# --------------------------------------------------------------------------- #
#  Reddit fetching                                                              #
# --------------------------------------------------------------------------- #

def _listing_url(base: str) -> str:
    suffix = "" if base.startswith("https://api.reddit.com") else ".json"
    return f"{base}/r/{SUBREDDITS}/top{suffix}"


def fetch_reddit_listing() -> list:
    """Fetch the top listing; try public JSON hosts, then OAuth if configured."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"t": TIME_RANGE, "limit": "25"}
    last_err = None
    for base in REDDIT_HOSTS:
        try:
            resp = requests.get(_listing_url(base), headers=headers,
                                params=params, timeout=(20, 60))
            if resp.status_code == 200:
                children = (resp.json().get("data") or {}).get("children")
                if isinstance(children, list):
                    log.info("Reddit listing fetched from %s (%d posts)",
                             base, len(children))
                    return children
                last_err = "unexpected JSON structure"
            else:
                last_err = f"HTTP {resp.status_code}"
                log.warning("Reddit host %s returned %s", base, last_err)
        except (requests.RequestException, ValueError) as exc:
            last_err = str(exc)
            log.warning("Reddit host %s failed: %s", base, exc)

    # Official OAuth API as the last resort (needs an app id/secret)
    if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        try:
            auth = (REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
            tok = requests.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=auth,
                headers={"User-Agent": USER_AGENT},
                data={"grant_type": "client_credentials"},
                timeout=(20, 40),
            )
            tok.raise_for_status()
            bearer = tok.json().get("access_token")
            resp = requests.get(
                "https://oauth.reddit.com/r/{}/top".format(SUBREDDITS),
                headers={"User-Agent": USER_AGENT,
                         "Authorization": f"Bearer {bearer}"},
                params=params, timeout=(20, 60),
            )
            children = (resp.json().get("data") or {}).get("children")
            if isinstance(children, list):
                log.info("Reddit listing fetched via OAuth (%d posts)", len(children))
                return children
            last_err = f"OAuth listing HTTP {resp.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_err = f"OAuth failed: {exc}"

    raise RuntimeError(
        f"All Reddit endpoints failed (subreddits={SUBREDDITS!r}): {last_err}. "
        "Datacenter IPs are sometimes blocked — retry later, or set "
        "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET for the official OAuth API."
    )


def pick_posts(children: list, posted_ids: set) -> list:
    """Filter, de-duplicate and rank the raw listing; return the best posts."""
    picked, seen = [], set()
    for child in children:
        d = child.get("data", {}) if isinstance(child, dict) else {}
        if not d:
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
        "- summary_fa: 1-3 short Persian sentences summarizing the post (use the "
        "post text when present, otherwise the title); friendly scientific tone\n"
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
        lines.append(
            f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
            f"• r/{post['subreddit']}"
        )
        lines.append(f"🇬🇧 {post['title']}")
    else:
        lines.append(f"🇬🇧 {post['title']}")
        lines.append("")
        lines.append(
            f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
            f"• r/{post['subreddit']}"
        )
    if CHANNEL_SIGNATURE:
        lines.append("")
        lines.append(f"— {CHANNEL_SIGNATURE}")
    return "\n".join(lines)


def build_caption(post: dict, translation: dict) -> str:
    """Media caption: the full text if it fits, otherwise a compact version."""
    full = build_post_text(post, translation)
    if len(full) <= MAX_CAPTION_LEN:
        return full
    if translation:
        compact = "\n".join([
            "🔥 برترهای ردیت | Reddit Daily Top",
            f"🇮🇷 {translation['title_fa']}",
            "",
            f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
            f"• r/{post['subreddit']}",
        ])
    else:
        compact = "\n".join([
            "🔥 برترهای ردیت | Reddit Daily Top",
            f"🇬🇧 {post['title']}",
            "",
            f"⬆️ {humanize(post['score'])} • 💬 {humanize(post['comments'])} "
            f"• r/{post['subreddit']}",
        ])
    return compact[: MAX_CAPTION_LEN - 1] + "…" if len(compact) > MAX_CAPTION_LEN else compact


def download_preview(url: str, workdir: Path):
    """Download a Reddit preview image. Returns a path or None."""
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
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    lines = []
    for base in REDDIT_HOSTS:
        try:
            resp = requests.get(_listing_url(base), headers=headers,
                                params={"t": TIME_RANGE, "limit": "5"}, timeout=(15, 40))
            if resp.status_code == 200:
                n = len((resp.json().get("data") or {}).get("children") or [])
                lines.append(f"{base}: HTTP {resp.status_code}, children={n} — OK")
            else:
                lines.append(f"{base}: HTTP {resp.status_code} — BLOCKED/ERROR")
        except Exception as exc:
            lines.append(f"{base}: {exc}")
    if REDDIT_CLIENT_ID:
        lines.append("OAuth credentials present: yes")
    else:
        lines.append("OAuth credentials present: no (optional REDDIT_CLIENT_ID/SECRET)")
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

    log.info("Subreddits: %s | top %d of %s | min score %d",
             SUBREDDITS, POSTS_COUNT, TIME_RANGE, MIN_SCORE)

    force = args.force or os.environ.get("FORCE_POST", "").strip().lower() in ("1", "true", "yes", "on")
    posted_ids = set(load_state().get("posted_ids", [])) if not force else set()
    if posted_ids:
        log.info("Duplicate protection active: %d recently posted ids", len(posted_ids))

    children = fetch_reddit_listing()
    posts = pick_posts(children, posted_ids)[:POSTS_COUNT]
    if not posts:
        log.info("No new qualifying Reddit posts today — nothing to do.")
        return 0
    log.info("Selected %d posts: %s", len(posts),
             ", ".join(f"[{p['score']}] {p['title'][:40]}…" for p in posts))

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

        text = build_post_text(post, translation)
        if image is not None:
            send_media(image, build_caption(post, translation))
        else:
            for chunk in split_text(text):
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
