"""Photo-to-code tests (capability item 7): sniffing, rate limit,
fence stripping, NO_CODE handling, and the endpoint's guardrails. The vision
API call is always mocked; no tokens are spent."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from tutor.agent import vision
from tutor.db.models import Student
from tests.helpers import make_session

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _fresh_limits():
    vision._reset_for_tests()
    yield
    vision._reset_for_tests()


class _FakeClient:
    def __init__(self, text):
        outer_text = text

        class _Msg:
            def create(self, **kwargs):
                class _Block:
                    type = "text"
                    text = outer_text

                class _Resp:
                    content = [_Block()]
                    usage = None

                return _Resp()

        self.messages = _Msg()


def test_sniff_types():
    assert vision.sniff(JPG) == "image/jpeg"
    assert vision.sniff(PNG) == "image/png"
    assert vision.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert vision.sniff(b"GIF89a....") is None
    assert vision.sniff(b"%PDF-1.7") is None


def test_transcribe_strips_fences(monkeypatch):
    monkeypatch.setattr(vision, "_client", lambda: _FakeClient("```cpp\nint main() {}\n```"))
    out = vision.transcribe_image(JPG, "image/jpeg")
    out.pop("usage")                       # the route ledgers it (bulletproofing R26)
    assert out == {"ok": True, "code": "int main() {}"}


def test_transcribe_no_code(monkeypatch):
    monkeypatch.setattr(vision, "_client", lambda: _FakeClient("NO_CODE"))
    out = vision.transcribe_image(JPG, "image/jpeg")
    assert out["ok"] is False and "No code" in str(out["error"])


def test_transcribe_api_failure_is_contained(monkeypatch):
    class _Boom:
        @property
        def messages(self):
            raise RuntimeError("api down")

    monkeypatch.setattr(vision, "_client", lambda: _Boom())
    out = vision.transcribe_image(JPG, "image/jpeg")
    assert out["ok"] is False


def test_daily_limit_window():
    for _ in range(vision.DAILY_PHOTO_LIMIT):
        assert vision.allow(7) is True
    assert vision.allow(7) is False
    assert vision.allow(8) is True  # other students unaffected


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client():
    from tutor.main import app
    from tutor.auth.deps import get_current_student, get_db

    session = make_session()
    s = Student(email="photo@example.com", name="Photo")
    session.add(s)
    session.commit()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_student] = lambda: s
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()
    session.close()


def _post(client, data: bytes, name="p.jpg"):
    return client.post(
        "/chat/transcribe-photo",
        files={"image": (name, io.BytesIO(data), "image/jpeg")},
    )


def test_endpoint_happy_path(app_client, monkeypatch):
    monkeypatch.setattr(vision, "transcribe_image", lambda d, m: {"ok": True, "code": "int x;"})
    r = _post(app_client, JPG)
    assert r.status_code == 200 and r.json() == {"ok": True, "code": "int x;"}


def test_endpoint_rejects_unsupported_type(app_client):
    r = _post(app_client, b"GIF89a" + b"\x00" * 32)
    assert r.status_code == 415


def test_endpoint_rejects_oversize(app_client):
    r = _post(app_client, JPG + b"\x00" * vision.MAX_IMAGE_BYTES)
    assert r.status_code == 413


def test_endpoint_rate_limited(app_client, monkeypatch):
    monkeypatch.setattr(vision, "transcribe_image", lambda d, m: {"ok": True, "code": "x"})
    monkeypatch.setattr(vision, "DAILY_PHOTO_LIMIT", 2)
    assert _post(app_client, JPG).status_code == 200
    assert _post(app_client, JPG).status_code == 200
    assert _post(app_client, JPG).status_code == 429
