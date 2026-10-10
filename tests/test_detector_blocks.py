"""Plan step 1.4 (finding A10): tilde fences and indented code blocks.

The detector used to understand only ``` fences. The chat renders ~~~ fences
and 4-space indented blocks as code too (marked, GFM), and shows indented code
after a paragraph line as visible text, so both slipped past the gate on
judge-free turns. The batch scan/redact and the streaming gate now find the
same blocks; these tests run every case at chunk sizes from 1 to 1000 and
check that the two agree.
"""
from __future__ import annotations

import asyncio
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent.detector import SUBSTITUTION_LINE, StreamGate, extract_code_blocks, redact, scan
from tests.helpers import make_session

CHUNKS = [1, 2, 3, 5, 7, 13, 64, 1000]

FLUX = """double flux(double lum, double d) {
    if (d <= 0) {
        return 0;
    }
    return lum / (4 * 3.14159 * d * d);
}"""

SHORT = """int add(int a, int b) {
    return a + b;
}"""


def _indent(body: str, prefix: str = "    ") -> str:
    return textwrap.indent(body, prefix)


def _gate(text: str, chunk: int, **kw) -> tuple[str, bool]:
    g = StreamGate(**kw)
    out = [g.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)]
    out.append(g.close())
    return "".join(out), g.cut


# (name, text, violating in lenient mode)
CASES = [
    ("tilde_solution", f"Here you go:\n\n~~~cpp\n{FLUX}\n~~~\n\nGood luck.", True),
    ("tilde_long_marker", f"Here:\n~~~~cpp\n{FLUX}\n~~~~\nDone.", True),
    ("tilde_indented_marker", f"Here:\n   ~~~cpp\n{FLUX}\n   ~~~\nDone.", True),
    ("tilde_short_snippet", f"Syntax:\n~~~cpp\n{SHORT}\n~~~\nTry it.", False),
    ("tilde_unclosed_at_eof", f"Sure:\n\n~~~cpp\n{FLUX}", True),
    ("indented_after_blank", f"Here you go:\n\n{_indent(FLUX)}\n\nGood luck.", True),
    ("indented_after_paragraph", f"Here is the code:\n{_indent(FLUX)}\nGood luck.", True),
    ("indented_with_tabs", f"Here:\n\n{_indent(FLUX, chr(9))}\n\nDone.", True),
    ("indented_split_by_blank_lines",
     "Look:\n\n    double f(double d) {\n        if (d <= 0) return 0;\n\n"
     "        double r = 1 / (d * d);\n        return r;\n    }\n\nDone.", True),
    ("indented_at_eof", f"Here:\n\n{_indent(FLUX)}", True),
    ("indented_short_snippet", f"Like this:\n\n{_indent(SHORT)}\n\nThen test.", False),
    ("nested_list_prose",
     "1. Read the slide\n    about moon phases, then\n    sketch a small orbit.\n2. Try it yourself.", False),
    ("mid_line_tildes", "That was close~~~ really close. Try d = 0 first.", False),
    ("backtick_solution_still_cut", f"Here:\n```cpp\n{FLUX}\n```\nDone.", True),
    ("fence_inside_list_item", f"1. Step:\n    ```cpp\n{_indent(FLUX)}\n    ```\n2. Next", True),
    ("plain_prose", "Think about where the Sun is first, then the Moon.", False),
]


@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("name,text,violating", CASES, ids=[c[0] for c in CASES])
def test_gate_and_batch_agree(name, text, violating, chunk):
    out, cut = _gate(text, chunk)
    assert cut is violating, name
    assert scan(text).violating is violating, name
    # The streaming gate and the batch redactor produce the same text.
    assert out == redact(text), name
    if violating:
        assert "4 * 3.14159 * d * d" not in out and "1 / (d * d)" not in out
        assert SUBSTITUTION_LINE in out
    else:
        assert out == text


@pytest.mark.parametrize("chunk", CHUNKS)
def test_prose_around_a_cut_block_survives(chunk):
    text = f"Here you go:\n\n~~~cpp\n{FLUX}\n~~~\n\nGood luck."
    out, _ = _gate(text, chunk)
    assert out.startswith("Here you go:\n\n")
    assert out.endswith("\n\nGood luck.")
    text = f"Here you go:\n\n{_indent(FLUX)}\n\nGood luck."
    out, _ = _gate(text, chunk)
    assert out == f"Here you go:\n\n{SUBSTITUTION_LINE}\n\nGood luck."


@pytest.mark.parametrize("chunk", CHUNKS)
def test_strict_mode_cuts_short_tilde_and_indented_snippets(chunk):
    for text in (f"~~~cpp\n{SHORT}\n~~~", f"Try:\n\n{_indent(SHORT)}\n"):
        out, cut = _gate(text, chunk, strict=True)
        assert cut is True
        assert "return a + b" not in out
        assert scan(text, strict=True).violating


def test_extract_code_blocks_kinds():
    text = f"A\n\n~~~cpp\n{SHORT}\n~~~\nB\n\n{_indent(SHORT)}\n\nC ```x``` D"
    kinds = [b.lang for b in extract_code_blocks(text)]
    assert kinds == ["cpp", "indented", "x"]


# ---------------------------------------------------------------------------
# Done when: a ~~~cpp solution is cut on a judge-free turn
# ---------------------------------------------------------------------------

def test_tilde_solution_is_cut_on_a_judge_free_turn(no_strategist):
    import json

    from tutor.agent import turn as agent
    from tutor.agent.envelope import EnvelopeBundle
    from tutor.agent.policy import Envelope, Intent
    from tutor.agent.preclassify import Classification
    from tutor.db.models import Student, TurnEvent

    db = make_session()
    db.add(Student(id=1, email="stu@example.edu", name="Stu"))
    db.commit()
    # H5, a concept question: the judge does not run, only the stream gate.
    envelope = Envelope(max_rung=5, goals=["G3"], exam_lockdown=[], must_ground=True,
                        allow_exercise=True, escalate_review=False, reason="band")
    bundle = EnvelopeBundle(envelope=envelope, goal_titles={}, gate_goal=None)
    reply = f"Sure, here it is:\n\n~~~cpp\n{FLUX}\n~~~\n\nTry it with d = 3."

    class _Usage:
        input_tokens = 100
        output_tokens = 20
        cache_read_input_tokens = 0

    async def one_round(system, messages, tools):
        for i in range(0, len(reply), 6):
            yield ("chunk", reply[i:i + 6])
        final = MagicMock()
        final.stop_reason = "end_turn"
        final.content = []
        final.usage = _Usage()
        yield ("final", final)

    judge = MagicMock()

    async def go():
        return [e async for e in agent.run_turn(
            student=db.get(Student, 1), db=db, user_message="how bright is a star twice as far away?",
            lecture_num=None, course="AST101",
        )]

    try:
        with patch("tutor.agent.preclassify.classify",
                   return_value=Classification(intent=Intent.QUESTION)), \
             patch("tutor.agent.mastery.get_active_exam", return_value=None), \
             patch("tutor.agent.envelope.assemble_envelope", return_value=bundle), \
             patch("tutor.agent.turn_stages.actor_client.stream_round", one_round), \
             patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"), \
             patch("tutor.agent.judge.judge", judge):
            events = asyncio.run(go())
        text = "".join(e.get("text", "") for e in events if e["type"] == "token")
        judge.assert_not_called()
        assert "4 * 3.14159 * d * d" not in text
        assert SUBSTITUTION_LINE in text
        assert text.startswith("Sure, here it is:") and text.endswith("Try it with d = 3.")
        ev = db.query(TurnEvent).filter_by(kind="chat").one()
        verdicts = json.loads(ev.verdicts_json)
        assert verdicts["detector_cut"] is True and verdicts["judged"] is False
    finally:
        db.close()
