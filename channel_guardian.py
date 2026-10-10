#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
channel_guardian.py — نگهبان کانال (the unified channel app script)
=====================================================================

Guarantees that EVERY day ends with a healthy, duplicate-free channel:

  * Reddit  — exactly POSTS_COUNT (5) posts for the UTC day
  * NASA    — exactly one media post (photo/video) per APOD date

It does not trust the state files alone: it reads the channel's REAL
content through the public preview (t.me/s/<channel>) and cross-checks
both. Then it self-heals every gap it can:

  +-----------+---------------------------------------+------------------------+
  | Problem   | Detection                             | Self-healing action    |
  +-----------+---------------------------------------+------------------------+
  | missing   | channel + state both lack the post    | run the pipeline bot   |
  | duplicate | same title / same Jalali date twice   | delete the later copy  |
  | broken    | APOD post without photo/video         | delete + unmark state  |
  |           |                                       | + re-run the bot       |
  | stale     | state says posted, channel disagrees  | alert (exit 1 — never  |
  |           | after a completed re-check            | risks a duplicate)     |
  +-----------+---------------------------------------+------------------------+

Safety rules (the guardian itself can NEVER cause a duplicate):
  1. It never posts content directly — only via the state-protected bots.
  2. It deletes only STRONG duplicates (normalized-identical titles or the
     same Jalali date), never "similar" posts.
  3. After deleting K duplicate Reddit messages it decrements the state's
     posts_today by exactly K, so the top-up run replaces them 1:1.
  4. All three workflows share one concurrency group, so no other pipeline
     run can interleave with a guardian heal.

Exit codes: 0 = channel healthy (healed counts as healthy),
            1 = unresolved problem (GitHub then emails the repo owner),
            2 = configuration error.

Usage:
    python channel_guardian.py                 # full guard (post + verify)
    python channel_guardian.py --dry-run       # verify only, change nothing
    python channel_guardian.py --skip-reddit   # APOD-only (morning profile)
    python channel_guardian.py --diag          # connectivity + channel dump
"""

import argparse
import html as html_mod
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from persian_utils import pretty_date_fa
except ImportError:                                     # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from persian_utils import pretty_date_fa

# --------------------------------------------------------------------------- #
#  Configuration                                                                #
# --------------------------------------------------------------------------- #

REPO_DIR = Path(__file__).resolve().parent

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CHANNEL_USERNAME = (
    os.environ.get("CHANNEL_USERNAME", "").strip().lstrip("@")
    or TELEGRAM_CHAT_ID.strip().lstrip("@")
)
NASA_API_KEY = os.environ.get("NASA_API_KEY", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()      # optional alerts
APOD_API_URL = "https://api.nasa.gov/planetary/apod"

POSTS_COUNT = max(1, int(os.environ.get("POSTS_COUNT", "5")))
APOD_STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json").strip())
REDDIT_STATE_FILE = Path(os.environ.get("REDDIT_STATE_FILE", "state_reddit.json").strip())
if not APOD_STATE_FILE.is_absolute():
    APOD_STATE_FILE = REPO_DIR / APOD_STATE_FILE
if not REDDIT_STATE_FILE.is_absolute():
    REDDIT_STATE_FILE = REPO_DIR / REDDIT_STATE_FILE

ALLOW_DELETE = os.environ.get("GUARDIAN_DELETE", "true").strip().lower() in ("1", "true", "yes", "on")
MAX_PAGES = max(1, int(os.environ.get("GUARDIAN_MAX_PAGES", "4")))
SCAN_WINDOW_HOURS = 40          # channel look-back window (covers 2 APOD dates)
REDDIT_BOT = "reddit_top_bot.py"
APOD_BOT = "nasa_apod_bot.py"
REDDIT_BOT_TIMEOUT = int(os.environ.get("GUARDIAN_REDDIT_TIMEOUT", "2400"))   # 40 min
APOD_BOT_TIMEOUT = int(os.environ.get("GUARDIAN_APOD_TIMEOUT", "1500"))       # 25 min

# The evening Reddit slots end at 17:30 UTC; the quota is final afterwards.
REDDIT_CHECK_AFTER_UTC = 17 * 60 + 45
# The main APOD slot starts at 17:30 UTC; before that a missing APOD is normal.
APOD_DUE_AFTER_UTC = 17 * 60 + 25

PREVIEW_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

APOD_MARKER = "عکس نجومی روز ناسا"
REDDIT_MARK_RE = re.compile(r"\br/(?:science|space|astronomy)\b", re.IGNORECASE)

log = logging.getLogger("guardian")


# --------------------------------------------------------------------------- #
#  Channel preview parsing (pure — unit tested)                                 #
# --------------------------------------------------------------------------- #

@dataclass
class ChanMsg:
    id: int
    dt_utc: datetime | None
    text: str
    has_photo: bool = False
    has_video: bool = False

    @property
    def has_media(self) -> bool:
        return self.has_photo or self.has_video


def _clean_text(html_frag: str) -> str:
    """HTML fragment of a t.me message -> plain text, newlines preserved."""
    t = re.sub(r"<br\s*/?>", "\n", html_frag, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_mod.unescape(t)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in t.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def parse_channel_html(raw: str) -> list[ChanMsg]:
    """Parse a t.me/s/<channel> preview page into messages (newest last)."""
    blocks = re.split(r'(?=<div class="tgme_widget_message )', raw)
    msgs: list[ChanMsg] = []
    for block in blocks:
        mid = re.search(r'data-post="[^"]*/(\d+)"', block)
        if not mid:
            continue
        dt_utc = None
        dt_m = re.search(r'datetime="([^"]+)"', block)
        if dt_m:
            try:
                dt_utc = datetime.fromisoformat(dt_m.group(1))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_utc = dt_utc.astimezone(timezone.utc)
            except ValueError:
                dt_utc = None
        text_parts = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block, re.S)
        text = _clean_text(text_parts[0]) if text_parts else ""
        msgs.append(ChanMsg(
            id=int(mid.group(1)),
            dt_utc=dt_utc,
            text=text,
            has_photo="tgme_widget_message_photo" in block,
            has_video=("tgme_widget_message_video" in block
                       or "tgme_widget_message_animation" in block
                       or "tgme_widget_message_document" in block),
        ))
    msgs.sort(key=lambda m: m.id)
    return msgs


def classify(msg: ChanMsg) -> str:
    """'apod' | 'reddit' | 'other' — by stable markers inside the text."""
    if APOD_MARKER in msg.text:
        return "apod"
    if REDDIT_MARK_RE.search(msg.text):
        return "reddit"
    return "other"


# --- duplicate keys -------------------------------------------------------- #

_PERSIAN_FIX = str.maketrans({"ي": "ی", "ك": "ک", "أ": "ا", "إ": "ا",
                              "آ": "ا", "ة": "ه", "ؤ": "و", "ئ": "ی"})


def normalize_fa(s: str) -> str:
    """Canonical Persian form: fixed letters, no ZWNJ, no spaces, lowered."""
    s = (s or "").translate(_PERSIAN_FIX)
    s = s.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    s = re.sub(r"[^\w\u0600-\u06FF]+", "", s)     # keep letters/digits only
    return s.lower()


def reddit_title_key(text: str) -> str:
    """Normalized key of a Reddit post's title line (first text line)."""
    first = (text or "").strip().split("\n", 1)[0]
    return normalize_fa(first)


def title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of normalized word sets (near-dup hint, 0..1)."""
    wa = {normalize_fa(w) for w in re.split(r"[^\w\u0600-\u06FF]+", (a or "")) if w}
    wb = {normalize_fa(w) for w in re.split(r"[^\w\u0600-\u06FF]+", (b or "")) if w}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def apod_jalali_line(text: str) -> str | None:
    """Extract the '📅 <jalali date>' line from an APOD caption, if any."""
    for line in (text or "").split("\n"):
        line = line.strip()
        if line.startswith("📅"):
            return line[1:].strip()
    return None


def apod_matches_date(msg: ChanMsg, iso_date: str) -> bool:
    """True when an APOD-classified message is the post for `iso_date`."""
    pretty = pretty_date_fa(iso_date)
    if not pretty:
        return False
    line = apod_jalali_line(msg.text)
    return bool(line) and normalize_fa(line) == normalize_fa(pretty)


# --------------------------------------------------------------------------- #
#  Network: channel preview, Telegram deletes, NASA metadata                    #
# --------------------------------------------------------------------------- #

class GuardianError(RuntimeError):
    pass


def _preview_url(before: int | None = None) -> str:
    url = f"https://t.me/s/{CHANNEL_USERNAME}"
    if before:
        url += f"?before={before}"
    return url


def fetch_channel_page(before: int | None = None) -> str:
    last = None
    for attempt in range(3):
        try:
            resp = requests.get(_preview_url(before), headers={"User-Agent": PREVIEW_UA},
                                timeout=(10, 30))
            if resp.status_code == 200 and "tgme_widget_message" in resp.text:
                return resp.text
            last = GuardianError(f"preview HTTP {resp.status_code}")
        except requests.RequestException as exc:
            last = GuardianError(f"preview request failed: {exc}")
        time.sleep(5 * (attempt + 1))
    raise last or GuardianError("preview fetch failed")


def fetch_channel_messages(since_utc: datetime) -> tuple[list[ChanMsg], bool]:
    """
    Fetch channel messages covering `since_utc` .. now.

    Returns (messages, pagination_complete). Pagination is complete when we
    reached messages older than the window (or the channel start); a false
    value means the scan may be truncated (MAX_PAGES hit) and channel-based
    verdicts must be downgraded to state-based ones.
    """
    msgs: dict[int, ChanMsg] = {}
    complete = False
    before = None
    for _ in range(MAX_PAGES):
        raw = fetch_channel_page(before)
        page = parse_channel_html(raw)
        if not page:
            complete = True
            break
        for m in page:
            msgs.setdefault(m.id, m)
        oldest = page[0]
        if oldest.dt_utc and oldest.dt_utc <= since_utc:
            complete = True
            break
        before = oldest.id
    out = sorted(msgs.values(), key=lambda m: m.id)
    # keep only the window (plus a small margin) and anything unparsable-old
    out = [m for m in out if (m.dt_utc or datetime.now(timezone.utc)) >= since_utc - timedelta(hours=2)]
    return out, complete


def tg_delete_message(message_id: int) -> bool:
    """Delete one channel message via the bot (admin rights required)."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id},
            timeout=(10, 30),
        )
        body = resp.json()
        if body.get("ok"):
            return True
        log.warning("deleteMessage(%s) failed: %s", message_id, body.get("description"))
    except (requests.RequestException, ValueError) as exc:
        log.warning("deleteMessage(%s) request failed: %s", message_id, exc)
    return False


def fetch_apod_meta(iso_date: str) -> tuple[dict | None, str]:
    """
    Light APOD metadata probe.

    Returns (meta, status) with status:
      "ok"          — the APOD record for that date exists
      "future"      — NASA says the date is not (yet) published (HTTP 400/404)
      "unreachable" — API down / key invalid; verification must then rely on
                      channel + state only (never skip checks because of it)
    The personal key is tried first and DEMO_KEY as a fallback, because an
    invalid key alone must not disable the guardian.
    """
    keys = [k for k in (NASA_API_KEY, "DEMO_KEY") if k]
    seen = []
    for api_key in keys:
        if api_key in seen:
            continue
        seen.append(api_key)
        for attempt in range(2):
            try:
                resp = requests.get(APOD_API_URL,
                                    params={"api_key": api_key, "date": iso_date},
                                    timeout=(10, 30))
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and payload.get("date"):
                        return payload, "ok"
                    return None, "unreachable"
                if resp.status_code in (400, 404):     # genuinely unknown date
                    return None, "future"
                log.info("NASA API HTTP %s for %s (key %s..., attempt %d)",
                         resp.status_code, iso_date, api_key[:6], attempt + 1)
            except (requests.RequestException, ValueError) as exc:
                log.info("NASA API error for %s: %s", iso_date, exc)
            time.sleep(4)
    return None, "unreachable"


def _meta_is_placeholder(meta: dict) -> bool:
    """NASA's migration-era placeholder record (generic title, logo URLs)."""
    title = (meta.get("title") or "").strip().lower()
    urls = " ".join(x or "" for x in (meta.get("url"), meta.get("hdurl"))).lower()
    return title == "nasa science" or "nasa-logo" in urls or "/wp-content/themes/" in urls


def send_admin_alert(text: str) -> None:
    """Best-effort Telegram alert to the admin (only if ADMIN_CHAT_ID set)."""
    if not (ADMIN_CHAT_ID and TELEGRAM_BOT_TOKEN):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": ADMIN_CHAT_ID, "text": text},
                      timeout=(10, 30))
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------- #
#  State helpers (minimal, atomic, backward compatible)                         #
# --------------------------------------------------------------------------- #

def load_json_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def apod_state_dates(state: dict) -> set[str]:
    dates = {d for d in state.get("posted_dates", []) if isinstance(d, str)}
    if state.get("date"):
        dates.add(state["date"])
    return dates


def apod_unmark_date(iso_date: str) -> None:
    """Remove a date from the APOD state so the bot re-posts it (repair)."""
    state = load_json_state(APOD_STATE_FILE)
    dates = [d for d in state.get("posted_dates", []) if d != iso_date]
    changed = len(dates) != len(state.get("posted_dates", []))
    out = {"posted_dates": dates}
    if state.get("date") == iso_date:
        out["date"] = None
        out["title"] = state.get("title")
        out["posted_at_utc"] = state.get("posted_at_utc")
        changed = True
    else:
        out["date"] = state.get("date")
        out["title"] = state.get("title")
        out["posted_at_utc"] = state.get("posted_at_utc")
    if changed:
        save_json_state(APOD_STATE_FILE, out)
        log.info("APOD state: unmarked %s (will be re-posted)", iso_date)


def reddit_state_quota(state: dict, day_iso: str) -> int:
    if state.get("day") == day_iso:
        try:
            return int(state.get("posts_today") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def reddit_decrement_quota(day_iso: str, n: int) -> None:
    """After deleting n duplicate messages, make room for exactly n new posts."""
    if n <= 0:
        return
    state = load_json_state(REDDIT_STATE_FILE)
    if state.get("day") == day_iso:
        try:
            state["posts_today"] = max(0, int(state.get("posts_today") or 0) - n)
        except (TypeError, ValueError):
            state["posts_today"] = 0
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_json_state(REDDIT_STATE_FILE, state)
        log.info("Reddit state: posts_today decremented by %d (deleted duplicates)", n)


def run_bot(script_name: str, extra_args: list[str], timeout_s: int) -> bool:
    """Run one of the pipeline bots; stream its output; True on exit 0."""
    cmd = [sys.executable, script_name] + extra_args
    log.info(">>> running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_DIR), timeout=timeout_s,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ds", script_name, timeout_s)
        return False
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines()[-60:]:
        log.info("[bot] %s", line)
    if proc.returncode != 0:
        log.error("%s exited with code %s", script_name, proc.returncode)
        return False
    return True


# --------------------------------------------------------------------------- #
#  Duplicate analysis (pure — unit tested)                                      #
# --------------------------------------------------------------------------- #

def _title_line(msg: ChanMsg) -> str:
    return (msg.text or "").strip().split("\n", 1)[0]


def _subreddit_of(msg: ChanMsg) -> str:
    m = re.search(r"\br/([A-Za-z0-9_]+)", msg.text or "")
    return (m.group(1).lower() if m else "")


def analyze_reddit_duplicates(msgs: list[ChanMsg]) -> tuple[list[int], list[tuple[int, int, float]]]:
    """
    Strong duplicates (safe to auto-delete): later copies of a group with an
    identical normalized title — or a near-identical title (Jaccard ≥ 0.90)
    in the same subreddit. Near misses (0.70–0.90) are reported, never
    deleted, because different Reddit posts can legitimately look similar.
    """
    posts = [m for m in msgs if classify(m) == "reddit"]
    strong: list[int] = []
    groups: dict[str, list[ChanMsg]] = {}
    for m in posts:
        key = reddit_title_key(m.text)
        if key:
            groups.setdefault(key, []).append(m)
    for group in groups.values():
        if len(group) >= 2:
            group.sort(key=lambda m: m.id)
            strong.extend(m.id for m in group[1:])          # keep the earliest
    near: list[tuple[int, int, float]] = []
    strong_set = set(strong)
    for i in range(len(posts)):
        for j in range(i + 1, len(posts)):
            a, b = posts[i], posts[j]
            if a.id in strong_set or b.id in strong_set:
                continue
            if not (_subreddit_of(a) and _subreddit_of(a) == _subreddit_of(b)):
                continue
            sim = title_similarity(_title_line(a), _title_line(b))
            if sim >= 0.90:
                later = b if b.id > a.id else a
                strong.append(later.id)
                strong_set.add(later.id)
            elif sim >= 0.70:
                near.append((a.id, b.id, round(sim, 2)))
    return strong, near


def _apod_follower(msg: ChanMsg, by_id: dict[int, ChanMsg]) -> ChanMsg | None:
    """The explanation text message the APOD bot sends right after its media."""
    nxt = by_id.get(msg.id + 1)
    if nxt and classify(nxt) == "other" and not nxt.has_media and nxt.text:
        return nxt
    return None


def pick_apod_duplicates(posts: list[ChanMsg],
                         by_id: dict[int, ChanMsg]) -> tuple[int, list[int]]:
    """
    Which APOD message to keep when a date was posted twice, and everything
    that must be deleted. Preference: a COMPLETE pair (media + explanation
    follower) over a lone media message, then the earliest.
    """
    ranked = sorted(posts, key=lambda m: (0 if _apod_follower(m, by_id) else 1, m.id))
    keep = ranked[0]
    delete: list[int] = []
    for m in posts:
        if m.id == keep.id:
            continue
        delete.append(m.id)
        follower = _apod_follower(m, by_id)
        if follower:
            delete.append(follower.id)
    return keep.id, delete


# --------------------------------------------------------------------------- #
#  Guardian phases                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class PhaseReport:
    name: str
    notes: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class ChannelView:
    """Refreshable view of the channel's recent messages."""

    def __init__(self):
        self.since = datetime.now(timezone.utc) - timedelta(hours=SCAN_WINDOW_HOURS)
        self.msgs: list[ChanMsg] = []
        self.complete = False
        self.available = True

    def refresh(self):
        try:
            self.msgs, self.complete = fetch_channel_messages(self.since)
        except GuardianError as exc:
            log.warning("Channel preview unavailable: %s", exc)
            if not self.msgs:
                self.available = False

    def by_id(self) -> dict[int, ChanMsg]:
        return {m.id: m for m in self.msgs}

    def reddit_on(self, day_iso: str) -> list[ChanMsg]:
        return [m for m in self.msgs
                if classify(m) == "reddit" and m.dt_utc
                and m.dt_utc.strftime("%Y-%m-%d") == day_iso]

    def apod_for(self, iso_date: str) -> list[ChanMsg]:
        return [m for m in self.msgs
                if classify(m) == "apod" and apod_matches_date(m, iso_date)]

    def delete(self, message_ids: list[int]) -> int:
        deleted = 0
        for mid in message_ids:
            if tg_delete_message(mid):
                deleted += 1
                self.msgs = [m for m in self.msgs if m.id != mid]
                log.info("Deleted channel message %s", mid)
        return deleted


def phase_apod(now: datetime, view: ChannelView, dry: bool) -> PhaseReport:
    """Verify (and heal) the APOD posts for yesterday and today."""
    r = PhaseReport("APOD")
    today = now.date()
    check_dates = [today.isoformat(), (today - timedelta(days=1)).isoformat()]

    for d in check_dates:
        state = load_json_state(APOD_STATE_FILE)
        sdates = apod_state_dates(state)
        meta, status = fetch_apod_meta(d)
        if status == "future":
            r.notes.append(f"{d}: APOD not published yet — skipped")
            continue
        if status == "unreachable":
            r.notes.append(f"{d}: NASA API unreachable — verifying via "
                           f"channel + state only")
            meta = {}
        due = (d < today.isoformat()) or (
            d == today.isoformat()
            and (now.hour * 60 + now.minute) >= APOD_DUE_AFTER_UTC)

        posts = view.apod_for(d) if view.available else []
        if not posts and d not in sdates and not due:
            r.notes.append(f"{d}: not yet due — nothing to check yet")
            continue
        dry_reported = False

        # -- 1) duplicates of this date -------------------------------------
        if len(posts) > 1:
            keep_id, del_ids = pick_apod_duplicates(posts, view.by_id())
            if dry:
                r.healed.append(f"[dry] {d}: {len(posts)} duplicate APOD posts "
                                f"— would delete {del_ids}, keep {keep_id}")
                posts = [p for p in posts if p.id == keep_id]   # simulate
            elif not ALLOW_DELETE:
                r.problems.append(f"{d}: {len(posts)} duplicate APOD posts "
                                  f"(deletion disabled)")
            else:
                deleted = view.delete(del_ids)
                if deleted:
                    r.healed.append(f"{d}: removed {deleted} duplicate APOD "
                                    f"message(s) (kept {keep_id})")
                    time.sleep(3)
                    view.refresh()
                    posts = view.apod_for(d) if view.available else []

        # -- 2) broken post (no photo/video) --------------------------------
        if len(posts) == 1 and not posts[0].has_media:
            if dry:
                r.healed.append(f"[dry] {d}: broken text-only APOD {posts[0].id} "
                                f"— would delete and re-post with media")
                dry_reported = True
                posts = []                                        # simulate
            elif not ALLOW_DELETE:
                r.problems.append(f"{d}: APOD post {posts[0].id} has NO media "
                                  f"(deletion disabled)")
            else:
                view.delete([posts[0].id])
                apod_unmark_date(d)
                sdates.discard(d)                    # allow the re-post below
                r.healed.append(f"{d}: deleted broken text-only APOD "
                                f"{posts[0].id} — re-posting")
                time.sleep(3)
                view.refresh()
                posts = view.apod_for(d) if view.available else []

        # -- 3) missing → backfill via the bot -------------------------------
        if not posts and due and d not in sdates:
            if dry:
                r.healed.append(f"[dry] {d}: missing — would run "
                                f"{APOD_BOT} --date {d}")
                dry_reported = True
            else:
                if run_bot(APOD_BOT, ["--date", d], APOD_BOT_TIMEOUT):
                    time.sleep(20)                       # let t.me/s index it
                    view.refresh()
                    posts = view.apod_for(d) if view.available else []

        # -- 4) verdict -------------------------------------------------------
        if not posts:
            if dry_reported:
                continue                      # the [dry] heal entry says it all
            sdates_now = apod_state_dates(load_json_state(APOD_STATE_FILE))
            if d in sdates_now:
                r.notes.append(f"{d}: posted per state — channel preview "
                               f"lagging/degraded, accepted")
            elif due:
                r.problems.append(f"{d}: APOD missing and could not be posted")
            else:
                r.notes.append(f"{d}: not posted yet (evening run pending)")
            continue
        if len(posts) == 1:
            m = posts[0]
            follower = _apod_follower(m, view.by_id()) if view.available else None
            if m.has_media:
                detail = "media + explanation" if follower else "media (explanation text not visible in preview)"
                title_part = (f" — {meta.get('title', '')[:45]}"
                              if meta and not _meta_is_placeholder(meta) else "")
                r.notes.append(f"{d}: ✓ healthy ({detail}){title_part}")
            else:
                r.problems.append(f"{d}: APOD post {m.id} still has no media")
        else:
            r.problems.append(f"{d}: {len(posts)} APOD posts remain — manual review")
    return r


def phase_reddit(now: datetime, view: ChannelView, dry: bool) -> PhaseReport:
    """Verify (and heal) the day's Reddit-post quota and duplicates."""
    r = PhaseReport("Reddit")
    day = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    expected = POSTS_COUNT

    # window-wide duplicate scan (catches midnight-boundary repeats too)
    strong: list[int] = []
    near: list[tuple[int, int, float]] = []
    if view.available:
        strong, near = analyze_reddit_duplicates(view.msgs)
        if strong:
            if dry:
                r.healed.append(f"[dry] duplicates detected — would delete {strong}")
            elif not ALLOW_DELETE:
                r.problems.append(f"duplicate Reddit messages {strong} "
                                  f"(deletion disabled)")
            else:
                # quota fix on the day each deleted copy had been counted on
                per_day: dict[str, int] = {}
                for m in view.msgs:
                    if m.id in strong and m.dt_utc:
                        day_iso = m.dt_utc.strftime("%Y-%m-%d")
                        per_day[day_iso] = per_day.get(day_iso, 0) + 1
                deleted = view.delete(strong)
                if deleted:
                    for day_iso, n in per_day.items():
                        reddit_decrement_quota(day_iso, n)
                    r.healed.append(f"removed {deleted} duplicate Reddit "
                                    f"message(s) {strong}")
                    time.sleep(3)
                    view.refresh()
        for a, b, sim in near:
            r.notes.append(f"possible similar pair {a}/{b} (sim {sim}) — "
                           f"kept both, manual review suggested")

    state = load_json_state(REDDIT_STATE_FILE)
    quota = reddit_state_quota(state, day)
    count = len(view.reddit_on(day)) if view.available else -1   # -1 = unknown
    if dry and count >= 0:
        # simulate the deletions the real run would perform
        count -= sum(1 for m in view.reddit_on(day) if m.id in set(strong))

    if 0 <= count < expected:
        if quota < expected:
            if dry:
                r.healed.append(f"[dry] {count}/{expected} today — would run "
                                f"{REDDIT_BOT} to top up")
                count = expected                          # simulate success
            else:
                if run_bot(REDDIT_BOT, [], REDDIT_BOT_TIMEOUT):
                    time.sleep(20)
                    view.refresh()
                    count = len(view.reddit_on(day)) if view.available else -1
                    quota = reddit_state_quota(load_json_state(REDDIT_STATE_FILE), day)
        if 0 <= count < expected:
            if quota >= expected:
                r.notes.append(f"{count}/{expected} in preview, state says "
                               f"{quota}/{expected} — preview lag/degraded, accepted")
            else:
                r.problems.append(f"only {max(count, 0)}/{expected} Reddit posts today "
                                  f"— top-up failed")
        elif not dry:
            r.healed.append(f"topped up to {count}/{expected} Reddit posts")
    elif count > expected:
        r.problems.append(f"{count} Reddit posts today (> {expected}) without "
                          f"exact duplicates — manual review")
    elif count == expected:
        r.notes.append(f"✓ {count}/{expected} posts today, no duplicates")
    else:                                            # channel unavailable
        if quota >= expected:
            r.notes.append(f"state says {quota}/{expected} — channel preview "
                           f"unavailable, accepted")
        else:
            if dry:
                r.healed.append(f"[dry] state {quota}/{expected} — would run "
                                f"{REDDIT_BOT}")
            elif run_bot(REDDIT_BOT, [], REDDIT_BOT_TIMEOUT):
                quota = reddit_state_quota(load_json_state(REDDIT_STATE_FILE), day)
                if quota >= expected:
                    r.healed.append(f"topped up to {quota}/{expected} (state)")
                else:
                    r.problems.append(f"top-up failed ({quota}/{expected})")
            else:
                r.problems.append(f"top-up failed ({quota}/{expected})")

    if view.available:
        ycount = len(view.reddit_on(yesterday))
        if ycount < expected:
            r.notes.append(f"yesterday ended {ycount}/{expected} — today's runs "
                           f"pick the missed posts up from the mirror window")
    return r


# --------------------------------------------------------------------------- #
#  Report / main                                                                #
# --------------------------------------------------------------------------- #

def _tehran(now: datetime) -> str:
    return (now + timedelta(hours=3, minutes=30)).strftime("%H:%M")


def print_report(now: datetime, reports: list[PhaseReport]) -> list[str]:
    problems: list[str] = []
    lines = ["", "=" * 64,
             f"🛡  گزارش نگهبان کانال — {now.strftime('%Y-%m-%d %H:%M')} UTC "
             f"({_tehran(now)} تهران)",
             "=" * 64]
    for rep in reports:
        lines.append(f"\n[{rep.name}]")
        for h in rep.healed:
            lines.append(f"  🔧 اصلاح: {h}")
        for n in rep.notes:
            lines.append(f"  • {n}")
        for p in rep.problems:
            lines.append(f"  ⚠️ مشکل: {p}")
            problems.append(f"[{rep.name}] {p}")
    lines.append("\n" + "=" * 64)
    if problems:
        lines.append("❌ نتیجه: مشکل حل‌نشده باقی ماند — برای اطلاع‌رسانی ایمیل "
                     "GitHub Actions فعال است.")
    else:
        lines.append("✅ نتیجه: همهٔ پست‌های ناسا و ردیت سالم، مرتب و بدون "
                     "تکراری هستند.")
    print("\n".join(lines))
    return problems


def diag() -> int:
    print("=== channel preview ===")
    print("channel:", CHANNEL_USERNAME or "(unset)")
    view = ChannelView()
    view.refresh()
    if not view.available:
        print("preview: UNAVAILABLE")
    else:
        print(f"preview: {len(view.msgs)} messages, pagination complete={view.complete}")
        for m in view.msgs:
            media = "P" if m.has_photo else ("V" if m.has_video else "-")
            when = m.dt_utc.strftime("%m-%d %H:%M") if m.dt_utc else "?"
            print(f"  {m.id:>4} {when} [{classify(m):6}] {media} "
                  f"{_title_line(m)[:70]}")
    print("\n=== NASA API ===")
    meta, status = fetch_apod_meta(datetime.now(timezone.utc).date().isoformat())
    print("today:", (meta or {}).get("date"), "—",
          (meta or {}).get("title", f"{status}")[:60])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Guardian: verify + self-heal the channel's NASA & Reddit posts.")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify only — no deletes, no bot runs, no state writes")
    parser.add_argument("--skip-reddit", action="store_true",
                        help="do not check the Reddit quota (APOD-only run)")
    parser.add_argument("--skip-apod", action="store_true",
                        help="do not check APOD posts (Reddit-only run)")
    parser.add_argument("--force-reddit-check", action="store_true",
                        help="check the Reddit quota even before the evening slots end")
    parser.add_argument("--no-delete", action="store_true",
                        help="never delete messages (report duplicates only)")
    parser.add_argument("--diag", action="store_true",
                        help="connectivity diagnostics + channel dump, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")

    if args.diag:
        return diag()

    if args.no_delete:
        global ALLOW_DELETE
        ALLOW_DELETE = False

    if not CHANNEL_USERNAME:
        log.error("No channel configured (CHANNEL_USERNAME / TELEGRAM_CHAT_ID)")
        return 2

    now = datetime.now(timezone.utc)
    log.info("Guardian start — channel @%s, dry_run=%s", CHANNEL_USERNAME, args.dry_run)

    view = ChannelView()
    view.refresh()
    if not view.available:
        log.warning("Channel preview unavailable — falling back to state-based "
                    "verification only (duplicate detection disabled)")

    reports: list[PhaseReport] = []

    if not args.skip_apod:
        reports.append(phase_apod(now, view, args.dry_run))
    else:
        reports.append(PhaseReport("APOD", notes=["skipped by flag"]))

    minutes = now.hour * 60 + now.minute
    if args.skip_reddit:
        reports.append(PhaseReport("Reddit", notes=["skipped by flag"]))
    elif minutes < REDDIT_CHECK_AFTER_UTC and not args.force_reddit_check:
        reports.append(PhaseReport("Reddit", notes=[
            f"quota not final before {REDDIT_CHECK_AFTER_UTC // 60:02d}:"
            f"{REDDIT_CHECK_AFTER_UTC % 60:02d} UTC — check skipped"]))
    else:
        reports.append(phase_reddit(now, view, args.dry_run))

    problems = print_report(now, reports)
    if problems and not args.dry_run:
        send_admin_alert("🛡 نگهبان کانال:\n" + "\n".join(problems[:10]))
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
