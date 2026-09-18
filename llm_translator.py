#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared LLM translation helper with automatic provider detection.

Works with OpenAI-compatible chat-completions APIs. Given one API key
(env GROK_API_KEY), it auto-detects which provider accepts it:

  1. Groq  (api.groq.com/openai/v1)  — keys look like gsk_xxx
  2. xAI   (api.x.ai/v1)             — Grok models, keys look like gsk-xxx

Public helpers:
    ask_llm(system, user, ...)          -> str | None
    translate_draft(...)                -> dict | None (first-pass JSON reply)
    review_translation(source, draft)   -> dict | None (second-pass editor)
    set_model_override(model)           -> force a model id (QA / testing)
    available_models()                  -> [str] model ids of the working provider
    provider_report()                   -> str (diagnostics, performs a smoke test)

Translation strategy: TWO passes.
    Pass 1 (translator): English -> Persian draft as JSON.
    Pass 2 (editor): a strict Persian language editor compares the draft
    with the English source and fixes meaningless words, wrong terms,
    calques, and grammar errors before the text reaches the channel.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("llm-translator")

API_KEY = (os.environ.get("GROK_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()

# Models are tried in order; the first one offered by the provider wins.
# Order chosen after live QA (2026-09-18) comparing Persian quality:
#   1. qwen/qwen3.8-27b    — most natural, correct Persian (winner)
#   2. openai/gpt-oss-120b — decent backup, occasionally awkward wording
# Never use gpt-oss-20b: it hallucinated a telescope name in the APOD post.
PROVIDERS = [
    {
        "name": "Groq",
        "base": "https://api.groq.com/openai/v1",
        "models": [
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ],
    },
    {
        "name": "xAI",
        "base": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-3-latest", "grok-2-latest", "grok-beta"],
    },
]

# Fallback pattern for chat-capable text models (never whisper/tts/guard).
_CHAT_MODEL_RE = re.compile(
    r"(llama|gpt-oss|qwen|kimi|gemma|grok|mistral|allam)", re.IGNORECASE
)

_HTTP_TIMEOUT = (15, 120)
_DETECTED = None        # {"provider": str, "base": str, "model": str}
_MODEL_OVERRIDE = None  # set via set_model_override() for QA runs


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _pick_model(ids) -> str:
    """Choose the model: explicit override > preference list > first chat model."""
    if _MODEL_OVERRIDE:
        return _MODEL_OVERRIDE
    for prov in PROVIDERS:
        for m in prov["models"]:
            if m in ids:
                return m
    for m in ids:
        if _CHAT_MODEL_RE.search(m):
            return m
    return ids[0]


def _detect():
    """Find the provider that accepts the key; pick the best model."""
    global _DETECTED
    if _DETECTED is not None:
        return _DETECTED
    if not API_KEY:
        log.info("No LLM API key set (GROK_API_KEY) — translation disabled")
        return None
    for prov in PROVIDERS:
        try:
            r = requests.get(f"{prov['base']}/models", headers=_headers(), timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                log.info("LLM provider %s rejected the key (HTTP %d)", prov["name"], r.status_code)
                continue
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            if not ids:
                log.info("LLM provider %s returned no models", prov["name"])
                continue
            _DETECTED = {"provider": prov["name"], "base": prov["base"],
                         "model": _pick_model(ids)}
            log.info("LLM provider detected: %s (%s)", prov["name"], _DETECTED["model"])
            return _DETECTED
        except requests.RequestException as exc:
            log.warning("LLM provider %s /models failed: %s", prov["name"], exc)
    log.warning("No LLM provider accepted the API key — translation disabled")
    return None


def set_model_override(model: str):
    """Force a specific model id (used by the QA script to compare models)."""
    global _MODEL_OVERRIDE, _DETECTED
    _MODEL_OVERRIDE = (model or "").strip() or None
    _DETECTED = None  # re-detect with the override in place


# Optional different model for the editor pass (None = same as translator).
_EDITOR_MODEL = (os.environ.get("LLM_EDITOR_MODEL") or "").strip() or None


def set_editor_model(model: str):
    """Force the model used by review_translation() (QA / experiments)."""
    global _EDITOR_MODEL
    _EDITOR_MODEL = (model or "").strip() or None


def available_models() -> list:
    """Model ids offered by the working provider (empty list if none)."""
    for prov in PROVIDERS:
        try:
            r = requests.get(f"{prov['base']}/models", headers=_headers(), timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                if ids:
                    return ids
        except requests.RequestException:
            continue
    return []


def current_model() -> str:
    det = _detect()
    return det["model"] if det else ""


def ask_llm(system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3,
             retries: int = 4, model: str = None):
    """Send one chat completion. Returns the reply text, or None on failure.

    `model` optionally overrides the detected model for this single call
    (used to run the editor pass on a different model). HTTP 429 gets a
    long backoff: the active model (qwen3.8-27b) allows only ~1000 output
    tokens per minute, so rate-limit windows need real time to roll over.
    """
    det = _detect()
    if det is None:
        return None
    url = f"{det['base']}/chat/completions"
    payload = {
        "model": (model or det["model"]).strip() or det["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=_headers(), json=payload, timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                choices = r.json().get("choices") or [{}]
                content = (choices[0].get("message") or {}).get("content")
                if content and content.strip():
                    return content.strip()
                last_err = "empty completion"
            elif r.status_code == 429:
                # Rate limited — wait for the token window to actually roll
                # over (Retry-After header wins when present).
                wait = 30
                try:
                    wait = max(int(r.headers.get("retry-after") or 0), 20)
                except (TypeError, ValueError):
                    pass
                last_err = f"HTTP 429: {r.text[:150]}"
                if attempt < retries:
                    log.warning("LLM rate-limited — sleeping %ds (attempt %d/%d)",
                                wait, attempt, retries)
                    time.sleep(wait)
                    continue
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (400, 401, 403):
                    break  # auth/permission errors will not heal by retrying
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < retries:
            time.sleep(3 * attempt)
    log.warning("LLM call failed: %s", last_err)
    return None


def parse_json_obj(text):
    """Extract the first JSON object from an LLM reply (tolerates fences).

    LLMs frequently emit literal newlines / tabs inside JSON string values
    (e.g. a multi-paragraph explanation_fa echoed back by the editor pass),
    which makes strict json.loads fail. We therefore try, in order:
      1. strict parse of the { ... } slice,
      2. the same slice after repairing control characters / trailing commas.
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = cleaned[start:end + 1]
    for attempt in (candidate, _repair_json(candidate)):
        try:
            data = json.loads(attempt)
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    return None


def _repair_json(text: str) -> str:
    """Best-effort repair of LLM JSON: escape control chars inside strings
    and drop trailing commas before } or ]."""
    out = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    repaired = "".join(out)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


# --------------------------------------------------------------------------- #
#  Pass 1: translation draft                                                   #
# --------------------------------------------------------------------------- #

TRANSLATOR_SYSTEM = (
    "You are a professional Persian (Farsi) science writer and translator for "
    "a popular Iranian Telegram science channel. You always answer with valid "
    "JSON only — no commentary, no markdown fences."
)


def translate_draft(prompt: str, required_keys, max_tokens: int = 2048,
                    temperature: float = 0.35) -> dict:
    """First pass: ask the LLM for a JSON object with the given keys."""
    det = _detect()
    if det is None:
        return None
    raw = ask_llm(TRANSLATOR_SYSTEM, prompt, max_tokens=max_tokens,
                  temperature=temperature)
    data = parse_json_obj(raw)
    if not data:
        log.warning("Draft translation was not valid JSON")
        return None
    for key in required_keys:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            log.warning("Draft JSON missing key %r", key)
            return None
    return {k: str(data[k]).strip() for k in required_keys}


# --------------------------------------------------------------------------- #
#  Pass 2: strict Persian editor                                               #
# --------------------------------------------------------------------------- #

EDITOR_SYSTEM = (
    "You are a strict, experienced Persian (Farsi) language editor (ویراستار) "
    "for a popular Iranian science channel. You always answer with valid JSON "
    "only — no commentary, no markdown fences."
)


def review_translation(english_source: str, draft: dict,
                       max_tokens: int = 2048) -> dict:
    """
    Second pass: a strict Persian editor fixes the draft translation.

    `english_source` is the ground-truth English text; `draft` is the parsed
    JSON from pass 1. Returns the corrected dict (same keys), or None when
    the editor call fails (callers then keep the draft as-is). Two attempts
    are made — a failed first reply is often a transient formatting glitch.
    """
    det = _detect()
    if det is None or not draft:
        return None
    draft_json = json.dumps(draft, ensure_ascii=False, indent=1)
    prompt = (
        "Below are (1) the English source text — the ground truth — and "
        "(2) a draft Persian translation as JSON.\n"
        "Your job: correct the Persian translation so it becomes flawless, "
        "natural Persian (فارسی صحیح و روان) — exactly what an educated "
        "Iranian editor would publish.\n\n"
        "Fix ALL of these problems wherever they exist:\n"
        "1. meaningless, invented, or plainly wrong words — words no Iranian "
        "would ever use in this sense → replace with the correct common word\n"
        "2. non-standard scientific terms → use the standard Persian "
        "terminology of Persian Wikipedia and Iranian science media\n"
        "3. literal word-by-word translation (calque) from English → rewrite "
        "the sentence naturally, translate the meaning\n"
        "4. grammar errors: اضافهٔ کسره (hazfe), نیم‌فاصله, prepositions, "
        "verb agreement, plurals\n"
        "5. awkward word order, unnatural phrasing, translated-sounding text\n"
        "6. numbers must be Persian digits (۰۱۲۳۴۵۶۷۸۹); fix broken "
        "punctuation\n\n"
        "Hard limits:\n"
        "- Do NOT change the meaning; do NOT add or remove any fact.\n"
        "- Keep the exact same JSON keys.\n"
        "- Keep the same paragraph structure and length (±20%).\n"
        "- Persian script only; well-known proper names in their common "
        "Persian form (ناسا، تلسکوپ فضایی جیمز وب), other proper names in "
        "Latin.\n\n"
        f"English source:\n{english_source}\n\n"
        f"Draft Persian translation (JSON):\n{draft_json}\n\n"
        "Return ONLY the corrected JSON with the same keys."
    )
    raw = ask_llm(EDITOR_SYSTEM, prompt, max_tokens=max_tokens, temperature=0.2,
                  model=_EDITOR_MODEL)
    data = parse_json_obj(raw)
    if not data and raw:
        log.warning("Editor reply was not valid JSON (head: %r)", raw[:160])
        # one more attempt — often the model just misformatted once
        time.sleep(2)
        raw = ask_llm(EDITOR_SYSTEM, prompt, max_tokens=max_tokens,
                      temperature=0.1, model=_EDITOR_MODEL)
        data = parse_json_obj(raw)
        if not data and raw:
            log.warning("Editor retry also failed (head: %r)", raw[:160])
    if not data:
        log.warning("Editor pass returned invalid JSON — keeping the draft")
        return None
    out = {}
    for key in draft:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if not out:
        log.warning("Editor pass dropped all keys — keeping the draft")
        return None
    return out


def provider_report() -> str:
    """Diagnostics: which provider works + one tiny live translation test."""
    det = _detect()
    if det is None:
        if not API_KEY:
            return "LLM: no API key configured (GROK_API_KEY not set)"
        return ("LLM: NO PROVIDER ACCEPTED THE KEY (tried "
                + ", ".join(p["name"] for p in PROVIDERS) + ")")
    reply = ask_llm(
        "You are a translation engine. Reply with the translation only.",
        'Translate to Persian: "the red planet"',
        max_tokens=50,
    )
    if reply:
        return f"LLM OK: {det['provider']} / {det['model']} — smoke test: {reply[:60]}"
    return f"LLM BROKEN: {det['provider']} / {det['model']} (chat call failed)"
