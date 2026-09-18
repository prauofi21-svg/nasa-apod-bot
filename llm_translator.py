#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared LLM translation helper with automatic provider detection.

Works with OpenAI-compatible chat-completions APIs. Given one API key
(env GROK_API_KEY), it auto-detects which provider accepts it:

  1. Groq  (api.groq.com/openai/v1)  — keys look like gsk_xxx
  2. xAI   (api.x.ai/v1)             — Grok models, keys look like gsk-xxx

Public helpers:
    ask_llm(system, user, ...)        -> str | None
    translate_fields(prompt, keys)    -> dict | None (JSON reply parsed)
    provider_report()                 -> str (diagnostics, performs a smoke test)
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

PROVIDERS = [
    {
        "name": "Groq",
        "base": "https://api.groq.com/openai/v1",
        "models": [
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.1-8b-instant",
            "llama3-70b-8192",
            "llama3-8b-8192",
        ],
    },
    {
        "name": "xAI",
        "base": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-3-latest", "grok-2-latest", "grok-beta"],
    },
]

_HTTP_TIMEOUT = (15, 90)
_DETECTED = None  # {"provider": str, "base": str, "model": str}


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _detect():
    """Find the first provider that accepts the key; pick the best model."""
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
            model = next((m for m in prov["models"] if m in ids), ids[0])
            _DETECTED = {"provider": prov["name"], "base": prov["base"], "model": model}
            log.info("LLM provider detected: %s (%s)", prov["name"], model)
            return _DETECTED
        except requests.RequestException as exc:
            log.warning("LLM provider %s /models failed: %s", prov["name"], exc)
    log.warning("No LLM provider accepted the API key — translation disabled (English-only posts)")
    return None


def ask_llm(system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3, retries: int = 3):
    """Send one chat completion. Returns the reply text, or None on failure."""
    det = _detect()
    if det is None:
        return None
    url = f"{det['base']}/chat/completions"
    payload = {
        "model": det["model"],
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
    """Extract the first JSON object from an LLM reply (tolerates fences)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def translate_fields(prompt: str, required_keys) -> dict:
    """
    Ask the LLM for a JSON object with the given keys (all must be non-empty
    strings). Returns the dict, or None if anything failed — callers should
    treat None as "fall back to English".
    """
    det = _detect()
    if det is None:
        return None
    system = (
        "You are a professional English-to-Persian (Farsi) translator writing for a "
        "popular Iranian Telegram science channel. You always answer with valid "
        "JSON only — no commentary, no markdown fences."
    )
    raw = ask_llm(system, prompt)
    data = parse_json_obj(raw)
    if not data:
        log.warning("LLM reply was not valid JSON — falling back to English")
        return None
    for key in required_keys:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            log.warning("LLM JSON missing key %r — falling back to English", key)
            return None
    return {k: str(data[k]).strip() for k in required_keys}


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
