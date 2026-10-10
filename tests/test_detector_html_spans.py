"""Two more shapes the solution detector treats as code blocks.

The gate understood ``` and ~~~ fences and 4-column indented runs. Two shapes
still rendered as a copy-pasteable block and streamed straight through:

* raw HTML ``<pre>`` (with or without ``<code>``), which marked passes through;
* a run of lines that are each nothing but one inline code span, which renders
  as a block of code one line at a time.

As in test_detector_blocks.py, every case runs at chunk sizes from 1 to 1000 and
the streaming gate has to agree with the batch scan/redact.

The last group is a regression: the move-tag hold used ``$``, which in Python
also matches just before a trailing newline, so a chunk boundary landing on a
trailing backtick (or ``**``, ``_``, ``[``, spaces) dropped the newline and
joined two lines of the reply.
"""
from __future__ import annotations

import pytest

from tutor.agent.detector import (
    SUBSTITUTION_LINE,
    StreamGate,
    extract_code_blocks,
    redact,
    scan,
)

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


def _gate(text: str, chunk: int, **kw) -> tuple[str, bool]:
    g = StreamGate(**kw)
    out = [g.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)]
    out.append(g.close())
    return "".join(out), g.cut


def _spans(body: str) -> str:
    """Each line of ``body`` as its own inline code span."""
    return "\n".join(f"`{line}`" for line in body.splitlines())


def _escaped(body: str) -> str:
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# (name, text, violating in lenient mode)
CASES = [
    # --- raw HTML <pre> ---------------------------------------------------
    ("pre_plain", f"Here you go:\n<pre>{FLUX}</pre>\nGood luck.", True),
    ("pre_with_code", f"Here:\n<pre><code>{FLUX}</code></pre>\nDone.", True),
    ("pre_entities_escaped", f"Here:\n<pre><code>{_escaped(FLUX)}</code></pre>\nDone.", True),
    ("pre_with_attributes",
     f'Here:\n<pre class="language-cpp" data-x="1">{FLUX}</pre>\nDone.', True),
    ("pre_uppercase_tag", f"Here:\n<PRE><CODE>{FLUX}</CODE></PRE>\nDone.", True),
    ("pre_close_with_space", f"Here:\n<pre>{FLUX}</pre >\nDone.", True),
    ("pre_unclosed_at_eof", f"Sure:\n<pre>{FLUX}", True),
    ("pre_short_snippet", f"Syntax:\n<pre>{SHORT}</pre>\nTry it.", False),
    ("pre_prose_only",
     "<pre>Read the slide on moon phases, then sketch a small orbit by hand.</pre>", False),
    ("pre_pseudocode_blanks",
     "<pre>total = ___\nfor each item:\n    total += ___\n    count += ___\nreturn ___</pre>",
     False),
    ("pre_not_cpp",
     "<pre>usage: prog [-h]\n  -h  show help\n  -v  verbose\n  -q  quiet\n  -x  extra</pre>",
     False),
    # --- one inline code span per line ------------------------------------
    ("spanrun_solution", f"Here you go:\n{_spans(FLUX)}\nGood luck.", True),
    ("spanrun_at_eof", f"Here:\n{_spans(FLUX)}", True),
    ("spanrun_indented_up_to_3", f"Here:\n{_spans(FLUX)}\nDone.".replace("`double", "  `double"),
     True),
    ("spanrun_short_snippet", f"Like this:\n{_spans(SHORT)}\nThen test.", False),
    ("span_single_line", "Try `if (d < 1) d = 1;` on its own line.\nThen think.", False),
    ("span_one_span_line_only", "`int i = 0;`\nThat is the initialiser.", False),
    ("spanrun_two_lines_only", "`int a = 1;`\n`int b = 2;`\nThat is all.", False),
    ("spanrun_not_cpp", "`alpha`\n`beta`\n`gamma`\n`delta`\n`epsilon`\nJust names.", False),
    ("spans_inside_prose",
     "Use `flux()` and then `size()`, and check `mags[0]` before you index.", False),
    # --- neither ----------------------------------------------------------
    ("prose_mentioning_pre", "The <pre-dawn sky shows the planets first.", False),
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
        assert "4 * 3.14159 * d * d" not in out, name
        assert SUBSTITUTION_LINE in out, name
    else:
        assert out == text, name


@pytest.mark.parametrize("chunk", CHUNKS)
def test_prose_around_a_cut_block_survives(chunk):
    out, _ = _gate(f"Here you go:\n<pre>{FLUX}</pre>\nGood luck.", chunk)
    assert out == f"Here you go:\n{SUBSTITUTION_LINE}\nGood luck."
    out, _ = _gate(f"Here you go:\n{_spans(FLUX)}\nGood luck.", chunk)
    assert out == f"Here you go:\n{SUBSTITUTION_LINE}\nGood luck."


@pytest.mark.parametrize("chunk", CHUNKS)
def test_a_span_run_is_never_handed_out_in_pieces(chunk):
    """The run must not settle two lines at a time: each pair is under
    min_lines, and the whole solution would stream out unflagged."""
    text = f"Here:\n{_spans(FLUX)}\nDone."
    out, cut = _gate(text, chunk)
    assert cut is True
    for line in FLUX.splitlines():
        assert line.strip() not in out, (chunk, line)


@pytest.mark.parametrize("chunk", CHUNKS)
def test_strict_mode_cuts_short_pre_and_span_snippets(chunk):
    for text in (f"<pre>{SHORT}</pre>", f"Try:\n{_spans(SHORT)}\n"):
        out, cut = _gate(text, chunk, strict=True)
        assert cut is True
        assert "return a + b" not in out
        assert scan(text, strict=True).violating


def test_extract_code_blocks_kinds():
    text = (f"A\n<pre>{SHORT}</pre>\nB\n{_spans(SHORT)}\nC\n```x```\nD")
    assert [b.lang for b in extract_code_blocks(text)] == ["html", "spanrun", "x"]


def test_html_body_decodes_entities_and_drops_tags():
    block, = extract_code_blocks("<pre><code>if (a &lt; b) return &amp;a;</code></pre>")
    assert block.body == "if (a < b) return &a;"


# ---------------------------------------------------------------------------
# Regression: the streamed reply kept every newline
# ---------------------------------------------------------------------------

NEWLINE_CASES = [
    "Try `v.size()`\nThen think about it.",
    "One line only: `double mags[3];`\nMove on.",
    "Bold **word**\nnext line",
    "Trailing spaces   \nnext line",
    "An underscore_\nnext line",
    "A bracket [\nnext line",
    "UPPERCASE\nnext line",
]


@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("text", NEWLINE_CASES)
def test_safe_prose_streams_through_unchanged(text, chunk):
    out, cut = _gate(text, chunk)
    assert cut is False
    assert out == text


@pytest.mark.parametrize("chunk", [1, 2, 4, 1000])
def test_a_leaked_move_tag_is_still_stripped_across_chunks(chunk):
    from tutor.agent.policy import Move

    tag = next(iter(Move)).value
    for text in (f"Before I explain **{tag}** let me ask.", f"Done {tag}.",
                 f"{tag} at the start."):
        out, _ = _gate(text, chunk)
        assert tag not in out, (chunk, text)
