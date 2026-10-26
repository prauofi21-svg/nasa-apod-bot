#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Meta-Guardian — the scheduling safety net for prauofi21-svg/nasa-apod-bot.

WHY IT EXISTS (2026-10-19): GitHub's cron scheduler can silently die for ONE
repo. On 2026-10-19, after 2026-10-18T23:23Z not a single scheduled trigger
of this repo fired for 17+ hours — all 8 NASA slots, all 6 Reddit slots and
all 3 Channel Guardian slots were silently dropped, the channel stayed
silent the whole day, while the same account's other repo scheduled normally
and workflow_dispatch worked flawlessly (both pipelines were revived by two
manual dispatches at 16:30Z). Cron-only resilience is NOT enough, so this
repo now runs TWO independent scheduling engines (see meta-guardian.yml):

  Engine 1   cron "7,37 * * * *" on the meta-guardian workflow itself —
             alive whenever GitHub's scheduler is healthy; while the
             self-chain is alive these runs are superseded (no double
             beats), and they restart the chain if it ever breaks.
  Engine 2   the self-chaining beat loop — each run checks the pipelines,
             sleeps BEAT minutes, then the workflow dispatches the NEXT
             beat with workflow_dispatch, which does not depend on the
             cron scheduler at all.
  Engine 3   lives in the jadvalak repo: its Gateway watchdog (whose cron
             kept firing through the 2026-10-19 outage) re-dispatches this
             workflow cross-repo whenever it has been silent for > 2.5 h.

WHAT A BEAT DOES (all times UTC; every dispatch target is state-protected —
state.json / state_reddit.json make an extra dispatch a safe no-op whenever
the channel is already healthy, so this loop can never double-post):

  NASA      state.json.date != today AND now >= 17:40
            -> dispatch nasa-apod-daily.yml
            (10 min after the MAIN 17:30 slot. The old rule waited for ALL
            evening slots until 21:45 — but GitHub's cron brownouts drop
            slots for whole days (2026-10-26: 17:30, 18:15 and 19:05 were
            all silently dropped), which made the post 3+ hours late.
            Now the FIRST missed main slot triggers a dispatch within one
            beat (~26 min), and every beat retries until one succeeds —
            one success flips the state and the rule goes quiet. An
            in-flight NASA run is never double-dispatched.)
  Reddit    state_reddit.json.day != today AND now >= 10:20
            -> dispatch reddit-top-daily.yml
            (the 10:00 main slot produced nothing for the whole day so far;
            retries every beat — one success flips the state; in-flight
            runs are skipped)
  Guardian  last Channel Guardian run older than 13 h
            -> dispatch channel-guardian.yml
            (its scheduled slots 09:55/22:10/23:05 have a max legit gap of
            10h50m; self-limiting: once dispatched, the fresh run resets
            the 13 h clock, so this fires at most ~2x/day when cron is dead)

DELIBERATELY NOT a meta rule: partial Reddit days (1-4 of 5 posts). Reading
the real channel and healing partial days is the Channel Guardian's own
specialty (it verifies "5/5 + APOD" against t.me/s/ content and backfills
dated gaps); the meta layer only guarantees that SOMETHING runs.

Pure stdlib (urllib) — no pip install, no third-party dependencies, no
secrets beyond the repo-scoped GITHUB_TOKEN.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("REPO", "prauofi21-svg/nasa-apod-bot")
TOKEN = os.environ.get("GH_TOKEN", "").strip()
BRANCH = "main"

NASA_WF = "nasa-apod-daily.yml"
REDDIT_WF = "reddit-top-daily.yml"
GUARDIAN_WF = "channel-guardian.yml"

NASA_MAIN_GRACE_MIN = 17 * 60 + 40     # main 17:30Z slot + 10 min grace
REDDIT_MAIN_GRACE_MIN = 10 * 60 + 20    # main 10:00 slot + 20 min grace
GUARDIAN_MAX_GAP_H = 13.0               # max legit gap is 10h50m
BEAT_DEFAULT_MIN = 26
BEAT_MAX_MIN = 32                       # stay under the job timeout


def log(msg):
    print(f"[meta] {msg}", flush=True)


def api(path, payload=None):
    """GET or POST the GitHub REST API with the repo-scoped token."""
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "meta-guardian",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def dispatch(workflow, why):
    """Dispatch a workflow in THIS repo (state-protected no-op when healthy)."""
    try:
        api(f"/repos/{REPO}/actions/workflows/{workflow}/dispatches",
            {"ref": BRANCH})
        log(f"DISPATCH {workflow} — {why}")
        return True
    except urllib.error.HTTPError as exc:
        # 409 = workflow disabled; 422 = a run is already queued/superseded.
        # Neither is a loop-breaking error — the next beat re-checks anyway.
        log(f"dispatch {workflow} not accepted (HTTP {exc.code}) — {why}")
        return False
    except Exception as exc:  # noqa: BLE001 — a beat never crashes the chain
        log(f"dispatch {workflow} FAILED: {exc}")
        return False


def read_state(path):
    """Read a JSON state file from the default branch (None if missing)."""
    try:
        blob = api(f"/repos/{REPO}/contents/{path}?ref={BRANCH}")
        return json.loads(base64.b64decode(blob["content"]))
    except Exception as exc:  # noqa: BLE001
        log(f"could not read {path}: {exc}")
        return None


def minutes_now_utc():
    now = datetime.now(timezone.utc)
    return now.hour * 60 + now.minute, now


def run_in_progress(workflow):
    """True when the workflow has a queued/in-progress run right now.

    Dispatching on top of an in-flight run just burns queue slots (the
    channel-pipelines concurrency group serializes them anyway) — and a
    pending queued run already IS the retry we wanted.
    """
    try:
        runs = api(f"/repos/{REPO}/actions/workflows/{workflow}/runs?per_page=5")
        for r in (runs.get("workflow_runs") or [])[:5]:
            if r.get("status") in ("queued", "in_progress", "waiting", "pending"):
                return True
    except Exception as exc:  # noqa: BLE001 — never break the beat
        log(f"run-in-progress check error for {workflow}: {exc}")
    return False


def check_nasa(today, minutes_now):
    state = read_state("state.json")
    nasa_date = (state or {}).get("date")
    log(f"NASA state date={nasa_date} (today={today})")
    if nasa_date is not None and nasa_date == today:
        return
    if minutes_now < NASA_MAIN_GRACE_MIN:
        log("NASA not due yet (main 17:30Z slot still ahead) — no dispatch")
        return
    if run_in_progress(NASA_WF):
        log("a NASA run is queued/in flight — letting it finish first")
        return
    dispatch(NASA_WF,
             f"main slot grace passed with date={nasa_date} != {today}")


def check_reddit(today, minutes_now):
    state = read_state("state_reddit.json")
    day = (state or {}).get("day")
    posts_today = (state or {}).get("posts_today")
    log(f"Reddit state day={day} posts_today={posts_today} (today={today})")
    if day == today:
        return  # posted today (partial days are the Guardian's specialty)
    if minutes_now < REDDIT_MAIN_GRACE_MIN:
        log("Reddit not due yet (before the 10:00 main slot) — no dispatch")
        return
    if run_in_progress(REDDIT_WF):
        log("a Reddit run is queued/in flight — letting it finish first")
        return
    dispatch(REDDIT_WF, f"day={day} != {today} and the main slot is long gone")


def check_guardian():
    try:
        runs = api(f"/repos/{REPO}/actions/workflows/{GUARDIAN_WF}/runs?per_page=1")
        entries = runs.get("workflow_runs") or []
        if not entries:
            log("Channel Guardian has never run — dispatching now")
            dispatch(GUARDIAN_WF, "no guardian run on record")
            return
        created = datetime.strptime(entries[0]["created_at"],
                                    "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        gap_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
        log(f"Guardian last run {entries[0]['created_at']} ({gap_h:.1f} h ago)")
        if gap_h > GUARDIAN_MAX_GAP_H:
            dispatch(GUARDIAN_WF,
                     f"guardian silent for {gap_h:.1f} h (max legit 10h50m)")
    except Exception as exc:  # noqa: BLE001
        log(f"Guardian check error: {exc}")


def main():
    if not TOKEN:
        log("FATAL: GH_TOKEN is not set — cannot dispatch anything")
        sys.exit(1)

    minutes_now, now = minutes_now_utc()
    today = now.strftime("%Y-%m-%d")
    log(f"beat @ {now.isoformat(timespec='seconds')} | repo={REPO} | today={today}")

    for name, fn in (
        ("NASA", lambda: check_nasa(today, minutes_now)),
        ("Reddit", lambda: check_reddit(today, minutes_now)),
        ("Guardian", check_guardian),
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — never break the beat loop
            log(f"{name} check error: {exc}")

    # Sleep, then the workflow's chain step dispatches the next beat.
    try:
        beat = int(os.environ.get("BEAT_MINUTES", str(BEAT_DEFAULT_MIN)))
    except ValueError:
        beat = BEAT_DEFAULT_MIN
    beat = max(1, min(beat, BEAT_MAX_MIN))
    log(f"sleeping {beat} min — the workflow then chains the next beat")
    sys.stdout.flush()
    time.sleep(beat * 60)
    log(f"beat done @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
