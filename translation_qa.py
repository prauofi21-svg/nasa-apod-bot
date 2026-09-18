#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Translation QA harness — compares candidate LLM models on real content.

Runs on GitHub Actions (Groq is not reachable from some sandboxes). For each
candidate model it translates the current APOD and the top Reddit posts with
the REAL production prompts (two passes: translator + editor) and prints the
full Persian output, so a human can compare quality side by side.

Nothing is sent to Telegram and no state file is touched.

Usage:
    python translation_qa.py                    # auto: all candidate models
    python translation_qa.py --models m1,m2     # only these models
    python translation_qa.py --list-only       # just print available models
"""

from __future__ import annotations

import argparse
import logging
import sys

import llm_translator
from llm_translator import available_models, set_editor_model, set_model_override
from nasa_apod_bot import fetch_apod, translate_apod, validate_apod
from reddit_top_bot import fetch_reddit_posts, pick_posts, translate_post

# (translator model, editor model or None = same model)
CONFIGS = [
    ("qwen/qwen3.8-27b", None),
    ("openai/gpt-oss-120b", None),
    ("qwen/qwen3.8-27b", "openai/gpt-oss-120b"),
    ("openai/gpt-oss-20b", None),
]

REDDIT_POSTS_TO_TEST = 2


def banner(title: str) -> None:
    print("\n" + "#" * 70)
    print(f"# {title}")
    print("#" * 70, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Translation QA harness")
    parser.add_argument("--models", default="",
                        help="comma-separated model ids to test (default: auto)")
    parser.add_argument("--list-only", action="store_true",
                        help="only print the models the provider offers")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")

    models = available_models()
    banner(f"PROVIDER MODEL LIST ({len(models)} models)")
    for m in models:
        print("  ", m)
    if not models:
        print("  (no provider accepted the key)")
        return 1
    if args.list_only:
        return 0

    if args.models:
        to_test = [m.strip() for m in args.models.split(",") if m.strip()]
        configs = [(m, None) for m in to_test]
    else:
        configs = [(t, e) for t, e in CONFIGS if t in models]
    banner("CONFIGS TO TEST: "
           + "; ".join(f"{t} + editor={e or t}" for t, e in configs))
    if not configs:
        print("No candidate model is available — pass --models explicitly.")
        return 1

    # --- fetch the real content once ---------------------------------------
    print("\nFetching the current APOD ...", flush=True)
    try:
        apod = fetch_apod()
        validate_apod(apod)
        print(f"APOD: {apod.get('date')} — {apod.get('title')}")
    except Exception as exc:
        print(f"APOD fetch failed ({exc}) — testing Reddit only.")
        apod = None

    print("\nFetching the top Reddit posts ...", flush=True)
    try:
        posts = pick_posts(fetch_reddit_posts(), set())[:REDDIT_POSTS_TO_TEST]
        for p in posts:
            print(f"  [{p['score']:>6}] r/{p['subreddit']}: {p['title'][:70]}")
    except Exception as exc:
        print(f"Reddit fetch failed ({exc}) — testing APOD only.")
        posts = []

    # --- run every candidate config on the same content ---------------------
    import time
    for index, (translator_model, editor_model) in enumerate(configs):
        if index:
            time.sleep(10)  # let rate-limit windows breathe between configs
        set_model_override(translator_model)
        set_editor_model(editor_model)
        banner(f"CONFIG {index + 1}/{len(configs)}: translator={translator_model} "
               f"editor={editor_model or translator_model}")

        if apod is not None:
            print("\n--- APOD translation (translator + editor) ---", flush=True)
            try:
                translation = translate_apod(apod)
                if translation:
                    print(f"TITLE_FA: {translation['title_fa']}")
                    print(f"EMOJI: {translation['emoji']}")
                    print(f"HASHTAGS: {' '.join(translation['hashtags'])}")
                    print(f"EXPLANATION_FA ({len(translation['explanation_fa'])} chars):")
                    print(translation["explanation_fa"])
                else:
                    print("TRANSLATION FAILED")
            except Exception as exc:
                print(f"TRANSLATION ERROR: {exc}")

        for i, post in enumerate(posts, 1):
            print(f"\n--- REDDIT post {i}/{len(posts)} translation ---", flush=True)
            try:
                translation = translate_post(post)
                if translation:
                    print(f"TITLE_FA: {translation['title_fa']}")
                    if translation["summary_fa"]:
                        print(f"SUMMARY_FA: {translation['summary_fa']}")
                    print(f"EMOJI: {translation['emoji']}")
                    print(f"HASHTAGS: {' '.join(translation['hashtags'])}")
                else:
                    print("TRANSLATION FAILED")
            except Exception as exc:
                print(f"TRANSLATION ERROR: {exc}")

    banner("QA COMPLETE — compare the sections above and pick the best model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
