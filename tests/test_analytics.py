"""The offline analysis (activity analytics, phase 3) on a
scripted store with known answers: visits and on-screen time, lecture
coverage and narration, question gaps and their buckets, topic switches
and disengagement points, class summaries and comparisons, and every
command of the CLI in every format.

The script: course AST101 has lecture 3 (six slides, 180 s of narration) and
lecture 4 (five slides, no narration). Class A holds S1 and S2, class B
holds S3 and S4.

  S1, 2026-10-01: a morning visit (three questions on lecture 3, the deck
     open beside the chat, 150 s of narration played, slides 1 to 3 seen,
     a heartbeat gap of 20 s, then the tab hidden 40 s after the last
     reply) and an afternoon visit (a question on lecture 4, one on
     lecture 3, then the lecture-4 deck for two minutes).
  S2: one question, then heartbeats for fifteen minutes (stayed), then the
     lecture-3 deck flipped through in a second a slide.
  S3: one question the browser never saw answered, and a lost batch.
  S4: never came.
"""
from __future__ import annotations

import datetime
import json
import sqlite3

import pytest

from tutor.analytics import cohort, daily, lectures as lectures_mod, questions as questions_mod, store as store_mod
from tutor.analytics import topics as topics_mod, visits as visits_mod
from tutor.analytics.__main__ import main
from tutor.analytics.base import Store, Thresholds, Window, window_days

DAY = datetime.datetime(2026, 10, 1)
T = Thresholds()


def at(h: int, m: int, s: int = 0, day: datetime.datetime = DAY) -> datetime.datetime:
    return day + datetime.timedelta(hours=h, minutes=m, seconds=s)


class Script:
    """Rows for the local store, written through the export's own writer."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {name: [] for name in store_mod.TABLE_NAMES}
        self.ids = {name: 0 for name in store_mod.TABLE_NAMES}
        self.seq: dict[str, int] = {}

    def add(self, table: str, **row) -> dict:
        self.ids[table] += 1
        row.setdefault("id", self.ids[table])
        self.rows[table].append(row)
        return row

    def event(self, sid: int, name: str, ts: datetime.datetime, *, page: str = "p", visit: str = "v",
              course="AST101", lecture=None, slide=None, value_ms=None, turn_id=None, message_id=None,
              path="/chat", skip_seq=0, **detail) -> dict:
        key = f"{sid}:{page}"
        self.seq[key] = self.seq.get(key, 0) + 1 + skip_seq
        if path == "/chat" and lecture and name.startswith(("deck_", "slide_view", "narration_")):
            path = f"/slides/{course}/lecture-{lecture:02d}/index.html"     # a deck's own page
        return self.add("activity_events", student_id=sid, login_session_id=sid, visit_id=f"{visit}{sid}",
                        page_view_id=f"{page}{sid}", seq=self.seq[key], name=name, ts=ts, client_ts=ts,
                        course=course, lecture_num=lecture, slide_num=slide, turn_id=turn_id,
                        message_id=message_id, path=path, value_ms=value_ms,
                        detail_json=json.dumps(detail, sort_keys=True) if detail else None)

    def turn(self, sid: int, asked: datetime.datetime, lecture: int, reply_after_s: int = 10,
             goal: str = "AST101-L03-sea", turn_id: str = "", page: str = "p", with_events: bool = True,
             outcome: str = "done") -> tuple[dict, dict]:
        turn_id = turn_id or f"t{sid}{asked:%H%M%S}"
        q = self.add("messages", student_id=sid, role="user", course="AST101", lecture_num=lecture,
                     created_at=asked, chars=18)
        a = self.add("messages", student_id=sid, role="assistant", course="AST101", lecture_num=lecture,
                     created_at=asked + datetime.timedelta(seconds=reply_after_s), chars=40)
        self.add("turn_events", student_id=sid, ts=asked + datetime.timedelta(seconds=1), kind="chat",
                 turn_id=turn_id, intent="QUESTION", rung_used=2, goal_ids_json=json.dumps([goal] if goal else []),
                 verdict="allow", latency_ms=reply_after_s * 1000, cost_usd=0.01)
        if with_events:
            self.event(sid, "question_sent", asked, lecture=lecture, page=page, chars=18, kind="typed")
            self.event(sid, "reply_done", asked + datetime.timedelta(seconds=reply_after_s + 2), lecture=lecture,
                       page=page, turn_id=turn_id, message_id=a["id"], value_ms=(reply_after_s + 2) * 1000,
                       chars=40, outcome=outcome)
        return q, a


def build_script() -> Script:
    sc = Script()
    for sid, name in ((1, "Ann"), (2, "Bo"), (3, "Cy"), (4, "Di"), (9, "Prof")):
        sc.add("students", id=sid, pseudonym=f"S{sid}", created_at=DAY, is_instructor=1 if sid == 9 else 0,
               login_count=1)
    sc.add("classes", id=1, name="AST 101 A", course="AST101", term="Au26", instructor_id=9, archived=0, created_at=DAY)
    sc.add("classes", id=2, name="AST 101 B", course="AST101", term="Au26", instructor_id=9, archived=0, created_at=DAY)
    for cid, sids in ((1, (1, 2)), (2, (3, 4))):
        for sid in sids:
            sc.add("class_enrollments", class_id=cid, student_id=sid, created_at=DAY)
    sc.add("learning_goals", id="AST101-L03-sea", title="Seasons", course="AST101", lecture_num=3, threshold=0)
    sc.add("learning_goals", id="AST101-L04-ph", title="Telescopes", course="AST101", lecture_num=4, threshold=0)
    sc.add("lectures", course="AST101", lecture_num=3, title="Seasons", slides_total=6,
           narration_s_json=json.dumps({str(i): 30.0 for i in range(1, 7)}), narration_total_s=180.0)
    sc.add("lectures", course="AST101", lecture_num=4, title="Telescopes", slides_total=5)

    # --- S1, the morning visit -------------------------------------------------
    sc.event(1, "page_view", at(10, 0), vw=1280, vh=800)
    sc.turn(1, at(10, 0, 5), 3, reply_after_s=9)                       # reply 10:00:14, done 10:00:16
    sc.event(1, "deck_open", at(10, 0, 20), lecture=3, slide=1, page="d", path="/slides/AST101/lecture-03/index.html",
             context="chat", has_audio=True, slides_total=6)
    sc.event(1, "slide_view", at(10, 0, 40), lecture=3, slide=2, page="d", value_ms=20000, prev_slide=1, how="next")
    sc.event(1, "slide_view", at(10, 1, 20), lecture=3, slide=3, page="d", value_ms=40000, prev_slide=2, how="next")
    sc.event(1, "narration_play", at(10, 1, 25), lecture=3, slide=3, page="d", value_ms=0, position_s=0)
    sc.event(1, "deck_heartbeat", at(10, 2, 20), lecture=3, slide=3, page="d", value_ms=55000, narration_playing=True)
    sc.event(1, "narration_ended", at(10, 3, 55), lecture=3, slide=3, page="d", value_ms=150000, position_s=150)
    sc.turn(1, at(10, 5, 0), 3)                                         # think 10:05:00 - 10:00:16 = 284 s
    sc.turn(1, at(10, 5, 40), 3)                                        # think 10:05:40 - 10:05:12 = 28 s
    sc.event(1, "page_hide", at(10, 6, 30), value_ms=390000)
    # --- S1, the afternoon visit -----------------------------------------------
    sc.event(1, "page_view", at(14, 0), page="q", visit="w")
    sc.turn(1, at(14, 0, 0), 4, goal="AST101-L04-ph", page="q")
    sc.turn(1, at(14, 0, 30), 3, page="q")                              # a switch, lecture 4 to 3
    sc.event(1, "deck_open", at(14, 1, 0), lecture=4, slide=1, page="e", visit="w",
             path="/slides/AST101/lecture-04/index.html", context="lecture", has_audio=False, slides_total=5)
    sc.event(1, "slide_view", at(14, 2, 0), lecture=4, slide=2, page="e", visit="w", value_ms=60000, prev_slide=1, how="next")
    sc.event(1, "deck_close", at(14, 3, 0), lecture=4, slide=2, page="e", visit="w", value_ms=50000,
             active_ms=110000, slides_seen=2)

    # --- S2: stayed after the reply, then flipped the deck -------------------
    sc.event(2, "page_view", at(9, 0))
    sc.turn(2, at(9, 0, 0), 3)
    for i in range(1, 16):
        sc.event(2, "page_heartbeat", at(9, i, 0), value_ms=60000)
    sc.event(2, "deck_open", at(9, 16, 0), lecture=3, slide=1, page="d", path="/slides/AST101/lecture-03/index.html",
             context="tab", has_audio=True, slides_total=6)
    for n in range(2, 7):
        sc.event(2, "slide_view", at(9, 16, n), lecture=3, slide=n, page="d", value_ms=1000, prev_slide=n - 1, how="next")
    sc.event(2, "deck_close", at(9, 16, 10), lecture=3, slide=6, page="d", value_ms=1000, active_ms=10000, slides_seen=6)

    # --- S3: a question never answered in the browser, a lost batch -----------
    sc.event(3, "page_view", at(20, 0))
    sc.turn(3, at(20, 0, 5), 3, with_events=False)
    sc.event(3, "question_sent", at(20, 0, 5), lecture=3, chars=18, kind="typed")
    sc.event(3, "click", at(20, 0, 30), skip_seq=1, target="send")       # seq 2 never arrived
    sc.event(3, "page_leave", at(20, 0, 40), value_ms=40000)
    return sc


@pytest.fixture(scope="module")
def store_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("analytics") / "store.sqlite"
    sc = build_script()
    con = store_mod.open_store(path)
    try:
        for name in store_mod.TABLE_NAMES:
            store_mod.write_rows(con, name, sc.rows[name])
        store_mod.set_meta(con, "last_export", "2026-10-02T09:00:00+00:00")
    finally:
        con.close()
    return path


@pytest.fixture
def store(store_path):
    s = Store(store_path)
    yield s
    s.close()


ALL = Window()


# ---------------------------------------------------------------------------
# Visits
# ---------------------------------------------------------------------------

def test_visits_split_on_the_idle_gap_and_measure_on_screen_time(store):
    visits = visits_mod.visits_for(store, [1, 2, 3, 4], ALL, T)
    s1 = visits[1]
    assert len(s1) == 2
    morning, afternoon = s1
    assert (morning.start, morning.end) == (at(10, 0), at(10, 6, 30))
    assert morning.questions == 3 and morning.replies == 3 and morning.pages == 1
    # Every signal holds for at most 75 s: the 95 s between the deck heartbeat
    # and the narration's end leaves a 20 s hole; the reply rows (server side)
    # cut a hold but the next browser event resumes it.
    assert 355 <= morning.active_s <= 375, morning.active_s
    assert morning.deck_s == pytest.approx(260, abs=1)
    assert morning.narration_s == pytest.approx(150)
    assert morning.lectures == {("AST101", 3)} and morning.slides_seen == 3
    assert afternoon.questions == 2 and afternoon.lectures == {("AST101", 3), ("AST101", 4)}
    assert afternoon.deck_s == pytest.approx(120, abs=1)       # open 14:01 to close 14:03
    assert visits[4] == []
    assert visits[3][0].lost_batches == 1 and visits[1][0].lost_batches == 0


def test_a_student_who_stays_keeps_being_active_through_the_heartbeats(store):
    (visit,) = visits_mod.visits_for(store, [2], ALL, T)[2]
    assert visit.active_s == pytest.approx(16 * 60 + 10, abs=5)     # 09:00 to the deck's close at 09:16:10


def test_an_unfinished_clip_is_credited_to_its_last_heartbeat():
    from tutor.analytics.base import Signal

    def ev(name, ts, **detail):
        return Signal(ts=ts, source="event", name=name, student_id=1, value_ms=detail.pop("value_ms", None), detail=detail)

    played = visits_mod.played_seconds([
        ev("narration_play", at(1, 0)), ev("deck_heartbeat", at(1, 1), narration_playing=True),
        ev("deck_heartbeat", at(1, 2), narration_playing=True), ev("deck_heartbeat", at(1, 3), narration_playing=False),
    ], "narration_play", ("narration_pause", "narration_seek", "narration_ended"), playing_flag="narration_playing")
    assert played == 120


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------

def test_lecture_use_reports_coverage_dwell_and_narration(store):
    uses = lectures_mod.lecture_use(store, [1, 2], ALL)
    l3 = uses[(1, "AST101", 3)]
    assert l3.slides_seen == 3 and l3.coverage_pct == 50 and l3.covered(T)
    assert l3.dwell_s[1] == 20 and l3.dwell_s[2] == 40 and l3.median_dwell_s == 30 and l3.reading(T) == "read"
    assert l3.narration_played_s == 150 and l3.listened_pct == pytest.approx(83.3, abs=0.1) and l3.listened(T)
    assert dict(l3.contexts) == {"chat": 1} and l3.opens == 1
    l4 = uses[(1, "AST101", 4)]
    assert l4.slides_seen == 2 and l4.coverage_pct == 40 and not l4.covered(T)
    assert l4.dwell_s == {1: 60, 2: 50} and l4.listened_pct is None and dict(l4.contexts) == {"lecture": 1}
    flipped = uses[(2, "AST101", 3)]
    assert flipped.coverage_pct == 100 and flipped.reading(T) == "flipped" and not flipped.listened(T)
    table = lectures_mod.class_lecture_table(uses, [1, 2], "AST101", T)
    row3 = next(r for r in table if r["lecture"] == 3)
    assert (row3["opened"], row3["covered"], row3["listened"], row3["read"], row3["flipped"]) == (2, 2, 1, 1, 1)
    matrix = lectures_mod.student_lecture_matrix(uses, [1, 2], "AST101", {1: "S1", 2: "S2"}, [3, 4])
    assert matrix[0] == {"student": "S1", "L03": "50%/83%", "L04": "40%"}
    assert matrix[1]["L04"] == ""


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def test_question_gaps_think_time_and_buckets(store):
    gaps = questions_mod.question_gaps(store, [1, 3], ALL, T)
    q1, q2, q3, q4, q5 = gaps[1]
    assert q1.gap_s is None and q1.think_s is None and q1.reply_wait_s == 9
    assert q2.gap_s == 295 and q2.think_s == 284 and q2.bucket == "reading"
    assert q3.think_s == 28 and q3.bucket == "follow_up"
    assert q4.bucket == "new_visit" and q5.bucket == "follow_up"
    summary = questions_mod.summarize_questions(gaps[1], questions_mod.abandoned_turns(store, [1], ALL)[1])
    assert summary["questions"] == 5 and summary["follow_up_rate"] == 0.5 and summary["abandoned_rate"] == 0
    assert summary["median_reply_wait_s"] == 10
    s3 = questions_mod.abandoned_turns(store, [3], ALL)[3]
    assert (s3["sent"], s3["abandoned"]) == (1, 1)


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

def test_turns_are_labeled_with_the_goal_and_switches_found(store):
    turns = topics_mod.turns_labeled(store, [1], ALL)[1]
    assert [t.label(store.goal_titles()) for t in turns] == [
        "AST101-L03 (Seasons)", "AST101-L03 (Seasons)", "AST101-L03 (Seasons)",
        "AST101-L04 (Telescopes)", "AST101-L03 (Seasons)"]
    assert turns[0].turn_id == "t1100005" and turns[0].verdict == "allow" and turns[0].reply_ts == at(10, 0, 14)
    sw = topics_mod.switches(turns, T, store.goal_titles())
    assert len(sw) == 1
    assert (sw[0]["kind"], sw[0]["from"], sw[0]["to"]) == ("lecture", "AST101-L04 (Telescopes)", "AST101-L03 (Seasons)")
    assert sw[0]["minutes_on_previous"] == 0.5


def test_disengagement_points_say_how_each_visit_ended(store):
    points = topics_mod.disengagement_points(store, [1, 2, 3], ALL, T)
    morning, afternoon = points[1]
    assert morning.label == "AST101-L03 (Seasons)" and morning.reply_ts == at(10, 5, 50)
    assert morning.seconds_after_reply == 40 and morning.after == "reading" and morning.left
    assert morning.next_visit == at(14, 0) and morning.hours_until_next == pytest.approx(3.89, abs=0.01)
    assert morning.next_label == "AST101-L04 (Telescopes)"
    assert afternoon.after == "deck" and afternoon.seconds_after_reply == 140 and afternoon.left
    assert afternoon.next_visit is None
    (stayed,) = points[2]
    assert stayed.after == "deck" and stayed.seconds_after_reply == 16 * 60 and not stayed.left
    (unanswered,) = points[3]
    assert unanswered.reply_ts == at(20, 0, 15) and unanswered.after == "reading"


def test_the_timeline_tells_a_day_in_words(store):
    rows = topics_mod.timeline(store, 1, DAY.date(), T)
    assert rows[0] == {"visit": 1, "time": "10:00:00", "what": "page_view", "where": "/chat", "slide": "",
                       "turn": "", "note": "vh=800, vw=1280"}
    questions = [r for r in rows if r["what"] == "question"]
    assert questions[0]["note"] == "AST101-L03 (Seasons)" and {r["visit"] for r in rows} == {1, 2}
    slide = next(r for r in rows if r["what"] == "slide_view")
    assert slide["note"] == "slide 2, 20 s on slide 1" and slide["where"] == "/slides/AST101/lecture-03/index.html"


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def test_student_metrics_class_summary_and_comparison(store):
    table = cohort.student_metrics(store, [1, 2, 3, 4], ALL, T)
    m1 = table[1].metrics
    assert (m1["active_days"], m1["visits"], m1["questions"], m1["slides_seen"]) == (1, 2, 5, 5)
    assert (m1["lectures_covered"], m1["lectures_listened"], m1["switches_per_visit"]) == (1, 1, 0.5)
    assert m1["narration_min"] == 2.5 and m1["follow_up_rate"] == 0.5
    assert table[4].metrics == {**cohort.ZERO, "median_think_s": None, "follow_up_rate": None,
                                "abandoned_rate": None, "active_min": 0.0, "deck_min": 0.0, "narration_min": 0.0}
    a = cohort.class_summary(store, 1, ALL, T)
    assert (a.n_enrolled, a.n_active, a.never_active) == (2, 2, [])
    questions = next(line for line in a.summary if line["key"] == "questions")
    assert (questions["median"], questions["q1"], questions["q3"]) == (3, 2, 4)
    b = cohort.class_summary(store, 2, ALL, T)
    assert (b.n_enrolled, b.n_active, b.never_active) == (2, 1, ["S4"])
    comparison = cohort.compare_classes(store, [1, 2], ALL, T)
    first = comparison["table"]
    assert first[0] == {"metric": "enrolled", "AST 101 A (Au26)": 2, "AST 101 B (Au26)": 2}
    assert first[1] == {"metric": "active", "AST 101 A (Au26)": 2, "AST 101 B (Au26)": 1}
    questions_row = next(r for r in first if r["metric"] == "questions (median)")
    assert questions_row == {"metric": "questions (median)", "AST 101 A (Au26)": 3, "AST 101 B (Au26)": 0.5}
    report = cohort.student_report(store, 1, ALL, T)
    assert report["percentiles"][0]["class"] == "AST 101 A (Au26)"
    assert report["percentiles"][0]["questions"] == "5 (p100)"
    weeks = cohort.weekly_trend(store, [1, 2, 3, 4], ALL, T)
    assert len(weeks) == 1 and weeks[0]["week"] == "2026-W40" and weeks[0]["active_students"] == 3


def test_windows_and_student_resolution(store):
    assert window_days(7, now=at(12, 0, day=DAY + datetime.timedelta(days=10))).since == at(12, 0, day=DAY + datetime.timedelta(days=3))
    w = window_days(None, "2026-10-01", "2026-10-02")
    assert w.contains(at(23, 59)) and not w.contains(at(0, 0, day=DAY + datetime.timedelta(days=1)))
    assert store.resolve_student("S2") == 2 and store.resolve_student("3") == 3
    with pytest.raises(LookupError):
        store.resolve_student("S77")
    with pytest.raises(LookupError):
        store.resolve_student("someone@example.edu")       # no emails in this store
    assert store.student_label(1) == "S1" and store.class_label(1) == "AST 101 A (Au26)"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

COMMANDS = (
    ["visits", "--student", "S1"],
    ["visits", "--class", "1"],
    ["lectures", "--class", "1"],
    ["lectures", "--student", "S1", "--lecture", "3"],
    ["questions", "--student", "S1"],
    ["questions", "--class", "2"],
    ["disengagement", "--class", "1"],
    ["timeline", "--student", "S1", "--day", "2026-10-01"],
    ["student", "S1"],
    ["class"],
    ["class", "1", "--days", "3650"],
    ["compare", "1", "2"],
    ["daily", "--class", "1", "--since", "2026-09-29", "--until", "2026-10-03", "--tz", "UTC"],
    ["daily", "--class-id", "2", "--days", "14", "--instructors"],
)


@pytest.mark.parametrize("fmt", ["md", "csv", "json", "html"])
@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: " ".join(c))
def test_every_command_runs_in_every_format(store_path, capsys, command, fmt):
    assert main([*command, "--db", str(store_path), "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert out.strip()
    if fmt == "json":
        assert json.loads(out)["title"]
    if fmt == "html":
        assert out.startswith("<!DOCTYPE html>")
        assert "<table>" in out or "nothing in this window" in out or 'id="data"' in out


def test_daily_totals_bucket_the_day_on_the_given_calendar(store):
    from zoneinfo import ZoneInfo
    one_day = window_days(None, "2026-10-01", "2026-10-02")
    table = daily.daily_table(store, store.roster(1), one_day, T, ZoneInfo("UTC"))
    assert table.days == ["2026-10-01"]
    assert table.metric("active_students").series[0].values == [2]          # S1 and S2
    questions = table.metric("questions")
    assert [s.name for s in questions.series] == ["AST101"]
    assert questions.series[0].values == [6]                               # S1 asked five, S2 one
    assert table.metric("visits").series[0].values == [3]                  # S1 twice, S2 once
    assert table.metric("narration_min").series[0].values == [2.5]         # S1's 150 s
    assert table.metric("cost_usd").total() == 0.06
    assert table.distinct_active() == 2
    rows = table.as_rows()
    assert rows[0]["day"] == "2026-10-01" and rows[0]["questions"] == 6    # one course: one column
    assert table.columns()[:2] == ["day", "active_students"]
    assert "feedback thumbs up" in table.columns()                           # two series: one each
    # S3's class, S4 never came: one student active, one question, the reply never seen
    other = daily.daily_table(store, store.roster(2), one_day, T, ZoneInfo("UTC"))
    assert other.metric("active_students").series[0].values == [1]
    assert other.metric("questions").series[0].values == [1]


def test_day_span_follows_the_calendar_of_the_time_zone():
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    # An open start counts back from the last day; the UTC window starts at local midnight.
    days, utc = daily.day_span(Window(None, datetime.datetime(2026, 10, 2, 7)), la, days=3)
    assert [d.isoformat() for d in days] == ["2026-09-29", "2026-09-30", "2026-10-01"]
    assert utc.since == datetime.datetime(2026, 9, 29, 7) and utc.until == datetime.datetime(2026, 10, 2, 7)
    # An open end is today on that calendar.
    now = datetime.datetime(2026, 10, 2, 3, tzinfo=datetime.timezone.utc)      # still Oct 1 in Seattle
    days, _ = daily.day_span(Window(), la, days=2, now=now)
    assert [d.isoformat() for d in days] == ["2026-09-30", "2026-10-01"]
    # The scripted morning in UTC is the evening before in Seattle.
    days, _ = daily.day_span(window_days(None, "2026-10-01", "2026-10-02"), la, days=1)
    assert [d.isoformat() for d in days] == ["2026-09-30", "2026-10-01"]


def test_the_daily_page_embeds_the_table_and_writes_to_a_file(store_path, tmp_path, capsys):
    out = tmp_path / "daily.html"
    assert main(["daily", "--class-id", "1", "--since", "2026-10-01", "--until", "2026-10-02", "--tz", "UTC",
                 "--db", str(store_path), "--format", "html", "--out", str(out)]) == 0
    assert "wrote" in capsys.readouterr().err
    page = out.read_text()
    assert page.startswith("<!DOCTYPE html>") and "<script src" not in page      # self-contained
    start = page.index('<script id="data" type="application/json">') + len('<script id="data" type="application/json">')
    data = json.loads(page[start:page.index("</script>", start)])
    assert data["days"] == ["2026-10-01"] and data["students"] == 2
    assert [m["key"] for m in data["metrics"]] == list(daily.METRIC_KEYS)
    assert data["metrics"][0]["series"][0]["values"] == [2]
    assert "Daily use: AST 101 A (Au26)" in page and "@" not in json.dumps(data)      # ids, never addresses
    main(["daily", "--class", "1", "--since", "2026-10-01", "--until", "2026-10-02", "--tz", "UTC",
          "--db", str(store_path), "--format", "md"])
    md = capsys.readouterr().out
    assert "## Totals" in md and "## Per day" in md and "| 2026-10-01 |" in md


def test_the_reports_say_what_they_used(store_path, capsys):
    main(["class", "1", "--db", str(store_path), "--idle-gap", "45", "--listened", "70"])
    out = capsys.readouterr().out
    assert "# Class: AST 101 A (Au26)" in out
    assert "visit idle gap 45 min" in out and "listened at 70%" in out
    assert "| S1 |" in out and "never active" in out
    main(["compare", "1", "2", "--db", str(store_path), "--format", "csv"])
    out = capsys.readouterr().out
    assert out.startswith("# Side by side") and "questions (median),3,0.5" in out


def test_a_missing_store_or_student_is_a_clear_error(tmp_path, store_path, capsys):
    with pytest.raises(FileNotFoundError):
        main(["class", "--db", str(tmp_path / "nope.sqlite")])
    with pytest.raises(LookupError):
        main(["student", "S99", "--db", str(store_path)])
    assert main(["timeline", "--student", "S1", "--day", "yesterday", "--db", str(store_path)]) == 2


def test_the_store_file_is_opened_read_only(store_path):
    s = Store(store_path)
    with pytest.raises(sqlite3.OperationalError):
        s.con.execute("DELETE FROM students")
    s.close()
