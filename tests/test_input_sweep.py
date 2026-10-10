"""Nothing a student types, pastes or puts in an address is a 500
(bulletproofing, Stage 1).

Every route a signed-in student or a visitor reaches gets each of its fields
replaced, one at a time, by values a paste or a hand-edited URL produces: a
100,000-character string, a NUL byte, an integer past 2^31, a negative, a
word where a number goes, a 300-digit number, a path with ``../``. Under
tests/postgres_strict.py the suite's SQLite refuses what Postgres refuses, so
a value only production would choke on fails here too. A route may refuse a
value (4xx) but must never answer 500 or email the operator.
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tutor import alerts
from tutor.db.models import LearningGoal, Message, Student, StudentProgress, WhitelistEntry
from tutor.utctime import utcnow
from tests.helpers import login_cookie, make_session

BAD = {
    "huge": "x" * 100_000,
    "nul": "a\x00b",
    "int_big": "3000000000",
    "neg": "-1",
    "zero": "0",
    "word": "abc",
    "float": "1.5",
    "exp": "1e9",
    "empty": "",
    "dots": "../../../etc/passwd",
    "digits": "9" * 300,
    "long_email": "a" * 300 + "@example.edu",
}
# An address is capped long before the app (uvicorn's request line limit),
# so a URL gets a long value it can still deliver.
URL_BAD = {**BAD, "huge": "x" * 4_000}
PROBE = {"id": "p1", "question": "What does a parallax measurement compare?", "rubric": "two positions six months apart"}
GRADED = {"score": 3, "feedback": "Right.", "usage": {"in_tokens": 10, "out_tokens": 5}}
HTML = {"accept": "text/html,application/xhtml+xml"}

# (path, the fields a working request sends)
FORMS = [
    ("/auth/magic-link", {"email": "ada@example.edu", "next": "/chat"}),
    ("/auth/magic-code", {"email": "ada@example.edu", "code": "123456", "next": "/chat"}),
    ("/chat/teachme", {"goal": "AST101-L01-daily-motion", "course": "AST101", "depth": "intro", "lecture_num": "1"}),
    ("/chat/message", {"message": "hi", "course": "AST101", "lecture_num": "1", "slide_num": "1",
                       "predicted_score": "2"}),
    ("/chat/feedback", {"message_id": "1", "text": "A reply.", "rating": "1", "comment": "wrong"}),
    ("/chat/tts", {"text": "A reply."}),
    ("/review/grade", {"goal_id": "G1", "probe_id": "p1", "answer": "the key", "confidence": "2"}),
    ("/review/grade", {"goal_id": "G1", "probe_id": "mc-1", "answer": "the key"}),
    ("/welcome", {"course": "AST101"}),
    ("/errors/report", {"ref": "ABC123"}),
]
JSON_BODIES = [
    ("/path/track", {"course": "AST101", "track_id": "AST101-lecture-order"}),
    ("/path/course", {"course": "AST101"}),
    ("/path/event", {"course": "AST101", "goal_id": "G1", "modality": "teach", "source": "student"}),
]
QUERIES = [
    ("/chat", {"course": "AST101"}),
    ("/chat/starters", {"course": "AST101"}),
    ("/chat/history", {"course": "AST101"}),
    ("/chat/progress", {"course": "AST101"}),
    ("/chat/running", {"course": "AST101"}),
    ("/path/next", {"course": "AST101", "n": "3"}),
    ("/path/current", {"course": "AST101"}),
    ("/track-picker", {"course": "AST101"}),
    ("/review", {"course": "AST101"}),
    ("/review/session", {"course": "AST101", "mode": "due"}),
    ("/review/nudge", {"course": "AST101"}),
    ("/auth/login", {"next": "/chat", "code": "1"}),
    ("/map", {"course": "AST101"}),
    ("/lectures", {"course": "AST101"}),
]
PATHS = [
    "/lectures/AST101/{v}",
    "/lectures/{v}/1",
    "/feedback/{v}",
    "/chat/message/{v}",
    "/auth/magic/{v}",
    "/slides/AST101/{v}/index.html",
    "/slides/{v}",
    "/book/{v}",
    "/explorables/{v}",
    "/lecture-audio/AST101/{v}",
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    import tutor.agent.turn as turn_mod
    from tutor.agent import mastery, vision
    from tutor.auth.deps import get_db
    from tutor.config import get_config
    from tutor.main import app
    from tutor.routers import auth as auth_mod

    # Real course directories, as production has: a long name only fails
    # once the path's parent exists.
    for course in ("AST101", "AST102"):
        deck = tmp_path / course / "lecture-01"
        deck.mkdir(parents=True)
        (deck / "index.html").write_text("<html><body>deck</body></html>")
    monkeypatch.setitem(get_config(), "output_dir", tmp_path)
    monkeypatch.setitem(get_config(), "lecture_audio_dir", tmp_path)

    db = make_session(
        Student(id=1, email="ada@example.edu", name="Ada"),
        WhitelistEntry(email="ada@example.edu", added_by="test"),
        WhitelistEntry(email="@example.edu", added_by="test"),          # a domain rule: long addresses get through
        Message(id=1, student_id=1, role="assistant", content="A reply.", course="AST101"),
        LearningGoal(id="G1", title="Parallax", description="", course="AST101", lecture_num=4),
    )
    db.add(StudentProgress(student_id=1, goal_id="G1", status="unlocked", mastery_score=0.7,
                           fsrs_stability=2.0, fsrs_difficulty=5.0, fsrs_reps=1,
                           fsrs_due=utcnow() - datetime.timedelta(hours=2),
                           last_evidence_at=utcnow() - datetime.timedelta(days=3)))
    db.commit()

    async def fake_run_turn(**kwargs):
        yield {"type": "done", "full_text": "ok"}

    sent: list[str] = []
    monkeypatch.setattr(turn_mod, "run_turn", fake_run_turn)
    monkeypatch.setattr(alerts, "send_alert", lambda key, subject, body, **kw: sent.append(key) or "queued")
    monkeypatch.setattr(vision, "transcribe_image", lambda data, media: {"ok": True, "code": "x"})
    monkeypatch.setattr(mastery, "grade_probe", lambda *a, **k: GRADED)
    monkeypatch.setattr(mastery, "_question_pool", lambda *a, **k: [PROBE])
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("session_token", login_cookie(db, 1))

    def reset():
        auth_mod._magic_link_log.clear()
        auth_mod._code_log.clear()
        db.rollback()

    with patch("tutor.agent.tts.synthesize", lambda text: b"ID3"):
        yield c, sent, reset
    app.dependency_overrides.clear()
    db.close()


def _check(results, sent):
    broken = [f"{label}: {status}" for label, status in results if status >= 500]
    alerted = [k for k in sent if k.startswith("exc:") or k in ("db-down", "db-pool")]
    assert not broken and not alerted, "\n".join(broken + [f"alert {k}" for k in alerted])


@pytest.mark.parametrize("path,base", FORMS, ids=[p for p, _ in FORMS])
def test_no_form_field_value_is_a_500(client, path, base):
    c, sent, reset = client
    results = []
    for field in base:
        for name, value in BAD.items():
            reset()
            r = c.post(path, data={**base, field: value}, headers=HTML)
            results.append((f"{path} {field}={name}", r.status_code))
    _check(results, sent)


@pytest.mark.parametrize("path,base", JSON_BODIES, ids=[p for p, _ in JSON_BODIES])
def test_no_json_field_value_is_a_500(client, path, base):
    c, sent, reset = client
    results = []
    for field in base:
        for name, value in BAD.items():
            reset()
            r = c.post(path, content=json.dumps({**base, field: value}),
                       headers={"content-type": "application/json"})
            results.append((f"{path} {field}={name}", r.status_code))
    _check(results, sent)


@pytest.mark.parametrize("path,base", QUERIES, ids=[p for p, _ in QUERIES])
def test_no_query_value_is_a_500(client, path, base):
    c, sent, reset = client
    results = []
    for field in base:
        for name, value in URL_BAD.items():
            reset()
            r = c.get(path, params={**base, field: value}, headers=HTML, follow_redirects=False)
            results.append((f"{path}?{field}={name}", r.status_code))
    _check(results, sent)


@pytest.mark.parametrize("template", PATHS)
def test_no_path_segment_is_a_500(client, template):
    c, sent, reset = client
    results = []
    for name, value in URL_BAD.items():
        if name in ("empty", "nul"):
            continue                        # not a path a browser sends
        reset()
        r = c.get(template.format(v=value), headers=HTML, follow_redirects=False)
        results.append((f"{template} {name}", r.status_code))
    reset()
    r = c.get(template.format(v="a%00b"), headers=HTML, follow_redirects=False)
    results.append((f"{template} %00", r.status_code))
    _check(results, sent)


def test_a_body_that_is_not_utf8_is_not_a_500(client):
    """Bytes posted as text/plain to a JSON route reached the validation
    handler, whose encoder decoded them strictly: a 500 and an alert (the
    Stage 2 review)."""
    c, sent, reset = client
    results = []
    for path in [p for p, _ in JSON_BODIES] + ["/api/chat"]:
        for ctype in ("text/plain", "application/octet-stream", None):
            reset()
            headers = {"content-type": ctype} if ctype else {}
            r = c.post(path, content=b"\xff\xfe\x00\x80", headers=headers)
            results.append((f"{path} {ctype}", r.status_code))
            reset()
            r = c.post(path, content=b"\xff\xfe", headers={**headers, **HTML})
            results.append((f"{path} {ctype} page", r.status_code))
    _check(results, sent)


def test_a_photo_of_any_name_or_size_is_not_a_500(client):
    c, sent, reset = client
    results = []
    for name, payload in {"tiny": b"\x89PNG\r\n\x1a\n", "text": b"hello", "big": b"\x89PNG" + b"0" * 7_000_000}.items():
        for filename in ("p.png", "x" * 5000 + ".png", "a\x00b.png"):
            reset()
            r = c.post("/chat/transcribe-photo", files={"image": (filename, payload, "image/png")})
            results.append((f"photo {name} {filename[:12]}", r.status_code))
    _check(results, sent)



# Stage 2: an address a student edits by hand opens a page, never JSON.
PAGES = [(p, base) for p, base in QUERIES
         if p in ("/chat", "/track-picker", "/review", "/auth/login", "/map", "/lectures")]
NAV = {**HTML, "sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}


def test_a_hand_edited_address_is_a_page_never_json(client):
    """FastAPI's 422 and the error handlers' JSON were what a navigation got
    for a garbled parameter or path (bulletproofing R13). A page answers 200
    with its defaults, 404 for a path that is not there, or a redirect;
    whatever it is, it is HTML."""
    from tutor.auth.deps import get_db
    from tutor.db.models import StudentPath

    c, sent, reset = client
    session = c.app.dependency_overrides[get_db]()
    session.add(StudentPath(student_id=1, course="AST101", track_id="AST101-lecture-order"))  # past /welcome
    session.commit()
    wrong = []

    def nav(label, url, params=None):
        reset()
        r = c.get(url, params=params, headers=NAV, follow_redirects=False)
        if r.status_code >= 500 or not (r.is_redirect or r.headers.get("content-type", "").startswith("text/html")):
            wrong.append(f"{label}: {r.status_code} {r.headers.get('content-type')}")

    for path, base in PAGES:
        for field in base:
            for name, value in URL_BAD.items():
                nav(f"{path}?{field}={name}", path, {**base, field: value})
    for template in PATHS:
        if template.startswith("/chat/message/"):
            continue                        # the stream a script resumes, not a page: JSON is its answer
        for name, value in URL_BAD.items():
            if name not in ("empty", "nul"):
                nav(f"{template} {name}", template.format(v=value))
    assert not wrong, "a navigation got JSON or a 5xx:\n" + "\n".join(wrong)
