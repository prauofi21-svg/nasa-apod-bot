#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NASA APOD -> Telegram channel automation.

Fetches NASA's Astronomy Picture of the Day (image or video) and posts it to
a Telegram channel with a clean English caption (title, date, explanation,
credit) — with no links inside the post.

Designed to run for free on GitHub Actions, with de-duplication via a state
file so an APOD is never posted twice for the same day.

Command line:
    python nasa_apod_bot.py                post today's APOD to the channel
    python nasa_apod_bot.py --dry-run      build the post locally, do not send
    python nasa_apod_bot.py --force        re-post even if already posted today
    python nasa_apod_bot.py --detect-chat  show chat IDs visible to your bot

Required environment variables (stored as GitHub Secrets):
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     channel chat id (looks like -1001234567890)
    NASA_API_KEY         personal key from https://api.nasa.gov/

Optional environment variables:
    CHANNEL_SIGNATURE       footer text, e.g. "@YourChannel" (default: empty)
    SEND_VIDEO_IF_POSSIBLE  "true" (default) / "false" — upload the real video
                            file on video days (falls back to preview image)
    STATE_FILE              de-duplication state path (default: state.json)
    FORCE_POST              "true" — same as the --force flag
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    import yt_dlp  # optional — only needed on video days
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

try:
    from llm_translator import translate_fields
    TRANSLATION_AVAILABLE = True
except ImportError:
    TRANSLATION_AVAILABLE = False

# --------------------------------------------------------------------------- #
#  Configuration                                                               #
# --------------------------------------------------------------------------- #

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NASA_API_KEY = os.environ.get("NASA_API_KEY", "").strip()

CHANNEL_SIGNATURE = os.environ.get("CHANNEL_SIGNATURE", "").strip()
SEND_VIDEO_IF_POSSIBLE = os.environ.get(
    "SEND_VIDEO_IF_POSSIBLE", "true"
).strip().lower() in ("1", "true", "yes", "on")
STATE_FILE = os.environ.get("STATE_FILE", "state.json").strip()

APOD_API_URL = "https://api.nasa.gov/planetary/apod"

MAX_CAPTION_LEN = 1024               # Telegram photo/video caption limit
MAX_MESSAGE_LEN = 4096               # Telegram text message limit
MAX_FILE_SIZE = 48 * 1024 * 1024     # stay under Telegram's 50 MB upload limit

HTTP_TIMEOUT = (30, 60)              # (connect, read) seconds
UPLOAD_TIMEOUT = (30, 300)           # file uploads get a longer window
NASA_RETRIES = 12                    # legacy cap — fetch_apod uses its own cycle logic
TG_RETRIES = 5

log = logging.getLogger("apod-bot")


# --------------------------------------------------------------------------- #
#  Post text builders                                                          #
# --------------------------------------------------------------------------- #

def pretty_date(iso_date: str) -> str:
    """'2026-09-18' -> 'September 18, 2026'."""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt:%B} {dt.day}, {dt.year}"
    except (TypeError, ValueError):
        return iso_date or ""


def build_header(apod: dict, video_preview: bool = False) -> str:
    """Compact header used as the media caption on long posts."""
    lines = [
        "🌌 Astronomy Picture of the Day",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✨ {(apod.get('title') or 'Untitled').strip()}",
        f"📅 {pretty_date(apod.get('date', ''))}",
    ]
    credit = " ".join((apod.get("copyright") or "").split())
    if credit:
        lines.append(f"🔭 Credit: {credit}")
    if video_preview:
        lines.append("🎞 Today's APOD is a video — preview image shown.")
    header = "\n".join(lines)
    if len(header) > MAX_CAPTION_LEN:
        header = header[: MAX_CAPTION_LEN - 1] + "…"
    return header


def normalize_explanation(text: str) -> str:
    """
    Tidy NASA's explanation text: NASA separates paragraphs with runs of
    spaces — turn those into real paragraph breaks for nicer rendering.
    """
    text = (text or "").strip()
    text = re.sub(r"[ \t]{3,}", "\n\n", text)   # 3+ spaces -> paragraph break
    text = re.sub(r"[ \t]{2,}", " ", text)      # leftover double spaces
    text = re.sub(r"\n{3,}", "\n\n", text)      # too many blank lines
    return text.strip()


def build_full_post(apod: dict, video_preview: bool = False) -> str:
    """The complete post text (header + explanation + optional signature)."""
    parts = [build_header(apod, video_preview)]
    explanation = normalize_explanation(apod.get("explanation"))
    if explanation:
        parts.append(explanation)
    if CHANNEL_SIGNATURE:
        parts.append(f"— {CHANNEL_SIGNATURE}")
    return "\n\n".join(parts)


def build_bilingual_header(apod: dict, translation: dict, video_preview: bool = False) -> str:
    """Bilingual (Persian + English) caption for the media post."""
    lines = [
        "🌌 عکس نجومی روز ناسا | Astronomy Picture of the Day",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✨ {translation['title_fa']}",
        f"✨ {(apod.get('title') or 'Untitled').strip()}",
        f"📅 {pretty_date(apod.get('date', ''))}",
    ]
    credit = " ".join((apod.get("copyright") or "").split())
    if credit:
        lines.append(f"🔭 Credit: {credit}")
    if video_preview:
        lines.append("🎞 امروز APOD یک ویدئو است — تصویر پیش‌نمایش | video day")
    header = "\n".join(lines)
    if len(header) > MAX_CAPTION_LEN:
        header = header[: MAX_CAPTION_LEN - 1] + "…"
    return header


def translate_apod(apod: dict):
    """
    Translate the APOD title + explanation into engaging Persian via the LLM.
    Returns {'title_fa': ..., 'explanation_fa': ...} or None on any failure
    (callers fall back to the English-only post).
    """
    if not TRANSLATION_AVAILABLE:
        return None
    title = (apod.get("title") or "").strip()
    explanation = normalize_explanation(apod.get("explanation"))
    prompt = (
        "Translate this NASA Astronomy Picture of the Day into engaging, natural "
        "Persian (Farsi) for a Telegram science channel.\n\n"
        f"Title: {title}\n\n"
        f"Explanation:\n{explanation}\n\n"
        "Rules:\n"
        "- title_fa: an attractive, faithful Persian translation of the title\n"
        "- explanation_fa: an engaging Persian translation of the explanation; "
        "accurate, 2-4 short paragraphs, friendly scientific tone, easy to read "
        "on a phone\n"
        "- No links, no hashtags, no markdown symbols; keep proper names in Latin "
        "where that is more natural; use Persian numerals where natural\n"
        'Respond ONLY as JSON: {"title_fa": "...", "explanation_fa": "..."}'
    )
    return translate_fields(prompt, ("title_fa", "explanation_fa"))


def split_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list:
    """Split long text into Telegram-sized chunks, preferring blank lines."""
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


# --------------------------------------------------------------------------- #
#  NASA API                                                                    #
# --------------------------------------------------------------------------- #

def fetch_apod() -> dict:
    """
    Fetch the latest available APOD.

    NASA's API returns HTTP 500 for the "today" query when the current day's
    entry has not been published yet (typically in the hours before the new
    APOD goes live) or during short outages. We therefore:
      1. try "today" (no date param), and
      2. if it keeps failing, walk back up to 3 previous days and use the
         most recent entry NASA can serve,
    repeating the whole cycle a few times so transient outages also heal.
    The returned entry's own `date` field drives duplicate protection, so a
    fallback entry that was already posted is never posted twice.
    """
    api_key = NASA_API_KEY or "DEMO_KEY"
    today = datetime.now(timezone.utc).date()
    candidates = [None] + [(today - timedelta(days=n)).isoformat() for n in range(1, 4)]
    last_error = None
    for cycle in range(1, 4):
        for cand in candidates:
            for _ in range(2):  # two quick tries per candidate
                params = {"api_key": api_key, "thumbs": "true"}
                if cand:
                    params["date"] = cand
                try:
                    resp = requests.get(APOD_API_URL, params=params, timeout=HTTP_TIMEOUT)
                    try:
                        payload = resp.json()
                    except ValueError:
                        payload = None
                    if resp.status_code == 200 and isinstance(payload, dict) and payload.get("url"):
                        if cand:
                            log.warning(
                                "Latest APOD unavailable — using the %s entry instead", cand
                            )
                        return payload
                    if resp.status_code == 429:
                        log.warning("NASA API rate limit — waiting 30 s")
                        time.sleep(30)
                        continue
                    desc = (payload or {}).get("error") or (payload or {}).get("msg") or f"HTTP {resp.status_code}"
                    last_error = RuntimeError(f"NASA API error: {desc}")
                except requests.RequestException as exc:
                    last_error = exc
                time.sleep(5)
        if cycle < 3:
            pause = 60 * cycle
            log.warning(
                "APOD fetch cycle %d/3 failed (%s) — retrying in %d s",
                cycle, last_error, pause,
            )
            time.sleep(pause)
    raise RuntimeError(f"Could not fetch APOD from NASA API: {last_error}")


def validate_apod(apod: dict) -> None:
    """Make sure the response contains everything we need for a valid post."""
    if apod.get("media_type") not in ("image", "video"):
        raise RuntimeError(f"Unexpected APOD media_type: {apod.get('media_type')!r}")
    if not apod.get("url"):
        raise RuntimeError("APOD response has no media url")
    if not apod.get("title"):
        raise RuntimeError("APOD response has no title")


# --------------------------------------------------------------------------- #
#  Media downloaders                                                           #
# --------------------------------------------------------------------------- #

def download_images(apod: dict, workdir: Path) -> list:
    """Download all APOD image variants (HD first, SD as backup)."""
    candidates = []
    for key in ("hdurl", "url"):
        value = apod.get(key)
        if value and value not in candidates:
            candidates.append(value)
    paths = []
    for idx, url in enumerate(candidates):
        try:
            log.info("Downloading image: %s", url)
            resp = requests.get(url, timeout=(30, 180))
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith("image/"):
                log.warning("Skipping non-image Content-Type %r", content_type)
                continue
            data = resp.content
            if len(data) < 10_000:
                log.warning("File too small (%d bytes) — skipping", len(data))
                continue
            if len(data) > MAX_FILE_SIZE:
                log.warning("File too large (%.1f MB) — trying next candidate", len(data) / 1e6)
                continue
            ext = Path(urlparse(url).path).suffix or mimetypes.guess_extension(content_type) or ".jpg"
            path = workdir / f"apod_image_{idx}{ext}"
            path.write_bytes(data)
            log.info("Image saved (%.1f MB)", len(data) / 1e6)
            paths.append(path)
        except requests.RequestException as exc:
            log.warning("Image download failed (%s)", exc)
    return paths


def shrink_for_telegram(path: Path):
    """
    Re-encode a large or oversized image for a Telegram *photo* upload.

    Telegram limits photos to 10 MB AND to sane pixel dimensions
    (width + height must stay small; huge panoramas are rejected with
    PHOTO_INVALID_DIMENSIONS). We cap the longest side at 4000 px — far
    above Telegram's own display resolution — and step quality down until
    the file fits ~9.7 MB.
    """
    import io

    size_limit = 10 * 1024 * 1024 - 300 * 1024   # 9.7 MB with safety margin
    max_side = 4000
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow not available — cannot prepare the image for photo upload")
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            width, height = im.size
            dim_scale = min(1.0, max_side / max(width, height))
            if path.stat().st_size <= size_limit and dim_scale >= 1.0:
                return path
            for rel_scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
                scale = dim_scale * rel_scale
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                frame = im if new_size == (width, height) else im.resize(new_size, Image.LANCZOS)
                for quality in (90, 85, 80, 74, 68, 62):
                    buf = io.BytesIO()
                    frame.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
                    if buf.tell() <= size_limit:
                        out = path.with_name("apod_photo.jpg")
                        out.write_bytes(buf.getvalue())
                        log.info(
                            "Image re-encoded to %.1f MB, %dx%d (scale %.2f, quality %d)",
                            buf.tell() / 1e6, new_size[0], new_size[1], scale, quality,
                        )
                        return out
    except Exception as exc:
        log.warning("Image shrinking failed (%s)", exc)
    return None


def select_image(paths: list):
    """
    Pick the best image for a Telegram *photo* post.
    1. If a big variant exists, try to shrink it (keeps the most detail).
    2. Otherwise use the largest variant that already fits the photo limit.
    3. As a last resort return the biggest file (sent as a document).
    """
    photo_limit = 10 * 1024 * 1024 - 300 * 1024
    if not paths:
        return None
    oversize = [p for p in paths if p.stat().st_size > photo_limit]
    if oversize:
        shrunk = shrink_for_telegram(max(oversize, key=lambda p: p.stat().st_size))
        if shrunk:
            return shrunk
    fitting = [p for p in paths if p.stat().st_size <= photo_limit]
    if fitting:
        # Even a small file can have oversized pixel dimensions — run it
        # through the dimension capper as well.
        for p in sorted(fitting, key=lambda p: p.stat().st_size, reverse=True):
            prepared = shrink_for_telegram(p)
            if prepared:
                return prepared
    return max(paths, key=lambda p: p.stat().st_size)


def download_video(url: str, workdir: Path):
    """Try to download the actual video file (<=480p mp4, <=48 MB)."""
    if not SEND_VIDEO_IF_POSSIBLE:
        log.info("SEND_VIDEO_IF_POSSIBLE=false — skipping the video file")
        return None
    if not YTDLP_AVAILABLE:
        log.warning("yt-dlp is not installed — will fall back to the preview image")
        return None
    log.info("Trying to download video: %s", url)
    opts = {
        "format": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[ext=mp4]/b",
        "outtmpl": str(workdir / "apod_video.%(ext)s"),
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 2,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # yt-dlp raises many exception types
        log.warning("Video download failed (%s) — will fall back to the preview image", exc)
        return None
    for candidate in sorted(workdir.glob("apod_video.*")):
        if candidate.suffix.lower() != ".mp4":
            continue
        size = candidate.stat().st_size
        if 100_000 < size <= MAX_FILE_SIZE:
            log.info("Video saved (%.1f MB)", size / 1e6)
            return candidate
        log.warning("Downloaded video is %.1f MB — unusable, discarding", size / 1e6)
        candidate.unlink(missing_ok=True)
    return None


def download_thumbnail(apod: dict, workdir: Path):
    """Download a preview image for video days. Returns path/None."""
    candidates = []
    thumb = apod.get("thumbnail_url")
    if thumb:
        candidates.append(thumb)
    match = re.search(
        r"(?:youtube\.com/(?:embed/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{6,})",
        apod.get("url", "") or "",
    )
    if match:
        vid = match.group(1)
        candidates.append(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
        candidates.append(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    for url in candidates:
        try:
            resp = requests.get(url, timeout=(30, 60))
            if resp.status_code != 200:
                continue
            data = resp.content
            if len(data) < 15_000:  # YouTube placeholder images are tiny
                continue
            path = workdir / "apod_thumb.jpg"
            path.write_bytes(data)
            log.info("Video preview image saved")
            return path
        except requests.RequestException:
            continue
    return None


def prepare_media(apod: dict):
    """Return (media_path_or_None, is_video_preview)."""
    workdir = Path(tempfile.mkdtemp(prefix="apod_"))
    if apod.get("media_type") == "video":
        video = download_video(apod.get("url", ""), workdir)
        if video:
            return video, False
        thumb = download_thumbnail(apod, workdir)
        if thumb:
            return thumb, True
        return None, True
    image = select_image(download_images(apod, workdir))
    if image:
        return image, False
    return None, False


# --------------------------------------------------------------------------- #
#  Telegram API                                                                #
# --------------------------------------------------------------------------- #

def tg_api(method: str, data: dict = None, files: dict = None, timeout=HTTP_TIMEOUT) -> dict:
    """Call the Telegram Bot API with retries and rate-limit handling."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last_error = None
    for attempt in range(1, TG_RETRIES + 1):
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
            try:
                payload = resp.json()
            except ValueError:
                payload = {"ok": False, "description": resp.text[:300]}
            if resp.status_code == 429:
                wait = int(payload.get("parameters", {}).get("retry_after", 5)) + 1
                log.warning("Telegram rate limit — waiting %d s", wait)
                time.sleep(min(wait, 90))
                continue
            if payload.get("ok"):
                return payload
            last_error = RuntimeError(f"Telegram {method}: {payload.get('description')}")
            if 400 <= resp.status_code < 500:
                # A 4xx error (bad token, wrong chat id, bad format) will not
                # fix itself by retrying — raise immediately.
                raise last_error
        except requests.RequestException as exc:
            last_error = exc
        if attempt < TG_RETRIES:
            delay = 2 ** attempt
            log.warning(
                "Telegram %s attempt %d/%d failed (%s) — retrying in %d s",
                method, attempt, TG_RETRIES, last_error, delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"Telegram {method} failed after {TG_RETRIES} attempts: {last_error}")


def send_message(text: str) -> None:
    """Send a (possibly long) text message, split into chunks if needed."""
    for chunk in split_text(text):
        tg_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        time.sleep(1)  # keep message ordering


def send_media(path: Path, caption: str) -> None:
    """Send a photo/video with caption; documents as a fallback format."""
    is_video = path.suffix.lower() == ".mp4"
    method, field = ("sendVideo", "video") if is_video else ("sendPhoto", "photo")
    mime = mimetypes.guess_type(str(path))[0] or ("video/mp4" if is_video else "image/jpeg")
    try:
        with open(path, "rb") as fh:
            tg_api(
                method,
                {"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={field: (path.name, fh, mime)},
                timeout=UPLOAD_TIMEOUT,
            )
            return
    except Exception as exc:
        if is_video:
            raise
        log.warning("sendPhoto failed (%s) — retrying as document", exc)
    with open(path, "rb") as fh:
        tg_api(
            "sendDocument",
            {"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"document": (path.name, fh, mime)},
            timeout=UPLOAD_TIMEOUT,
        )


def detect_chat() -> int:
    """Helper mode: list chats the bot can see, to find the channel chat_id."""
    payload = tg_api("getUpdates", {"timeout": 0})
    chats = {}
    for update in payload.get("result", []):
        for key in ("channel_post", "edited_channel_post", "message",
                    "edited_message", "my_chat_member"):
            entry = update.get(key)
            if not entry:
                continue
            chat = entry.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            chats[chat_id] = (chat.get("type", "?"), name)
    if not chats:
        print("\nNo chats found yet. Do this, then run again:")
        print("  1. Add your bot as an ADMIN of the channel.")
        print("  2. Post any message inside the channel.")
        print("  3. Run:  python nasa_apod_bot.py --detect-chat")
        return 1
    print("\nChats visible to your bot:")
    for chat_id, (kind, name) in sorted(chats.items()):
        print(f"  chat_id: {chat_id:<16}  type: {kind:<8}  name: {name}")
    print("\nYour channel's chat_id usually looks like -100XXXXXXXXXX.")
    print("Put it in the TELEGRAM_CHAT_ID secret.")
    return 0


# --------------------------------------------------------------------------- #
#  State (duplicate protection)                                                #
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(apod: dict) -> None:
    Path(STATE_FILE).write_text(
        json.dumps(
            {
                "date": apod.get("date"),
                "title": apod.get("title"),
                "posted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
#  Publishing                                                                  #
# --------------------------------------------------------------------------- #

def publish(apod: dict, media_path, video_preview: bool, translation: dict = None) -> None:
    """
    Post the APOD to the channel.

    With a Persian translation available (recommended):
      - media post with a bilingual caption (fa+en title, date, credit)
      - follow-up message with the full Persian translation
      - follow-up message with the English original
    Without a translation: the classic English-only layout.
    """
    if translation:
        header = build_bilingual_header(apod, translation, video_preview)
        if media_path is not None:
            send_media(media_path, header)
        else:
            log.warning("No media available — sending the post as text only")
            send_message(header)
        time.sleep(1)
        fa_text = "🇮🇷 ترجمه فارسی:\n\n" + translation["explanation_fa"]
        if CHANNEL_SIGNATURE:
            fa_text += f"\n\n— {CHANNEL_SIGNATURE}"
        send_message(fa_text)
        time.sleep(1)
        en_explanation = normalize_explanation(apod.get("explanation"))
        if en_explanation:
            send_message("🌍 English original:\n\n" + en_explanation)
        return

    full_post = build_full_post(apod, video_preview)
    if media_path is None:
        log.warning("No media available — sending the post as text only")
        send_message(full_post)
        return
    if len(full_post) <= MAX_CAPTION_LEN:
        send_media(media_path, full_post)
        return
    log.info("Post is longer than %d chars — splitting into media + text", MAX_CAPTION_LEN)
    send_media(media_path, build_header(apod, video_preview))
    time.sleep(1)
    explanation = normalize_explanation(apod.get("explanation"))
    if CHANNEL_SIGNATURE:
        explanation = f"{explanation}\n\n— {CHANNEL_SIGNATURE}" if explanation else f"— {CHANNEL_SIGNATURE}"
    if explanation:
        send_message(explanation)


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post NASA's Astronomy Picture of the Day to a Telegram channel."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="build the post locally without sending it")
    parser.add_argument("--force", action="store_true",
                        help="post even if this APOD was already posted")
    parser.add_argument("--detect-chat", action="store_true",
                        help="list chat IDs visible to the bot, then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.detect_chat:
        return detect_chat()

    if not NASA_API_KEY:
        log.warning("NASA_API_KEY is not set — falling back to DEMO_KEY (heavily rate-limited)")

    if not args.dry_run:
        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
            ) if not value
        ]
        if missing:
            log.error("Missing environment variables: %s", ", ".join(missing))
            return 2

    apod = fetch_apod()
    validate_apod(apod)
    log.info("APOD fetched: %s — %s", apod.get("date"), apod.get("title"))

    force = args.force or os.environ.get("FORCE_POST", "").strip().lower() in ("1", "true", "yes", "on")
    if not args.dry_run and not force:
        state = load_state()
        if state.get("date") == apod.get("date"):
            log.info("APOD for %s was already posted — nothing to do.", apod.get("date"))
            return 0

    media_path, video_preview = prepare_media(apod)
    translation = translate_apod(apod) if TRANSLATION_AVAILABLE else None
    if translation:
        log.info("Persian translation ready (%d chars)", len(translation["explanation_fa"]))
    else:
        log.info("No Persian translation — posting English-only")

    if args.dry_run:
        print("\n" + "=" * 62)
        print("POST PREVIEW (dry run — nothing was sent)")
        print("=" * 62)
        if translation:
            print(build_bilingual_header(apod, translation, video_preview))
            print("\n🇮🇷 ترجمه فارسی:\n\n" + translation["explanation_fa"])
            en_explanation = normalize_explanation(apod.get("explanation"))
            if en_explanation:
                print("\n🌍 English original:\n\n" + en_explanation)
        else:
            print(build_full_post(apod, video_preview))
        print("=" * 62)
        if media_path:
            target = Path("apod_dry_run" + media_path.suffix)
            target.write_bytes(media_path.read_bytes())
            print(f"Media saved locally to: {target.resolve()}")
        else:
            print("No media could be downloaded (the post would be text-only).")
        return 0

    publish(apod, media_path, video_preview, translation)
    save_state(apod)
    log.info("Posted APOD for %s successfully.", apod.get("date"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        log.error("FAILED: %s", exc)
        sys.exit(1)
