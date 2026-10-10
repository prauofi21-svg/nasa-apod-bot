#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline tests for channel_guardian.py — no network, no Telegram, no bots run.
Run:  python test_guardian_logic.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import channel_guardian as g

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {info}")
        print(f"  FAIL {name}  {info}")


# --------------------------------------------------------------------------- #
#  Synthetic t.me/s-like HTML fixture (same markup as the real preview)         #
# --------------------------------------------------------------------------- #

def _msg_html(mid, iso_dt, text_html, photo=False, video=False):
    media = ""
    if photo:
        media = ('<div class="tgme_widget_message_photo_wrap">'
                 '<div class="tgme_widget_message_photo" style="width:4px"></div></div>')
    if video:
        media = ('<div class="tgme_widget_message_video_wrap">'
                 '<div class="tgme_widget_message_video"></div></div>')
    return f'''
<div class="tgme_widget_message text_not_allowed_wrap js-widget_message"
     data-post="daily_sciences/{mid}"
     data-view="messages">
  <div class="tgme_widget_message_bubble">
    <i class="tgme_widget_message_bubble_tail"></i>
    <div class="tgme_widget_message_meta"><time datetime="{iso_dt}"><span>12:00</span></time></div>
    {media}
    <div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>
  </div>
</div>'''


def fixture_html(messages):
    parts = "".join(_msg_html(*m) for m in messages)
    return (f'<html><body><div class="tgme_channel_history">'
            f'{parts}</div></body></html>')


def mkmsg(mid, dt, text, photo=False, video=False):
    return g.ChanMsg(id=mid, dt_utc=dt, text=text, has_photo=photo, has_video=video)


UTC = timezone.utc
TODAY = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
D10 = datetime(2026, 10, 10, 9, 49, tzinfo=UTC)
D9 = datetime(2026, 10, 9, 10, 13, tzinfo=UTC)
D10_EVE = datetime(2026, 10, 10, 17, 32, tzinfo=UTC)

APOD_TEXT_10 = ("🌌 عکس نجومی روز ناسا\n\n🌕 پشتِ ماه\n\n"
                "📅 شنبه ۱۸ مهر ۱۴۰۵\n\n🔭 اعتبار: NASA / GSFC\n\n#نجوم #ناسا\n\n"
                "@daily_sciences")
APOD_TEXT_9 = ("🌌 عکس نجومی روز ناسا\n\n🪐 دهانه استیکنی\n\n"
               "📅 جمعه ۱۷ مهر ۱۴۰۵\n\n🔭 اعتبار: HiRISE\n\n#نجوم #ناسا\n\n"
               "@daily_sciences")
APOD_EXPL = ("به دلیل قفل‌شدگی جزر و مدی و چرخش هم‌زمان، ماه همیشه همان سمت "
             "آشنای خود را به ساکنان کرهٔ زمین نشان می‌دهد.\n\n@daily_sciences")

R1 = "🍬 پروتئین طبیعی ۲۰۰۰ برابر شیرین‌تر از قند، جهش قند خون را کاهش داد\n\n⬆️ ۵۴۳ امتیاز • 💬 ۹۷ دیدگاه • r/science\n\n#دانشمند\n\n@daily_sciences"
R2 = "🛰 ماهوارهٔ آب‌وهوایی شوروی «متیور ۲-۷» ممکن است تکه‌تکه شده باشد\n\n⬆️ ۴۲۱ امتیاز • 💬 ۶۶ دیدگاه • r/space\n\n#فضا\n\n@daily_sciences"
R3 = "💪 گیاهخواران مسن به اندازهٔ گوشتخواران قوی می‌شوند\n\n⬆️ ۳۱۱ امتیاز • 💬 ۵۲ دیدگاه • r/science\n\n#سلامت\n\n@daily_sciences"
R4 = "🚀 ناسا فراخوانی دیرپا برای ایستگاه‌های فضایی خصوصی صادر کرد\n\n⬆️ ۲۸۹ امتیاز • 💬 ۴۱ دیدگاه • r/space\n\n#ناسا\n\n@daily_sciences"
R5 = "💼 زنان بیشتر به مشاغل اکثراً زن‌دار علاقه‌مندند\n\n⬆️ ۲۰۴ امتیاز • 💬 ۳۸ دیدگاه • r/science\n\n#جامعه\n\n@daily_sciences"

# same as R1 but with Arabic ي, different ZWNJ/spacing and different numbers
R1_DUP = "🍬 پروتئین طبیعی ۲۰۰۰ برابر شیرین‌تر از قند، جهش قند خون را کاهش داد\n\n⬆️ ۵۵۰ امتیاز • 💬 ۱۰۱ دیدگاه • r/science\n\n#دانشمند\n\n@daily_sciences"

# ============================================================================ #
print("== 1. parse_channel_html ==")
html_fix = fixture_html([
    (184, "2026-10-09T09:56:33+00:00",
     "🌌 عکس نجومی روز ناسا<br/><br/>🪐 دهاره استیکنی<br/><br/>📅 جمعه ۱۷ مهر ۱۴۰۵<br/><br/>@daily_sciences", True, False),
    (185, "2026-10-09T09:56:34+00:00",
     "دهاره استیکنی، بزرگ‌ترین دهاره بر روی فوبوس<br/><br/>@daily_sciences", False, False),
    (191, "2026-10-10T09:49:38+00:00",
     "🌌 عکس نجومی روز ناسا<br/><br/>🌕 پشتِ ماه<br/><br/>📅 شنبه ۱۸ مهر ۱۴۰۵<br/><br/>@daily_sciences", True, False),
    (192, "2026-10-10T09:49:40+00:00", APOD_EXPL.replace("\n", "<br/>"), False, False),
    (193, "2026-10-10T17:32:11+00:00", R1.replace("\n", "<br/>"), True, False),
    (194, "2026-10-10T17:32:19+00:00", R2.replace("\n", "<br/>"), True, False),
])
msgs = g.parse_channel_html(html_fix)
check("6 messages parsed", len(msgs) == 6, f"got {len(msgs)}")
check("ids sorted", [m.id for m in msgs] == [184, 185, 191, 192, 193, 194])
check("datetime parsed+utc", msgs[0].dt_utc == datetime(2026, 10, 9, 9, 56, 33, tzinfo=UTC))
check("photo flag", msgs[0].has_photo and not msgs[3].has_photo)
check("newlines preserved", "\n" in msgs[2].text and msgs[2].text.count("\n") >= 4)
check("text first line intact", msgs[2].text.split("\n")[0] == "🌌 عکس نجومی روز ناسا")

print("== 2. classification ==")
by_id = {m.id: m for m in msgs}
check("apod classified", g.classify(by_id[191]) == "apod" and g.classify(by_id[184]) == "apod")
check("explanation is other", g.classify(by_id[192]) == "other" and g.classify(by_id[185]) == "other")
check("reddit classified", g.classify(by_id[193]) == "reddit" and g.classify(by_id[194]) == "reddit")

print("== 3. Jalali date matching ==")
check("msg191 == 2026-10-10", g.apod_matches_date(by_id[191], "2026-10-10"))
check("msg191 != 2026-10-09", not g.apod_matches_date(by_id[191], "2026-10-09"))
check("msg184 == 2026-10-09", g.apod_matches_date(by_id[184], "2026-10-09"))
check("msg184 != 2026-10-10", not g.apod_matches_date(by_id[184], "2026-10-10"))
check("pretty_date_fa real value", g.pretty_date_fa("2026-10-10") == "شنبه ۱۸ مهر ۱۴۰۵",
      g.pretty_date_fa("2026-10-10"))

print("== 4. normalize / keys / similarity ==")
check("arabic yeh vs farsi yeh same key",
      g.normalize_fa("شیرین") == g.normalize_fa("شيرين"))
check("zwnj ignored",
      g.normalize_fa("شیرین‌تر") == g.normalize_fa("شیرینتر"))
check("spaces ignored", g.normalize_fa("a b c") == g.normalize_fa("abc"))
check("R1 vs R1_DUP same title key",
      g.reddit_title_key(R1) == g.reddit_title_key(R1_DUP),
      f"{g.reddit_title_key(R1)!r} vs {g.reddit_title_key(R1_DUP)!r}")
check("R1 vs R2 different keys", g.reddit_title_key(R1) != g.reddit_title_key(R2))
sim_same = g.title_similarity(R1.split("\n")[0], R1_DUP.split("\n")[0])
check("near-identical titles sim >= 0.9", sim_same >= 0.9, f"sim={sim_same}")
sim_diff = g.title_similarity(R1.split("\n")[0], R5.split("\n")[0])
check("different titles low sim", sim_diff < 0.3, f"sim={sim_diff}")

print("== 5. analyze_reddit_duplicates ==")
rmsgs = [
    mkmsg(193, D10_EVE, R1, photo=True),
    mkmsg(194, D10_EVE, R2, photo=True),
    mkmsg(195, D10_EVE, R3, photo=True),
    mkmsg(196, D10_EVE, R4, photo=True),
    mkmsg(197, D10_EVE + timedelta(minutes=1), R5, photo=True),
    mkmsg(198, D10_EVE + timedelta(minutes=2), R1_DUP, photo=True),
]
strong, near = g.analyze_reddit_duplicates(rmsgs)
check("dup detected -> delete later", strong == [198], f"got {strong}")
strong, near = g.analyze_reddit_duplicates(rmsgs[:5])
check("clean day no strong", strong == [])
# triple post: keep earliest only
triple = [mkmsg(10, D10_EVE, R1, photo=True), mkmsg(11, D10_EVE, R1_DUP, photo=True),
          mkmsg(12, D10_EVE, R1, photo=True)]
strong, _ = g.analyze_reddit_duplicates(triple)
check("triple keeps earliest", strong == [11, 12], f"got {strong}")
# similar but different posts (sim in 0.70..0.90) -> near only, never deleted
t1 = mkmsg(20, D10_EVE, "🔬 دانشمندان موفق به ساخت باتری جدید جامد شدند\n\nr/science", photo=True)
t2 = mkmsg(21, D10_EVE, "🔬 دانشمندان موفق به ساخت باتری جدید مایع شدند\n\nr/science", photo=True)
strong, near = g.analyze_reddit_duplicates([t1, t2])
check("similar-but-different not deleted", strong == [])
check("similar-but-different reported as near", len(near) == 1 and near[0][2] >= 0.7,
      f"{near}")

print("== 6. pick_apod_duplicates ==")
# scenario A: first is an orphan media (no follower), second is a complete
# pair (media + explanation) -> keep the complete pair, delete the orphan
A = [mkmsg(100, D10, APOD_TEXT_10, photo=True), mkmsg(102, D10, APOD_TEXT_10, photo=True)]
by = {100: A[0], 102: A[1], 103: mkmsg(103, D10, APOD_EXPL)}
keep, dele = g.pick_apod_duplicates(A, by)
check("complete pair preferred over orphan", keep == 102, f"keep={keep}")
check("orphan deleted, pair untouched", dele == [100], f"del={dele}")
# scenario B: both complete -> keep earliest pair
B = [mkmsg(200, D10, APOD_TEXT_10, photo=True), mkmsg(202, D10, APOD_TEXT_10, photo=True)]
by2 = {200: B[0], 201: mkmsg(201, D10, APOD_EXPL), 202: B[1], 203: mkmsg(203, D10, APOD_EXPL)}
keep, dele = g.pick_apod_duplicates(B, by2)
check("earliest pair kept", keep == 200, f"keep={keep}")
check("later pair + follower deleted", sorted(dele) == [202, 203], f"del={dele}")

print("== 7. state surgery ==")
tmp = Path(tempfile.mkdtemp())
g.REDDIT_STATE_FILE = tmp / "state_reddit.json"
g.APOD_STATE_FILE = tmp / "state.json"
g.REDDIT_STATE_FILE.write_text(json.dumps(
    {"day": "2026-10-10", "posts_today": 5,
     "posted_ids": ["a", "b", "c", "d", "e"]}), encoding="utf-8")
g.reddit_decrement_quota("2026-10-10", 2)
st = json.loads(g.REDDIT_STATE_FILE.read_text())
check("quota decremented", st["posts_today"] == 3, st)
g.reddit_decrement_quota("2026-10-10", 99)
st = json.loads(g.REDDIT_STATE_FILE.read_text())
check("quota floor 0", st["posts_today"] == 0, st)
g.reddit_decrement_quota("2026-10-09", 1)
st = json.loads(g.REDDIT_STATE_FILE.read_text())
check("other day untouched", st["day"] == "2026-10-10" and st["posts_today"] == 0)
check("quota reader", g.reddit_state_quota(st, "2026-10-10") == 0
      and g.reddit_state_quota(st, "2026-10-09") == 0)

g.APOD_STATE_FILE.write_text(json.dumps(
    {"date": "2026-10-10", "title": "T",
     "posted_dates": ["2026-10-09", "2026-10-10"]}), encoding="utf-8")
g.apod_unmark_date("2026-10-09")
st = json.loads(g.APOD_STATE_FILE.read_text())
check("backfill-date unmark keeps current", st.get("date") == "2026-10-10"
      and st.get("posted_dates") == ["2026-10-10"], st)
g.apod_unmark_date("2026-10-10")
st = json.loads(g.APOD_STATE_FILE.read_text())
check("current-date unmark clears date", st.get("date") is None
      and st.get("posted_dates") == [], st)
check("apod_state_dates union", g.apod_state_dates(
    {"date": "2026-10-10", "posted_dates": ["2026-10-09"]})
    == {"2026-10-10", "2026-10-09"})

print("== 8. phase_apod (dry-run scenarios) ==")


def reset_apod_state(dates=(), current=None):
    g.APOD_STATE_FILE.write_text(json.dumps(
        {"date": current, "title": "T", "posted_dates": list(dates)}),
        encoding="utf-8")


def reset_reddit_state(day="2026-10-10", n=5):
    g.REDDIT_STATE_FILE.write_text(json.dumps(
        {"day": day, "posts_today": n, "posted_ids": ["x"] * n}),
        encoding="utf-8")



class FakeView(g.ChannelView):
    """ChannelView with preset messages; refresh is a no-op."""

    def __init__(self, msgs):
        super().__init__()
        self.msgs = msgs
        self.complete = True

    def refresh(self):
        pass


def mock_meta(status_map):
    def _meta(iso):
        return status_map.get(iso, ({"date": iso, "title": "Mock Title"}, "ok"))
    return _meta


HEALTHY = FakeView([
    mkmsg(184, D9, APOD_TEXT_9, photo=True),
    mkmsg(185, D9, APOD_EXPL),
    mkmsg(191, D10, APOD_TEXT_10, photo=True),
    mkmsg(192, D10, APOD_EXPL),
])
orig_meta = g.fetch_apod_meta
g.fetch_apod_meta = mock_meta({})
reset_apod_state(dates=("2026-10-09", "2026-10-10"), current="2026-10-10")
r = g.phase_apod(TODAY, HEALTHY, dry=True)
check("healthy: no problems", r.ok, r.problems)
check("healthy: both dates reported", len(r.notes) == 2, r.notes)

# missing today, due (evening) -> dry would-run (fresh state: nothing posted)
reset_apod_state()
r = g.phase_apod(TODAY, FakeView([]), dry=True)
check("missing+due: dry would-run", any("would run" in h for h in r.healed), r.healed)
check("missing+due: no problem in dry", r.ok, r.problems)

# missing today, NOT due (morning) -> today pending, yesterday would-backfill
reset_apod_state()
MORNING = datetime(2026, 10, 10, 9, 55, tzinfo=UTC)
r = g.phase_apod(MORNING, FakeView([]), dry=True)
check("missing+not-due: no problem", r.ok, r.problems)
check("missing+not-due: pending note for today",
      any("not posted yet" in n or "not yet due" in n for n in r.notes), r.notes)
check("missing+not-due: yesterday backfill would-run",
      any("2026-10-09" in h and "would run" in h for h in r.healed), r.healed)

# missing but state says posted -> accepted (preview lag)
reset_apod_state(dates=("2026-10-10",), current="2026-10-10")
r = g.phase_apod(TODAY, FakeView([]), dry=True)
check("state-lag accepted", r.ok and any("accepted" in n for n in r.notes), (r.problems, r.notes))

# duplicate APOD (dry): simulated -> healthy verdict
reset_apod_state(dates=("2026-10-10",), current="2026-10-10")
DUPVIEW = FakeView([
    mkmsg(191, D10, APOD_TEXT_10, photo=True),
    mkmsg(192, D10, APOD_EXPL),
    mkmsg(193, D10, APOD_TEXT_10, photo=True),
    mkmsg(194, D10, APOD_EXPL),
])
r = g.phase_apod(TODAY, DUPVIEW, dry=True)
check("dup apod: dry would-delete", any("duplicate" in h and "would delete" in h
                                        for h in r.healed), r.healed)
check("dup apod: simulated healthy verdict", r.ok, r.problems)

# broken APOD (text-only, dry) -> would-repair, no problem
reset_apod_state(dates=("2026-10-10",), current="2026-10-10")
BROKEN = FakeView([mkmsg(191, D10, APOD_TEXT_10, photo=False)])
r = g.phase_apod(TODAY, BROKEN, dry=True)
check("broken apod: would-repair in dry",
      any("broken" in h and "would" in h for h in r.healed), r.healed)
check("broken apod: no problem in dry", r.ok, r.problems)

# yesterday missing (backfill case, dry)
reset_apod_state(dates=("2026-10-10",), current="2026-10-10")
r = g.phase_apod(TODAY, FakeView([mkmsg(191, D10, APOD_TEXT_10, photo=True)]), dry=True)
check("yesterday missing: would-run backfill",
      any("2026-10-09" in h and "would run" in h for h in r.healed), r.healed)

# yesterday missing + preview UNAVAILABLE -> never backfill blindly (no dups)
reset_apod_state(dates=("2026-10-10",), current="2026-10-10")
NOVIEW_A = FakeView([mkmsg(191, D10, APOD_TEXT_10, photo=True)])
NOVIEW_A.available = False
r = g.phase_apod(TODAY, NOVIEW_A, dry=True)
check("no-preview: no blind backfill of older days",
      r.ok and any("no channel evidence" in n for n in r.notes)
      and not any("2026-10-09" in h for h in r.healed), (r.problems, r.notes, r.healed))

# today missing + preview UNAVAILABLE + state lacks -> bot still runs (like slots)
reset_apod_state()
NOVIEW_T = FakeView([])
NOVIEW_T.available = False
r = g.phase_apod(TODAY, NOVIEW_T, dry=True)
check("no-preview: today still top-up-able",
      any("2026-10-10" in h and "would run" in h for h in r.healed), r.healed)

# NASA API unreachable -> checks still proceed (never skipped)
reset_apod_state(dates=("2026-10-09", "2026-10-10"), current="2026-10-10")
g.fetch_apod_meta = mock_meta({"2026-10-10": (None, "unreachable"),
                               "2026-10-09": (None, "unreachable")})
r = g.phase_apod(TODAY, HEALTHY, dry=True)
check("api unreachable: still verified",
      r.ok and sum(1 for n in r.notes if "✓ healthy" in n) == 2, (r.problems, r.notes))

# NASA says future -> skipped quietly
reset_apod_state()
g.fetch_apod_meta = mock_meta({"2026-10-10": (None, "future")})
r = g.phase_apod(MORNING, FakeView([]), dry=True)
check("future: skipped without problem",
      r.ok and any("not published yet" in n for n in r.notes), (r.problems, r.notes))
g.fetch_apod_meta = orig_meta

print("== 9. phase_reddit (dry-run scenarios) ==")
reset_reddit_state()
FIVE = FakeView([
    mkmsg(193, D10_EVE, R1, photo=True), mkmsg(194, D10_EVE, R2, photo=True),
    mkmsg(195, D10_EVE, R3, photo=True), mkmsg(196, D10_EVE, R4, photo=True),
    mkmsg(197, D10_EVE, R5, photo=True),
])
r = g.phase_reddit(TODAY, FIVE, dry=True)
check("5/5: ok", r.ok and any("5/5" in n for n in r.notes), (r.problems, r.notes))
check("5/5: no dup actions", r.healed == [])

# 4/5 + quota 4 -> would top up (simulated success: no problem in dry)
reset_reddit_state(n=4)
r = g.phase_reddit(TODAY, FakeView(FIVE.msgs[:4]), dry=True)
check("4/5: dry would top up", any("would run" in h for h in r.healed), r.healed)
check("4/5: no problem in dry", r.ok, r.problems)

# 4/5 + quota 5 (preview lag) -> accepted
reset_reddit_state(n=5)
r = g.phase_reddit(TODAY, FakeView(FIVE.msgs[:4]), dry=True)
check("preview-lag accepted", r.ok and any("accepted" in n for n in r.notes),
      (r.problems, r.notes))

# duplicates present (dry): simulate deletion -> verdict should stay clean
reset_reddit_state(n=5)
DUPR = FakeView(FIVE.msgs + [mkmsg(198, D10_EVE + timedelta(minutes=2), R1_DUP, photo=True)])
r = g.phase_reddit(TODAY, DUPR, dry=True)
check("dup reddit: would delete", any("would delete" in h and "198" in h
                                      for h in r.healed), r.healed)
check("dup reddit: simulated verdict ok", r.ok, r.problems)

# channel unavailable + quota met -> accepted
reset_reddit_state(n=5)
NOVIEW = FakeView([])
NOVIEW.available = False
r = g.phase_reddit(TODAY, NOVIEW, dry=True)
check("no-preview + quota 5: accepted", r.ok and any("unavailable" in n for n in r.notes),
      (r.problems, r.notes))

# channel unavailable + quota 0 -> would run
reset_reddit_state(n=0)
r = g.phase_reddit(TODAY, NOVIEW, dry=True)
check("no-preview + quota 0: would run", any("would run" in h for h in r.healed), r.healed)

# more than expected without dups -> manual review problem
reset_reddit_state(n=6)
SIX = FakeView(FIVE.msgs + [
    mkmsg(198, D10_EVE + timedelta(minutes=2), "🧪 روان‌شناسی تصمیم‌گیری در شرایط بحران\n\nr/science", photo=True)])
r = g.phase_reddit(TODAY, SIX, dry=True)
check("over-quota without dups: problem", any("manual review" in p for p in r.problems),
      r.problems)

print("== 10. print_report ==")
probs = g.print_report(TODAY, [g.PhaseReport("APOD", notes=["n1"]),
                               g.PhaseReport("Reddit", problems=["p1"])])
check("report collects problems", probs == ["[Reddit] p1"])


# ============================================================================ #
print()
print("=" * 60)
print(f"PASS: {PASS}   FAIL: {FAIL}")
if FAILURES:
    print("FAILURES:")
    for f in FAILURES:
        print("  -", f)
sys.exit(1 if FAIL else 0)
