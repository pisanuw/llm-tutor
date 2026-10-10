"""tutor/prewarm_tts.py renders the Teach-me openers into the chat's
voice cache, so a clip made once is served to every student who presses 🔊 on
that opener. The key has to match the app's by construction: after a run,
``synthesize`` on the same opener never calls ElevenLabs."""
from __future__ import annotations

import json

import httpx
import pytest

from tutor import prewarm_tts as prewarm
from tutor import config
from tutor.agent import tts

TRACKS = ("lecture-order", "exam-prep")


def _teachme(tmp_path):
    root = tmp_path / "teachme"
    for course, goals in {"AST101": ("g1", "g2"), "AST102": ("h1",)}.items():
        for goal in goals:
            variants = {}
            for track in TRACKS:
                variants[f"{course}-{track}"] = {
                    "brief": f"Brief **{goal}** for `{track}` in {course}.",
                    "standard": f"Standard {goal} for {track}. " * 3,
                    "thorough": f"Thorough {goal}. " * 120 if track == "lecture-order" else "",
                }
            (root / course).mkdir(parents=True, exist_ok=True)
            (root / course / f"{goal}.json").write_text(json.dumps(
                {"goal_id": goal, "title": goal.upper(), "course": course, "variants": variants}))
    return root


@pytest.fixture
def voice(monkeypatch, tmp_path):
    """The voice on, a fresh cache, and ElevenLabs faked; returns the texts posted."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-1")
    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path / "tts"))
    config.get_config.cache_clear()
    posted: list[str] = []

    def post(url, **kw):
        posted.append(kw["json"]["text"])
        return httpx.Response(200, content=b"mp3:" + kw["json"]["text"][:12].encode(),
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(tts.httpx, "post", post)
    return posted


def test_a_dry_run_bills_nothing_and_says_what_it_would(voice, tmp_path, capsys):
    root = _teachme(tmp_path)
    assert prewarm.main(["--teachme-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert voice == []
    assert "6 openers (brief): 0 already cached, 6 to render" in out
    assert "Dry run" in out


def test_spend_renders_each_opener_once_and_the_app_then_serves_it_from_the_cache(voice, tmp_path):
    root = _teachme(tmp_path)
    assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 0
    assert len(voice) == 6
    assert "Brief g1 for lecture-order in AST101." in voice          # the markup stripped, as the app does

    opener = json.loads((root / "AST101" / "g1.json").read_text())["variants"]["AST101-lecture-order"]["brief"]
    assert tts.synthesize(opener) == b"mp3:Brief g1 for"
    assert len(voice) == 6                                        # served from the cache, no call


def test_a_second_run_skips_what_is_cached(voice, tmp_path, capsys):
    root = _teachme(tmp_path)
    prewarm.main(["--teachme-dir", str(root), "--spend", "--course", "AST101"])
    assert len(voice) == 4
    assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 0
    assert len(voice) == 6                                        # only AST102's two
    assert "6 openers (brief): 4 already cached, 2 to render" in capsys.readouterr().out
    assert prewarm.main(["--teachme-dir", str(root)]) == 0
    assert "6 already cached, 0 to render" in capsys.readouterr().out


def test_the_budget_stops_the_run_and_a_rerun_continues(voice, tmp_path, capsys):
    root = _teachme(tmp_path)
    first_two = sum(len(t) for t in ("Brief g1 for exam-prep in AST101.", "Brief g1 for lecture-order in AST101."))
    assert prewarm.main(["--teachme-dir", str(root), "--spend", "--budget", str(first_two)]) == 0
    assert len(voice) == 2
    assert "Budget of" in capsys.readouterr().out
    assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 0
    assert len(voice) == 6


def test_the_filters_pick_a_course_a_track_and_the_depths(voice, tmp_path, capsys):
    root = _teachme(tmp_path)
    assert prewarm.main(["--teachme-dir", str(root), "--course", "AST102",
                         "--track", "AST102-exam-prep", "--depth", "brief", "standard"]) == 0
    assert "2 openers (brief, standard): 0 already cached, 2 to render" in capsys.readouterr().out


def test_an_opener_over_the_quota_is_counted(voice, tmp_path, capsys):
    root = _teachme(tmp_path)
    prewarm.main(["--teachme-dir", str(root), "--depth", "thorough"])
    out = capsys.readouterr().out
    assert "3 openers (thorough)" in out                          # the exam-prep thorough is empty
    assert f"3 of them exceed the {tts.MAX_CHARS}-character quota" in out


def test_with_the_voice_off_it_refuses(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    config.get_config.cache_clear()
    assert prewarm.main(["--teachme-dir", str(_teachme(tmp_path)), "--spend"]) == 2
    assert "Voice is off" in capsys.readouterr().err


def test_an_elevenlabs_failure_stops_the_run(voice, tmp_path, monkeypatch, capsys):
    root = _teachme(tmp_path)
    calls = []

    def post(url, **kw):
        calls.append(kw["json"]["text"])
        if len(calls) == 2:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, content=b"mp3", request=httpx.Request("POST", url))

    monkeypatch.setattr(tts.httpx, "post", post)
    assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 1
    captured = capsys.readouterr()
    assert len(calls) == 2
    assert "Stopped at AST101/g1" in captured.err
    assert "Rendered 1 clips" in captured.out
    assert len(list((tmp_path / "tts").glob("*.mp3"))) == 1       # nothing half-written


def test_the_cache_dir_flag_overrides_the_environment(voice, tmp_path):
    root = _teachme(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    assert prewarm.main(["--teachme-dir", str(root), "--spend", "--cache-dir", str(elsewhere)]) == 0
    assert len(list(elsewhere.glob("*.mp3"))) == 6
    assert not (tmp_path / "tts").exists()


def test_a_second_spend_refuses_while_the_first_holds_the_cache(voice, tmp_path, capsys):
    """Two runs at once each check the cache and both render the next missing
    clip, so the bill doubles (2026-09-28). The lock lives in the cache."""
    import fcntl

    root = _teachme(tmp_path)
    cache = tmp_path / "tts"
    cache.mkdir()
    holder = open(cache / ".prewarm.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 3
        assert voice == []
        assert "Another run holds" in capsys.readouterr().err
        assert prewarm.main(["--teachme-dir", str(root)]) == 0          # a dry run still reads
    finally:
        holder.close()
    assert prewarm.main(["--teachme-dir", str(root), "--spend"]) == 0  # released: it runs
    assert len(voice) == 6
