"""Plan step 5.7 (findings B27, A16, A18): dead code and small consolidations.

B27: dead or duplicated code. The lecture-wide progress helpers had no caller
outside tests, the script.md exercise scraper could not run in production,
state.apply_verdict had no caller, and the three report scripts each carried
a copy of the Resend send, the recipient fallback, and the window helper.
A16: the exam bank's cache held one parse for the life of the process, so a
bank missing on the first call read as empty until a restart. A18: tool
dispatch trusted the model's top_k (unbounded) and course (outside the enum,
into file paths); a missing ChromaDB store was created empty, and every miss
was swallowed without a word.
Done when: grep finds no callers of the deleted names (the first test).
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from tutor import mail
from tutor.agent import mastery, tools
from tutor.agent.turn_stages import actor_loop
from tutor.db.models import Student
from tests.helpers import make_session

ROOT = pathlib.Path(__file__).resolve().parent.parent

DELETED = {
    "unlock_goals_for_lecture", "master_goals_for_lecture",          # progress.py
    "_scrape_from_scripts", "_infer_topic", "_TOPIC_MAP", "_SI_RE", "_LC_RE",   # exercises.py
    "apply_verdict",                                                 # state.py
}


# ---------------------------------------------------------------------------
# B27: no callers of the deleted names
# ---------------------------------------------------------------------------

def _uses(path: pathlib.Path) -> list[str]:
    """Every use of a deleted name in a file: a name, an attribute, a
    definition, or an import. Comments and strings do not count."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        name = (node.id if isinstance(node, ast.Name) else
                node.attr if isinstance(node, ast.Attribute) else
                node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else None)
        if name in DELETED:
            found.append(f"{path.name}:{node.lineno} {name}")
        if isinstance(node, ast.ImportFrom):
            found += [f"{path.name}:{node.lineno} {a.name}" for a in node.names if a.name in DELETED]
    return found


def test_no_code_uses_the_deleted_names():
    offenders = []
    for top in ("tutor", "scripts", "pipeline", "tests"):
        for path in sorted((ROOT / top).rglob("*.py")):
            offenders += [f"{top}/{hit}" for hit in _uses(path)]
    assert offenders == []


def test_the_scan_would_catch_one(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from tutor.agent.progress import unlock_goals_for_lecture\n"
                     "state.apply_verdict(db, ev, 'allow')\n"
                     "# apply_verdict in a comment is not a caller\n")
    assert _uses(probe) == ["probe.py:1 unlock_goals_for_lecture", "probe.py:2 apply_verdict"]


def test_a_missing_exercise_bank_is_logged_not_scraped(tmp_path, tutor_log):
    from tutor.agent import exercises

    (tmp_path / "output" / "AST101" / "lecture-01").mkdir(parents=True)
    (tmp_path / "output" / "AST101" / "lecture-01" / "script.md").write_text(
        "**Student Interaction:**\n\nWrite a function that sums an array of ints.\n")
    exercises.load_exercise_bank.cache_clear()
    try:
        with patch.object(exercises, "_exercises_json_path", return_value=tmp_path / "curriculum" / "exercises.json"):
            assert exercises.load_exercise_bank() == []
    finally:
        exercises.load_exercise_bank.cache_clear()
    assert "no exercise bank at" in tutor_log.text


# ---------------------------------------------------------------------------
# B27: one mail helper and one window for the three report scripts
# ---------------------------------------------------------------------------

def test_the_report_scripts_share_the_mail_helper():
    for name in ("daily_report.py", "feedback_report.py", "instructor_report.py"):
        text = (ROOT / "scripts" / name).read_text()
        assert "import resend" not in text and 'getenv("RESEND_API_KEY"' not in text, name
        assert "mail.send_report(" in text, name
        assert "report_window(" in text, name


@pytest.fixture
def sent(monkeypatch):
    """A fake resend module: the payloads it was asked to send."""
    box: list[dict] = []
    fake = types.ModuleType("resend")
    fake.Emails = types.SimpleNamespace(send=lambda payload: box.append(payload))
    monkeypatch.setitem(sys.modules, "resend", fake)
    for key in ("RESEND_API_KEY", "REPORT_EMAIL", "ADMIN_EMAILS", "FROM_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    return box


def test_report_recipient_falls_back_in_order(sent, monkeypatch):
    assert mail.report_recipient(None) is None
    monkeypatch.setenv("ADMIN_EMAILS", " first@example.edu , second@example.edu")
    assert mail.report_recipient(None) == "first@example.edu"
    monkeypatch.setenv("REPORT_EMAIL", " reports@example.edu ")
    assert mail.report_recipient("") == "reports@example.edu"
    assert mail.report_recipient(" me@example.edu ") == "me@example.edu"


def test_send_report_prints_without_a_key(sent, capsys):
    assert mail.send_report("me@example.edu", "S", text="the body", html="<p>", tag="t") is False
    out = capsys.readouterr().out
    assert "t: no RESEND_API_KEY" in out and "the body" in out
    assert sent == []


def test_send_report_needs_a_recipient(sent, monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    assert mail.send_report(None, "S", text="x", html="x", tag="t") is False
    assert "t: no recipient" in capsys.readouterr().out
    assert sent == []


def test_send_report_sends_text_and_html(sent, monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("FROM_EMAIL", "tutor@example.edu")
    assert mail.send_report("me@example.edu", "Subject", text="plain", html="<b>rich</b>", tag="t") is True
    assert sent == [{"from": "tutor@example.edu", "to": "me@example.edu", "subject": "Subject",
                     "html": "<b>rich</b>", "text": "plain"}]
    assert "t: emailed me@example.edu" in capsys.readouterr().out


def test_send_report_blind_copies_and_attaches(sent, monkeypatch, capsys):
    import base64

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    assert mail.send_report("teach@example.edu", "S", text="x", html="x", tag="t",
                            bcc=" admin@example.edu ", attachments=[("a.html", b"<!DOCTYPE html>")]) is True
    assert sent[-1]["bcc"] == "admin@example.edu"
    assert sent[-1]["attachments"] == [{"filename": "a.html",
                                        "content": base64.b64encode(b"<!DOCTYPE html>").decode()}]
    assert "t: emailed teach@example.edu (copy to admin@example.edu)" in capsys.readouterr().out
    # The admin's own email carries no copy of itself; a blank bcc is none.
    mail.send_report("Admin@example.edu", "S", text="x", html="x", tag="t", bcc="admin@example.edu")
    mail.send_report("me@example.edu", "S", text="x", html="x", tag="t", bcc="")
    assert "bcc" not in sent[-1] and "bcc" not in sent[-2] and "attachments" not in sent[-1]


def test_a_failed_send_is_reported_not_raised(sent, monkeypatch, capsys):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    def boom(payload):
        raise RuntimeError("mail outage")

    monkeypatch.setattr(sys.modules["resend"].Emails, "send", boom)
    assert mail.send_report("me@example.edu", "S", text="x", html="x", tag="t") is False
    assert "t: email to me@example.edu failed (mail outage)" in capsys.readouterr().out


def test_the_daily_digest_goes_through_the_helper(sent, monkeypatch):
    import scripts.daily_report as dr

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("REPORT_EMAIL", "prof@example.edu")
    dr._email_digest(None, "Fri Sep 25", "# Digest <1>", 3)
    [payload] = sent
    assert payload["to"] == "prof@example.edu"
    assert payload["subject"] == "Astra daily report: Fri Sep 25 (3 active)"
    assert payload["text"] == "# Digest <1>" and "# Digest &lt;1&gt;</pre>" in payload["html"]


def test_report_window():
    from tutor.utctime import local_day, report_window

    assert report_window("2026-09-25", 24) == local_day("2026-09-25")
    fixed = datetime.datetime(2026, 9, 26, 12, 0)
    with patch("tutor.utctime.utcnow", return_value=fixed):
        assert report_window(None, 36) == (fixed - datetime.timedelta(hours=36), fixed)
        import scripts.instructor_report as ir

        assert ir._window(7) == (fixed - datetime.timedelta(days=7), fixed)


# ---------------------------------------------------------------------------
# A16: the exam bank is parsed once per version of the file
# ---------------------------------------------------------------------------

@pytest.fixture
def bank(tmp_path, monkeypatch):
    path = tmp_path / "mastery_exams.json"
    monkeypatch.setattr(mastery, "_EXAM_BANK_PATH", path)
    mastery._parse_exam_bank.cache_clear()
    yield path
    mastery._parse_exam_bank.cache_clear()


def _bump(path: pathlib.Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_the_bank_is_parsed_once_per_version(bank):
    bank.write_text('{"G1": {"questions": []}}')
    for _ in range(3):
        assert set(mastery._load_exam_bank()) == {"G1"}
    info = mastery._parse_exam_bank.cache_info()
    assert (info.misses, info.hits) == (1, 2)
    bank.write_text('{"G1": {}, "G2": {}}')
    _bump(bank)
    assert set(mastery._load_exam_bank()) == {"G1", "G2"}   # a new bank without a restart


def test_a_bank_missing_at_first_is_found_later(bank):
    assert mastery._load_exam_bank() == {}
    assert mastery.load_exam_questions("G1") is None
    bank.write_text('{"G1": {"questions": [{"question": "What is parallax?"}]}}')
    assert mastery.load_exam_questions("G1") == {"questions": [{"question": "What is parallax?"}]}


def test_an_unreadable_bank_warns_once_per_version(bank, tutor_log):
    bank.write_text("{not json")
    assert mastery._load_exam_bank() == {} and mastery._load_exam_bank() == {}
    assert mastery._parse_exam_bank.cache_info().misses == 1          # not re-read per call
    assert tutor_log.text.count("Could not load mastery_exams.json") == 1


# ---------------------------------------------------------------------------
# A18: tool inputs are clamped and validated
# ---------------------------------------------------------------------------

class _Collection:
    def __init__(self, size=50):
        self.size = size
        self.asked: list[int] = []

    def count(self):
        return self.size

    def query(self, query_embeddings, n_results, include):
        self.asked.append(n_results)
        return {"documents": [[f"chunk {i}" for i in range(n_results)]],
                "metadatas": [[{"source_file": "x.pdf"} for _ in range(n_results)]],
                "distances": [[0.1 + i / 100 for i in range(n_results)]]}


@pytest.mark.parametrize("asked,expected", [(1000, tools.TOP_K_MAX), (0, 1), (-4, 1), ("lots", 5), (3, 3)])
def test_find_topic_clamps_top_k(asked, expected):
    coll = _Collection()
    with patch.object(tools, "_collection", return_value=coll), \
         patch.object(tools, "_embed_query", return_value=[[0.0]]):
        out = tools.dispatch_tool("find_topic", {"query": "parallax", "top_k": asked})
    assert len(out["results"]) == expected
    assert coll.asked and max(coll.asked) == expected


@pytest.mark.parametrize("value,expected", [
    ("AST102", "AST102"), ("ast102", "AST102"), (" AST 102 ", "AST102"), ("ast 101", "AST101"),
    (None, "D"), ("", "D"), ("344", "D"), ("../../etc", "D"), ("AST101/../AST102", "D"),
])
def test_valid_course(value, expected):
    assert tools.valid_course(value, "D") == expected


def test_dispatch_never_passes_an_unknown_course():
    seen = []
    with patch.object(tools, "get_lecture_outline",
                      side_effect=lambda course, lecture_num: seen.append(course) or {}):
        tools.dispatch_tool("get_lecture_outline", {"course": "../../etc", "lecture_num": 1})
        tools.dispatch_tool("get_lecture_outline", {"course": "AST 102", "lecture_num": 1})
    assert seen == ["AST101", "AST102"]


def test_the_turn_fills_in_its_own_course():
    given = {"query": "galaxies"}
    assert actor_loop.with_turn_course(given, "AST102") == {"query": "galaxies", "course": "AST102"}
    assert given == {"query": "galaxies"}                                    # not mutated
    assert actor_loop.with_turn_course({"course": "AST101"}, "AST102")["course"] == "AST101"
    assert actor_loop.with_turn_course({"course": "bogus"}, "AST102")["course"] == "AST102"
    assert actor_loop.with_turn_course(None, "AST101") == {"course": "AST101"}


def test_a_ast102_turn_searches_ast102_when_the_model_names_no_course():
    """End to end: the model's find_topic call carries no course."""
    from tutor.agent import turn as agent
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.policy import Envelope, Intent
    from tutor.agent.preclassify import Classification
    from tutor.agent.strategist import NO_MOVE

    db = make_session()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.commit()
    agent._INFLIGHT.clear()

    use = types.SimpleNamespace(type="tool_use", name="find_topic", id="tu_1", input={"query": "galaxies"})
    script = [([], "tool_use", [use]), (["Galaxies hold billions of stars."], "end_turn", [])]
    calls = {"n": 0}

    async def rounds(system, messages, tools_):
        chunks, stop, content = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for c in chunks:
            yield ("chunk", c)
        final = MagicMock(stop_reason=stop, content=list(content), usage=None, model=None)
        yield ("final", final)

    bundle = EnvelopeBundle(envelope=Envelope(max_rung=5, goals=["G1"], exam_lockdown=[], must_ground=True,
                                              allow_exercise=True, escalate_review=False, reason="band"),
                            goal_titles={}, gate_goal=None)

    async def go():
        return [e async for e in agent.run_turn(student=db.get(Student, 1), db=db, user_message="galaxies?",
                                                lecture_num=None, course="AST102")]

    with patch("tutor.agent.preclassify.classify", return_value=Classification(intent=Intent.QUESTION)), \
         patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE), \
         patch("tutor.agent.mastery.get_active_exam", return_value=None), \
         patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
         patch("tutor.agent.turn_stages.actor_client.stream_round", rounds), \
         patch("tutor.agent.tools.dispatch_tool", return_value={"results": []}) as dispatched, \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        asyncio.run(go())
    db.close()
    name, tool_input = dispatched.call_args.args
    assert name == "find_topic" and tool_input["course"] == "AST102"


def test_a_missing_store_is_not_created(tmp_path, tutor_log):
    missing = tmp_path / "chromadb"
    tools._chroma_client.cache_clear()
    try:
        with patch.object(tools, "get_config",
                          return_value={"chroma_dir": missing, "output_dir": tmp_path / "output"}):
            out = tools.find_topic("what is parallax?")
    finally:
        tools._chroma_client.cache_clear()
    assert out["results"] == [] and out["slide_ref"] is None and out["error"]
    assert not missing.exists()
    assert f"no knowledge base at {missing}" in tutor_log.text


def test_the_health_check_does_not_create_one_either(tmp_path):
    from tutor import health

    empty = tmp_path / "chromadb"
    empty.mkdir()                                  # a folder, no store (a failed seed)
    with patch.object(health, "get_config", return_value={"chroma_dir": empty}):
        row = health.check_chromadb()
    assert row.status == "missing" and not row.ok
    assert list(empty.iterdir()) == []


def test_zero_hits_are_logged_without_the_query(tutor_log):
    def collection(name):
        if name in ("code", "forum"):
            raise RuntimeError("not indexed")
        return _Collection(size=0)

    with patch.object(tools, "_collection", side_effect=collection):
        out = tools.find_topic("my secret question about parallax")
    assert out["results"] == []
    assert f"no hits across {len(tools._SEARCH_COLLECTIONS)} collections (2 failed)" in tutor_log.text
    assert "secret question" not in tutor_log.text
