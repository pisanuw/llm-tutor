"""Session 21 (S30 / Task 7): admin supervision surfaces.

Covers:
- the supervision tab renders every TurnEvent field somewhere
- the explain-mode toggle round-trips through the Setting table and is read
  back by the envelope assembler
- the conduct restriction set/clear button round-trips through Student.restricted
"""
from __future__ import annotations

import html
import re

import json

import pytest

from tutor.agent.envelope import EXPLAIN_MODE_SETTING, _explain_mode_on
from tutor.db.models import Setting, Student, TurnEvent
from tests.helpers import make_session


@pytest.fixture
def db():
    """No students: each test adds the ones it needs."""
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def admin_client(admin_client, db):
    """The shared admin client (tests/conftest.py) and its database."""
    return admin_client, db


def _student(db, email="s@example.edu") -> Student:
    s = Student(email=email, name="Test")
    db.add(s)
    db.flush()
    return s


# ---------------------------------------------------------------------------
# Turn-event drill-down shows every field
# ---------------------------------------------------------------------------

def test_supervision_tab_renders_every_field(admin_client):
    client, db = admin_client
    s = _student(db)
    db.add(TurnEvent(
        student_id=s.id, kind="chat", intent="QUESTION", rung_used=2,
        envelope_json=json.dumps({
            "max_rung": 3, "move": "ELICIT_PREDICTION", "exam_lockdown": [],
            "allow_exercise": True, "must_ground": True, "escalate_review": False,
            "reason": "envelope-reason-text", "goals": ["AST101-L01-x"],
        }),
        verdicts_json=json.dumps({"verdict": "allow", "eval_source": "test"}),
        det_check_json=json.dumps({"cut": False}),
        grounding_hit=True, model="claude-sonnet-test",
        in_tokens=111, out_tokens=22, cache_read_tokens=333,
        cost_usd=0.00123, latency_ms=850,
        goal_ids_json=json.dumps(["AST101-L01-x"]),
    ))
    db.commit()

    r = client.get("/admin?tab=supervision")
    assert r.status_code == 200
    # Each column by its header, from the event's own row (plan step 6.2a,
    # finding F16: "2" in the page was true of almost any page).
    row = _table_row(r.text, "s@example.edu")
    assert row["Student"] == "s@example.edu"
    assert row["Kind"] == "chat" and row["Intent"] == "QUESTION"
    assert row["Rung"] == "H2"                                   # the ceiling that was enforced
    assert row["Move"] == "ELICIT_PREDICTION"
    assert row["Verdict"] == "allow (test)"                      # verdict, then eval source
    # "clean": the detector ran and removed nothing ({"cut": false} is not a cut)
    assert row["Detector"] == "clean" and row["Grnd"] == "✓"
    assert row["Model"] == "claude-sonnet-test"
    assert row["In/Out/Cache"] == "111/22/333"
    assert row["Cost"] == "$0.00123" and row["ms"] == "850"
    assert row["Goals"] == "AST101-L01-x"


def test_the_detector_column_shows_a_real_cut(admin_client):
    client, db = admin_client
    s = _student(db, "cut@example.edu")
    db.add(TurnEvent(student_id=s.id, kind="chat", intent="QUESTION", rung_used=4,
                     envelope_json=json.dumps({"max_rung": 4}),
                     verdicts_json=json.dumps({"verdict": "detector_cut", "detector_cut": True})))
    db.commit()
    row = _table_row(client.get("/admin?tab=supervision").text, "cut@example.edu")
    # The Verdict column is the judge's three: a cut reads "revise", because the
    # student did not get the draft the actor wrote. The cut itself is Detector.
    assert row["Detector"] == "cut" and row["Verdict"] == "revise"


def test_the_ceiling_cell_shows_the_band_it_moved_from(admin_client):
    """A floor or a cap moves the mastery-band ceiling, and the cell shows
    both numbers: that move is the contract, and it used to read "4/4"."""
    client, db = admin_client
    s = _student(db, "floored@example.edu")
    db.add(TurnEvent(student_id=s.id, kind="chat", intent="ATTEMPT", rung_used=4,
                     envelope_json=json.dumps({
                         "max_rung": 4, "rung_base": 2,
                         "reason": "mastery band ceiling H2; constructive code turn floors ceiling at H4"}),
                     verdicts_json=json.dumps({"verdict": "allow"})))
    db.commit()
    body = client.get("/admin?tab=supervision").text
    assert _table_row(body, "floored@example.edu")["Rung"] == "H2→H4"
    assert "constructive code turn floors ceiling at H4" in html.unescape(body)   # the hover


def test_a_turn_with_no_reply_says_the_detector_never_ran(admin_client):
    client, db = admin_client
    s = _student(db, "flight@example.edu")
    db.add(TurnEvent(student_id=s.id, kind="chat", intent="QUESTION", rung_used=5,
                     envelope_json=json.dumps({"max_rung": 5}),
                     verdicts_json=json.dumps({"verdict": "in_progress"})))
    db.commit()
    row = _table_row(client.get("/admin?tab=supervision").text, "flight@example.edu")
    assert row["Verdict"] == "running" and row["Detector"] == "not run"


# ---------------------------------------------------------------------------
# The kind filter: three rows in four are a supervisor call, not a turn
# ---------------------------------------------------------------------------

def _rows_by_kind(body: str) -> list[str]:
    """The Kind cell of every data row of the turn log, in order. Scoped by
    that table's own header: the tab holds other tables (the quota panel)."""
    head = body.index("In/Out/Cache")
    start = body.index("<tbody", head)
    body = body[start: body.index("</tbody>", start)]
    out = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, re.S):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                 for c in re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) > 2:
            out.append(cells[2])
        elif cells:
            out.append(cells[0])          # the empty-table row
    return out


@pytest.fixture
def ledger(admin_client):
    """One turn and the three supervisor calls it writes."""
    client, db = admin_client
    s = _student(db, "mix@example.edu")
    db.add(TurnEvent(student_id=s.id, kind="chat", intent="QUESTION", rung_used=5,
                     envelope_json=json.dumps({"max_rung": 5}),
                     verdicts_json=json.dumps({"verdict": "allow"})))
    for kind in ("classify", "strategist", "judge"):
        db.add(TurnEvent(student_id=s.id, kind=kind, model="haiku", in_tokens=10, out_tokens=2))
    db.commit()
    return client, db


def test_the_table_opens_on_the_turns(ledger):
    client, _db = ledger
    assert _rows_by_kind(client.get("/admin?tab=supervision").text) == ["chat"]


def test_every_kind_is_one_click_away_with_its_count(ledger):
    client, _db = ledger
    body = client.get("/admin?tab=supervision").text
    assert set(_rows_by_kind(body := client.get("/admin?tab=supervision&kind=all").text)) == {
        "chat", "classify", "strategist", "judge"}
    assert _rows_by_kind(client.get("/admin?tab=supervision&kind=classify").text) == ["classify"]
    # the filter names each kind and how many rows it holds
    assert "all kinds" in body and "chat" in body


def test_a_supervisor_call_says_the_columns_do_not_apply(ledger):
    client, _db = ledger
    row = _table_row(client.get("/admin?tab=supervision&kind=classify").text, "mix@example.edu")
    assert row["Intent"] == "—"
    assert row["Rung"] == "n/a" and row["Verdict"] == "n/a" and row["Detector"] == "n/a"


def test_an_unknown_or_absent_kind_opens_the_page_on_the_turns(ledger):
    client, _db = ledger
    for query in ("&kind=nonsense", "&kind=", "&kind=" + "x" * 200, ""):
        r = client.get(f"/admin?tab=supervision{query}")
        assert r.status_code in (200, 422), query
        if r.status_code == 200:
            assert _rows_by_kind(r.text) == ["chat"], query


def _table_row(body: str, needle: str) -> dict[str, str]:
    """{header: cell text} for the table row that contains ``needle``."""
    def cells(fragment: str, tag: str) -> list[str]:
        return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                for c in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", fragment, re.S)]

    start = body.rindex("<tr", 0, body.index(f">{needle}</td>"))       # a cell, not the datalist
    head = body.rindex("<thead", 0, start)
    headers = cells(body[head: body.index("</thead>", head)], "th")
    values = cells(body[start: body.index("</tr>", start)], "td")
    assert len(headers) == len(values), (headers, values)
    return dict(zip(headers, values))


def test_event_row_exposes_all_columns():
    from tutor.routers.admin import _event_row

    te = TurnEvent(
        student_id=1, kind="exercise", intent="ATTEMPT", rung_used=1,
        envelope_json=json.dumps({"max_rung": 1, "move": None, "exam_lockdown": ["G2"],
                                  "allow_exercise": False, "reason": "r"}),
        verdicts_json=json.dumps({"score": 1.0, "eval_source": "llm-opinion"}),
        det_check_json=json.dumps({"cut": True}),
        grounding_hit=None, model="m", in_tokens=1, out_tokens=2,
        cache_read_tokens=3, cost_usd=0.1, latency_ms=10,
        goal_ids_json=json.dumps(["G1"]),
    )
    row = _event_row(te, "a@b.com")
    assert row.email == "a@b.com"
    assert row.kind == "exercise"
    assert row.max_rung == 1
    assert row.exam_lockdown == ["G2"]
    assert row.allow_exercise is False
    assert row.eval_source == "llm-opinion"
    assert row.det_check == {"cut": True}
    assert row.goal_ids == ["G1"]


# ---------------------------------------------------------------------------
# explain-mode toggle round-trips
# ---------------------------------------------------------------------------

def test_explain_mode_toggle_round_trips(admin_client):
    client, db = admin_client

    r = client.post("/admin/settings/explain-mode", data={"value": "AST101"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = db.get(Setting, EXPLAIN_MODE_SETTING)
    assert row is not None and row.value == "AST101"
    # The envelope assembler reads it back, per course.
    assert _explain_mode_on(db, "AST101") is True
    assert _explain_mode_on(db, "AST102") is False

    # Turning it off round-trips too.
    client.post("/admin/settings/explain-mode", data={"value": ""}, follow_redirects=False)
    db.expire_all()
    assert _explain_mode_on(db, "AST101") is False


def test_explain_mode_rejects_bad_value(admin_client):
    client, db = admin_client
    client.post("/admin/settings/explain-mode", data={"value": "evil"}, follow_redirects=False)
    row = db.get(Setting, EXPLAIN_MODE_SETTING)
    assert row.value == ""  # coerced to off


# ---------------------------------------------------------------------------
# conduct restriction set / clear
# ---------------------------------------------------------------------------

def test_restriction_set_and_clear(admin_client):
    client, db = admin_client
    s = _student(db, "bad@example.edu")
    db.commit()

    client.post("/admin/restriction/set",
                data={"email": "bad@example.edu", "restricted": "on"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Student, s.id).restricted is True

    # The supervision page lists the restricted student with a Clear button.
    body = client.get("/admin?tab=supervision").text
    assert "bad@example.edu" in body and "Clear" in body

    client.post("/admin/restriction/set",
                data={"email": "bad@example.edu", "restricted": "off"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Student, s.id).restricted is False


def test_restriction_unknown_email_is_noop(admin_client):
    client, db = admin_client
    r = client.post("/admin/restriction/set",
                    data={"email": "ghost@example.edu", "restricted": "on"}, follow_redirects=False)
    assert r.status_code == 303  # no crash
