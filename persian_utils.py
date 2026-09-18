#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared Persian (Farsi) text helpers for the Telegram channel bots.

Provides:
    to_fa_digits(s)        '1234'            -> '۱۲۳۴'
    humanize_fa(n)         11868             -> '۱۱٫۹ هزار'
    gregorian_to_jalali()  (2026, 9, 18)     -> (1405, 6, 27)
    pretty_date_fa()       '2026-09-18'      -> 'جمعه ۲۷ شهریور ۱۴۰۵'
    pick_emoji(text, dflt) pick the first emoji in an LLM reply
    parse_hashtags(text, dflt, n)
                           validate Persian hashtag tokens from an LLM reply
"""

from __future__ import annotations

import re
from datetime import datetime

# --------------------------------------------------------------------------- #
#  Digits                                                                      #
# --------------------------------------------------------------------------- #

_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(value) -> str:
    """Convert ASCII digits to Persian digits."""
    return str(value).translate(_DIGIT_MAP)


def humanize_fa(n) -> str:
    """
    Human-friendly Persian count: 11868 -> '۱۱٫۹ هزار', 234 -> '۲۳۴',
    2500000 -> '۲٫۵ میلیون'. Uses the Persian decimal separator (٫).
    """
    n = int(n)
    if n >= 1_000_000:
        s = f"{n / 1e6:.1f}".rstrip("0").rstrip(".").replace(".", "٫")
        return to_fa_digits(s) + " میلیون"
    if n >= 1_000:
        s = f"{n / 1e3:.1f}".rstrip("0").rstrip(".").replace(".", "٫")
        return to_fa_digits(s) + " هزار"
    return to_fa_digits(n)


# --------------------------------------------------------------------------- #
#  Jalali (Solar Hijri) calendar                                               #
# --------------------------------------------------------------------------- #

JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

# indexed by Python's weekday() (Monday = 0)
WEEKDAYS_FA = [
    "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه",
]


def gregorian_to_jalali(gy: int, gm: int, gd: int):
    """Convert a Gregorian date to the Jalali (Solar Hijri) calendar."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
            + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1])
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def pretty_date_fa(iso_date: str) -> str:
    """'2026-09-18' -> 'جمعه ۲۷ شهریور ۱۴۰۵' (empty string on bad input)."""
    try:
        dt = datetime.strptime((iso_date or "").strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    weekday = WEEKDAYS_FA[dt.weekday()]
    return f"{weekday} {to_fa_digits(jd)} {JALALI_MONTHS[jm - 1]} {to_fa_digits(jy)}"


# --------------------------------------------------------------------------- #
#  Emoji + hashtag extraction from LLM replies                                 #
# --------------------------------------------------------------------------- #

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF]"
)

# Persian/Latin letters, digits (ASCII + Persian) and underscores allowed
_TAG_RE = re.compile(r"^#[A-Za-z0-9\u0600-\u06FF_]{2,30}$")

_HAS_PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")


def has_persian(text: str) -> bool:
    """True when the text contains at least one Persian letter."""
    return bool(_HAS_PERSIAN_RE.search(text or ""))


def pick_emoji(text: str, default: str = "🔭") -> str:
    """Return the first emoji found in the text, or the default."""
    match = _EMOJI_RE.search(text or "")
    return match.group(0) if match else default


def parse_hashtags(text, default, max_tags: int = 4):
    """
    Validate hashtag tokens from an LLM reply.

    Keeps tokens that look like '#نجوم' / '#سیاهچاله_فردی' (single token,
    letters/underscores only, ZWNJ replaced with '_'), de-duplicates, and
    returns at most `max_tags`. Falls back to `default` when nothing valid
    remains.
    """
    tags = []
    for token in (text or "").split():
        token = token.strip(",،\"'«»").replace("\u200c", "_")
        if _TAG_RE.match(token) and token not in tags:
            tags.append(token)
    if not tags:
        return list(default)
    return tags[:max_tags]
