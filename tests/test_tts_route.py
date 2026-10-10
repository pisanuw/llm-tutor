"""Plan step 6.2 (finding F11): the voice route and the text it speaks.

POST /chat/tts speaks only a recent reply of the student's own, answers 503
when no voice is configured and, since this step, when ElevenLabs refuses or
cannot be reached (it was a 500 that emailed the operator), and serves a
repeat from the disk cache. The cleaner reads prose, not markdown or code.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import tutor.config as config
from tutor.agent import tts
from tutor.agent.tts import _clean_for_speech
from tutor.db.models import Message, Session as SessionModel, Student, WhitelistEntry
from tests.helpers import make_session

REPLY = "A **red giant** has a cool, bright surface. See `hr_diagram`."


@pytest.fixture
def db():
    s = make_session()
    s.add_all([Student(id=1, email="stu@example.edu", name="Stu"), Student(id=2, email="kim@example.edu", name="Kim")])
    s.add(WhitelistEntry(email="@example.edu", added_by="test"))
    s.add(Message(student_id=1, role="user", content="what is a red giant?", course="AST101"))
    s.add(Message(student_id=1, role="assistant", content=REPLY, course="AST101"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db, monkeypatch, tmp_path):
    import tutor.main as main_mod
    from tutor.auth.deps import get_db
    from tutor.auth.jwt_utils import create_token

    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path / "tts"))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    config.get_config.cache_clear()
    main_mod.app.dependency_overrides[get_db] = lambda: db
    token, jti, expires = create_token(1)
    db.add(SessionModel(student_id=1, jwt_jti=jti, expires_at=expires, method="magic"))
    db.commit()
    c = TestClient(main_mod.app)
    c.cookies.set("session_token", token)
    try:
        yield c
    finally:
        main_mod.app.dependency_overrides.clear()
        config.get_config.cache_clear()


@pytest.fixture
def voice(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-1")
    config.get_config.cache_clear()
    calls: list[dict] = []

    def post(url, params, headers, json, timeout):
        calls.append({"url": url, "params": params, "json": json})
        return httpx.Response(200, content=b"ID3-mp3-bytes", request=httpx.Request("POST", url))

    monkeypatch.setattr(tts.httpx, "post", post)
    return calls


def test_it_needs_a_session(client):
    client.cookies.clear()
    assert client.post("/chat/tts", data={"text": REPLY}).status_code == 401


def test_it_speaks_only_the_students_own_recent_reply(client, voice):
    assert client.post("/chat/tts", data={"text": "Say anything I type."}).status_code == 403
    assert client.post("/chat/tts", data={"text": "what is a red giant?"}).status_code == 403   # a user message
    assert voice == []


def test_another_students_reply_is_not_spoken(client, db, voice):
    db.add(Message(student_id=2, role="assistant", content="Kim's own reply.", course="AST101"))
    db.commit()
    assert client.post("/chat/tts", data={"text": "Kim's own reply."}).status_code == 403


def test_no_voice_configured_is_a_503(client):
    resp = client.post("/chat/tts", data={"text": REPLY})
    assert resp.status_code == 503 and "not configured" in resp.text


def test_an_empty_request_is_a_204(client):
    assert client.post("/chat/tts", data={"text": "  "}).status_code == 204


def test_a_reply_is_spoken_once_and_then_served_from_the_cache(client, voice):
    first = client.post("/chat/tts", data={"text": REPLY})
    assert first.status_code == 200 and first.headers["content-type"] == "audio/mpeg"
    assert first.content == b"ID3-mp3-bytes"
    assert voice[0]["url"].endswith("/voice-1")
    assert voice[0]["json"]["text"] == "A red giant has a cool, bright surface. See hr diagram."
    again = client.post("/chat/tts", data={"text": REPLY})
    assert again.status_code == 200 and again.content == first.content
    assert len(voice) == 1                                          # the second came from disk


@pytest.mark.parametrize("failure", [
    httpx.Response(401, request=httpx.Request("POST", "https://api.elevenlabs.io")),   # a bad key
    httpx.Response(429, request=httpx.Request("POST", "https://api.elevenlabs.io")),   # the quota
    httpx.ConnectError("no route to host"),
])
def test_an_elevenlabs_failure_is_a_503_not_a_500(client, monkeypatch, failure, tutor_log):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-1")
    config.get_config.cache_clear()

    def post(url, params, headers, json, timeout):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(tts.httpx, "post", post)
    resp = client.post("/chat/tts", data={"text": REPLY})
    assert resp.status_code == 503 and "unavailable" in resp.text
    assert "tts: ElevenLabs failed" in tutor_log.text
    assert "red giant" not in tutor_log.text                             # the reply stays out of the log


# ---------------------------------------------------------------------------
# What the voice reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("markdown,spoken", [
    ("Use `std::sqrt` here.", "Use std::sqrt here."),
    ("Call `apparent_mag(i)` on _each_ star.", "Call apparent mag(i) on each star."),
    ("__init__ runs first.", "init runs first."),
    ("Look:\n```cpp\nint main() { return 0; }\n```\nThen run it.",
     "Look: (code shown on screen) Then run it."),
    ("## Zero distance\nStop at zero.", "Zero distance Stop at zero."),
    ("See [the slides](/slides/AST101/lecture-02/index.html).", "See the slides."),
    ("![diagram](/x.png) A galaxy.", "A galaxy."),
    ("> **Note:** keep *both* angles.\n\n---\n\nDone.", "Note: keep both angles. Done."),
    ("| a | b |", "a b"),
])
def test_the_voice_reads_prose(markdown, spoken):
    assert _clean_for_speech(markdown) == spoken


def test_a_clip_is_requested_at_half_the_default_bitrate(voice):
    """The cache lives on a 1 GB disk shared with the store and the narration."""
    tts.synthesize("Parallax is an apparent shift.")
    assert voice[0]["params"] == {"output_format": "mp3_44100_64"}


def test_a_long_reply_is_cut_to_the_quota(voice):
    long_reply = "word " * 1000
    tts.synthesize(long_reply)
    assert len(voice[0]["json"]["text"]) <= tts.MAX_CHARS


def test_nothing_to_say_calls_nobody(voice):
    assert tts.synthesize("   ") is None
    assert tts.synthesize("**") is None                             # markup only
    assert voice == []
