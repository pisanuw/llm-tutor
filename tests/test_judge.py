"""Task 4 tests: the post-judge (mocked Anthropic client, network-free)."""

from __future__ import annotations

from unittest.mock import MagicMock


from tutor.agent.judge import judge, should_judge, _render_input, _parse
from tutor.agent.policy import Envelope, Intent


# ---------------------------------------------------------------------------
# _parse hardening: a truncated reason must not downgrade revise/block to allow
# ---------------------------------------------------------------------------

def test_parse_valid_json():
    assert _parse('{"verdict":"revise","rule":"R","reason":"too much"}') == ("revise", "R", "too much")


def test_parse_strips_code_fences():
    assert _parse('```json\n{"verdict":"block","rule":"x","reason":"y"}\n```')[0] == "block"


def test_parse_salvages_verdict_from_truncated_json():
    # A long reason hit max_tokens: the JSON is cut off mid-string with no closing
    # quote/brace. Must recover "revise", NOT default to allow (under-block).
    raw = ('{"verdict": "revise", "rule": "must_ground", "reason": "The draft cites '
           "a textbook source but no sources were retrieved to verify this claim or "
           "its three-requirement framework which goes on and on past the token")  # truncated
    verdict, rule, reason = _parse(raw)
    assert verdict == "revise"
    assert rule == "must_ground"
    assert "The draft cites" in reason


def test_parse_salvages_block():
    raw = '{"verdict": "block", "reason": "revealed an exam answer and then kept'
    assert _parse(raw)[0] == "block"


def test_parse_unrecoverable_defaults_to_allow():
    assert _parse("total gibberish, no verdict here")[0] == "allow"
    assert _parse("")[0] == "allow"


def test_parse_biases_to_strictest_when_multiple():
    # Defensive: if salvage finds more than one verdict field, the stricter wins
    # (never under-block). Malformed so json.loads fails and salvage runs.
    raw = 'noise "verdict": "revise" noise "verdict": "block" unclosed'
    assert _parse(raw)[0] == "block"


def _env(max_rung=3, exam_lockdown=(), must_ground=True, escalate=False):
    return Envelope(
        max_rung=max_rung,
        goals=["G1"],
        exam_lockdown=list(exam_lockdown),
        must_ground=must_ground,
        allow_exercise=True,
        escalate_review=escalate,
        reason="test",
    )


def _client(*texts):
    c = MagicMock()
    resps = []
    for t in texts:
        r = MagicMock()
        r.content = [MagicMock(text=t)]
        r.usage = None
        resps.append(r)
    c.messages.create.side_effect = resps
    return c


# ---------------------------------------------------------------------------
# should_judge gating
# ---------------------------------------------------------------------------

def test_should_judge_on_escalate_review():
    assert should_judge(_env(escalate=True), Intent.QUESTION, code_adjacent=False) is True


def test_should_judge_on_low_rung_code_adjacent():
    assert should_judge(_env(max_rung=3), Intent.QUESTION, code_adjacent=True) is True


def test_should_judge_covers_floored_attempt_at_h4():
    # A floored ATTEMPT sits at H4; the judge must still run (now permitting H4)
    # so it keeps catching code/over-help instead of dropping coverage.
    assert should_judge(_env(max_rung=4), Intent.ATTEMPT, code_adjacent=True) is True


def test_should_skip_routine_concept_chat():
    assert should_judge(_env(max_rung=5, escalate=False), Intent.QUESTION, code_adjacent=False) is False


def test_should_skip_high_rung_code_adjacent():
    # Above the H4 bound (e.g. a low-mastery worked-example turn) the judge is
    # off again; the detector still guards code.
    assert should_judge(_env(max_rung=5, escalate=False), Intent.ATTEMPT, code_adjacent=True) is False


# ---------------------------------------------------------------------------
# Verdict parsing / majority
# ---------------------------------------------------------------------------

def test_single_verdict_revise():
    v = judge(_env(), "draft", ["slide 4"], client=_client('{"verdict":"revise","rule":"H3","reason":"leaked code"}'))
    assert v.verdict == "revise"
    assert v.rule == "H3"


def test_three_sample_majority_revise():
    c = _client(
        '{"verdict":"revise","rule":"r","reason":"a"}',
        '{"verdict":"allow","rule":"","reason":"b"}',
        '{"verdict":"revise","rule":"r","reason":"c"}',
    )
    v = judge(_env(escalate=True), "draft", [], samples=3, client=c)
    assert v.verdict == "revise"
    assert len(v.samples) == 3


def test_tie_breaks_toward_stricter():
    c = _client(
        '{"verdict":"allow","rule":"","reason":"a"}',
        '{"verdict":"revise","rule":"","reason":"b"}',
        '{"verdict":"block","rule":"","reason":"c"}',
    )
    v = judge(_env(escalate=True), "draft", [], samples=3, client=c)
    assert v.verdict == "block"  # 1-1-1 tie -> most severe


def test_block_majority():
    c = _client(
        '{"verdict":"block","rule":"","reason":"a"}',
        '{"verdict":"block","rule":"","reason":"b"}',
        '{"verdict":"allow","rule":"","reason":"c"}',
    )
    v = judge(_env(escalate=True), "draft", [], samples=3, client=c)
    assert v.verdict == "block"


def test_unparseable_judge_defaults_to_allow():
    v = judge(_env(), "draft", [], client=_client("not json"))
    assert v.verdict == "allow"


# ---------------------------------------------------------------------------
# Collusion resistance: the judge never sees the student message
# ---------------------------------------------------------------------------

def test_render_input_has_no_transcript_slot():
    rendered = _render_input(_env(exam_lockdown=["G9"]), "the draft text", ["src a"])
    assert "DRAFT" in rendered
    assert "the draft text" in rendered
    assert "G9" in rendered  # exam lockdown surfaced
    # There is simply no parameter through which a student message could enter.


# ---------------------------------------------------------------------------
# Plan 1.2 (A7): fail closed on high-risk turns; judge the whole draft
# ---------------------------------------------------------------------------

def _raising_client(n=3):
    c = MagicMock()
    c.messages.create.side_effect = [RuntimeError("overloaded")] * n
    return c


def test_errors_fail_closed_on_an_escalated_turn():
    v = judge(_env(escalate=True), "draft", [], samples=3, client=_raising_client())
    assert v.verdict == "block"
    assert v.rule == "judge_unavailable"
    assert [s[0] for s in v.samples] == ["error", "error", "error"]


def test_errors_fail_closed_under_an_exam_lockdown():
    v = judge(_env(exam_lockdown=["G1"]), "draft", [], client=_raising_client(1))
    assert v.verdict == "block"


def test_errors_fail_open_on_a_routine_turn():
    # Decision 4 default: a judge outage must not silence routine tutoring.
    v = judge(_env(), "draft", [], client=_raising_client(1))
    assert v.verdict == "allow"
    assert v.rule == "judge_error"


def test_errored_samples_do_not_outvote_a_real_block():
    c = MagicMock()
    ok = MagicMock()
    ok.content = [MagicMock(text='{"verdict":"block","rule":"leak","reason":"code"}')]
    ok.usage = None
    c.messages.create.side_effect = [ok, RuntimeError("x"), RuntimeError("y")]
    v = judge(_env(escalate=True), "draft", [], samples=3, client=c)
    # Before: the two failures voted "allow" and outvoted the block.
    assert v.verdict == "block"
    assert v.rule == "leak"


def test_unreadable_output_fails_closed_under_lockdown():
    v = judge(_env(exam_lockdown=["G1"]), "draft", [], client=_client("not json"))
    assert v.verdict == "block"
    v = judge(_env(exam_lockdown=["G1"]), "draft", [], client=_client('{"rule": "x"}'))
    assert v.verdict == "block"


def test_missing_key_fails_closed_under_lockdown(monkeypatch):
    import tutor.config as config

    monkeypatch.setattr(config, "get_config", lambda: {})
    assert judge(_env(exam_lockdown=["G1"]), "draft", []).verdict == "block"
    assert judge(_env(), "draft", []).verdict == "allow"


def test_parse_flags_a_missing_or_invalid_verdict():
    assert _parse('{"rule": "r", "reason": "no verdict"}')[1] == "parse_error"
    assert _parse('{"verdict": "maybe"}')[1] == "parse_error"
    assert _parse('["allow"]')[1] == "parse_error"


def test_judge_reads_the_whole_draft():
    tail = "```cpp\nint leak() { return 42; }\n```"
    draft = ("word " * 3000) + tail          # about 15,000 characters
    rendered = _render_input(_env(), draft, [])
    assert tail in rendered
