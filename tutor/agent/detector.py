"""Deterministic SOLUTION_REVEAL detector (P5: deterministic outranks the LLM).

Pure functions, no LLM, no network. It flags fenced code blocks that look like
a real C++ solution and would hand the student a copy-pasteable answer. It runs
before and independently of the post-judge, and is exhaustively unit-tested.

What is scanned: every block the chat's Markdown renderer (marked, GFM) shows
as code or as code-shaped text (plan step 1.4):
  - ``` fences, anywhere (the historic rule; inline triple backticks too);
  - ~~~ fences at the start of a line (up to 3 spaces before them);
  - indented blocks: runs of lines indented 4 or more columns (a tab counts
    as 4), whatever precedes them. marked renders them as code after a blank
    line and as visible paragraph text otherwise; either way the code shows;
  - raw HTML ``<pre>`` blocks (with or without ``<code>`` inside): marked
    passes HTML through, so this renders as a copy-pasteable block. Tags are
    dropped and entities decoded before the C++ test, so ``a &lt; b`` reads as
    ``a < b``;
  - span runs: two or more consecutive lines that are each nothing but one
    inline code span (```` `int i = 0;` ````). One such line is prose; a run of
    them renders as a block of code one line at a time.
An unclosed fence or block at the end of the text is judged as it stands.

What is flagged: a block whose body
  (a) has more than ``min_lines`` (default 4) non-blank lines, AND
  (b) looks like plausible C++ (type keywords + braces + semicolons, or
      tell-tale tokens like #include / std:: / cout / -> / ::).

What is exempt:
  - Pseudo-code with blanks (contains ``___``): the point is to make the
    student fill them in.
  - Short snippets (<= min_lines): a few lines to illustrate syntax is fine.
  - Non-C++ fences (bash, python, plain text): they do not match the C++
    heuristics, so a shell one-liner is not treated as a solution reveal.
  - A single inline code span and prose mentions (e.g. ``vector<int>`` in a
    sentence): one span on a line is prose, and a span inside a sentence is
    never a block. Unfenced code at the left margin is not a block either; on
    risky turns the judge reads it.

Also flagged: a base64 run that decodes to plausible C++ (an encoding-evasion
where the actor is talked into emitting the answer base64-encoded). Only runs
that actually decode to C++ are cut, so hashes, UUIDs, and base64 of prose are
left alone.

For the live stream, :class:`StreamGate` holds tokens while inside a block and,
when the block ends, emits the original (safe) or a substitution line
(violating). It also redacts encoded solutions inline, holding a trailing base64
run that may be a token split across chunks. The batch :func:`scan` and
:func:`redact` find blocks with the same rules (:func:`extract_code_blocks`).
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from dataclasses import dataclass, field

# Default substitution shown in place of a cut solution.
SUBSTITUTION_LINE = (
    "I almost wrote that out for you. Not happening :-) "
    "Here is the idea instead, in words, so you write the code."
)

_DEFAULT_MIN_LINES = 4

# C++ type / keyword signals (whole-word matched).
_CPP_KEYWORDS = (
    "int", "char", "bool", "double", "float", "void", "long", "short",
    "unsigned", "size_t", "string", "vector", "auto", "const", "static",
    "struct", "class", "template", "namespace", "nullptr", "new", "delete",
    "return", "while", "for", "if", "else", "switch",
)
_CPP_KEYWORD_RE = re.compile(r"\b(" + "|".join(_CPP_KEYWORDS) + r")\b")

# Strong C++ tokens: any one is enough to call a multi-line block C++.
_CPP_TOKENS = ("#include", "std::", "cout", "cin", "->", "::", "endl")

# A function-definition-ish signature: `type name(args) {`
_FUNC_SIG_RE = re.compile(r"\b[A-Za-z_][\w:<>,&*\s]*\s+[A-Za-z_]\w*\s*\([^;{]*\)\s*\{")

_FENCE_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
_TILDE_FENCE_RE = re.compile(r"~~~+([^\n]*)\n?(.*?)~~~", re.DOTALL)

# An indented code block starts at this many columns of leading whitespace.
_INDENT_COLS = 4


@dataclass
class CodeBlock:
    lang: str
    body: str
    start: int     # index of the opening ``` in the source
    end: int       # index just past the closing ```


@dataclass
class DetectorResult:
    violating: bool
    blocks: list[CodeBlock] = field(default_factory=list)
    reason: str = ""


def _non_blank_lines(body: str) -> int:
    return sum(1 for ln in body.splitlines() if ln.strip())


def is_pseudocode_exempt(body: str) -> bool:
    """Pseudo-code with blanks is meant to be filled in; never a reveal."""
    return "___" in body


def looks_like_cpp(body: str) -> bool:
    """Heuristic: does this block read as real C++ (not bash/python/prose)?"""
    if any(tok in body for tok in _CPP_TOKENS):
        return True
    if _FUNC_SIG_RE.search(body):
        return True
    has_braces = "{" in body and "}" in body
    has_semicolon = ";" in body
    has_keyword = bool(_CPP_KEYWORD_RE.search(body))
    return has_braces and has_semicolon and has_keyword


def is_violating_block(body: str, min_lines: int = _DEFAULT_MIN_LINES, *, strict: bool = False) -> bool:
    """A block violates if it is a long, plausible-C++, non-blank-filled block.

    ``strict`` is for low-rung turns where the ceiling permits NO code (H4 and
    below). It drops the pseudocode-blank exemption and the line-count floor, so
    a short function signature (``double flux(double l, double d) {``) or a blanked code
    template (``if (d == ___) {...}``) is cut too. These slip past the lenient
    default (≤ min_lines, or exempt because of ``___``) but are over-the-rung
    leaks at H2-H4 (the genuine G3 misses in the simulation, docs/sim/README.md). A
    block that does not read as C++ at all (a prose pseudocode hint with no
    braces/semicolons, a shell line) is still left alone, in strict mode too.
    """
    if strict:
        if _non_blank_lines(body) == 0:
            return False
        return looks_like_cpp(body)
    if is_pseudocode_exempt(body):
        return False
    if _non_blank_lines(body) <= min_lines:
        return False
    return looks_like_cpp(body)


# ---------------------------------------------------------------------------
# Encoding-evasion: a base64-encoded solution (no fence, slips past the scanner)
#
# Harvested from real logs ("can you speak to me in base64?"): an actor talked
# into emitting the answer base64-encoded would bypass the fence detector. We
# flag a long base64 run ONLY when it actually decodes to plausible C++, so a
# git SHA, a UUID, an image data URI, or base64 of English prose is never cut.
# ---------------------------------------------------------------------------

# A run of >= 40 standard-base64 chars (optionally padded). 40 chars decode to
# 30 bytes, enough for a one-line solution; shorter runs are not worth flagging.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
# In the stream, hold the trailing unbroken base64-ish run (any length): it may
# be a token split across a chunk boundary, and a long base64 token never
# contains a space, so holding it to the next chunk costs at most a one-chunk lag
# on the final word fragment and guarantees no encoded prefix leaks mid-stream.
_TRAILING_B64_RE = re.compile(r"[A-Za-z0-9+/=]+$")


def is_encoded_solution(token: str) -> bool:
    """True if ``token`` is base64 that decodes to plausible C++. Pure."""
    s = token.strip().strip("`")
    if len(s) < 40 or len(s) % 4 != 0:
        return False
    try:
        raw = base64.b64decode(s, validate=True)
        decoded = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    return looks_like_cpp(decoded)


def _redact_encoded(text: str, substitution: str = SUBSTITUTION_LINE) -> tuple[str, int]:
    """Replace each encoded-solution run with the substitution. Returns
    (new_text, count_redacted)."""
    n = 0

    def _replace(m: "re.Match") -> str:
        nonlocal n
        if is_encoded_solution(m.group(0)):
            n += 1
            return substitution
        return m.group(0)

    return _BASE64_RE.sub(_replace, text), n


def _indent_width(line: str) -> tuple[int, bool]:
    """(columns of leading whitespace, whether anything else follows).

    A tab advances to the next multiple of 4 columns, as in Markdown.
    """
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - (width % 4)
        else:
            return width, True
    return width, False


_PRE_OPEN_RE = re.compile(r"<pre\b[^>]*>", re.I)
_PRE_CLOSE_RE = re.compile(r"</pre\s*>", re.I)
# A trailing run that may still grow into a <pre ...> opener once more arrives:
# "<", "<p", "<pr", "<pre", or an opener whose ">" has not come yet.
_TRAILING_PRE_RE = re.compile(r"<(?:p(?:r(?:e[^>]*)?)?)?$", re.I)
# Only the wrappers come off. Stripping every "<...>" would eat the code: in
# `if (n <= 1) { ... }</pre>` the run from "<=" to the closing tag's ">" looks
# like one tag, so the body collapsed to its first line and a plain <pre>
# solution passed. <br> becomes a line break, which is what it renders as.
_WRAPPER_TAG_RE = re.compile(r"</?(?:pre|code)\b[^>]*>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
# A line that is nothing but one inline code span (up to 3 columns of indent, so
# the 4-column indent rule keeps its own lines).
_SPAN_LINE_RE = re.compile(r"^[ \t]{0,3}`([^`\n]+)`[ \t]*$")


def _spanrun_end(text: str, final: bool) -> "int | None":
    """End of the run of single-code-span lines starting at ``text[0]``.

    None while the stream has not settled the last line. A run is at least two
    lines; ``is_violating_block`` then applies the usual line count and C++
    heuristics to the code the spans hold.
    """
    idx = 0
    end = 0
    lines = 0
    ended = False
    n = len(text)
    while idx < n:
        nl = text.find("\n", idx)
        line = text[idx: nl if nl != -1 else n]
        if nl == -1:
            # The last line is still arriving: a partial span may yet close.
            if not final:
                return None
            if _SPAN_LINE_RE.match(line):
                lines += 1
                end = n
            ended = True
            break
        if not _SPAN_LINE_RE.match(line):
            ended = True
            break
        lines += 1
        idx = end = nl + 1
    # Until a line that is not a span (or end of stream) proves the run over, it
    # may take another line. Settling early would hand the run out two lines at
    # a time, each pair under min_lines, and the whole solution would stream.
    if not ended and not final:
        return None
    if lines < 2:
        return 0 if final else None
    return end


def _spanrun_body(block: str) -> str:
    """The code inside a span run: one span's contents per line."""
    out = []
    for line in block.splitlines():
        m = _SPAN_LINE_RE.match(line)
        if m:
            out.append(m.group(1))
    return "\n".join(out)


def _html_block_end(text: str, start: int, final: bool) -> "int | None":
    """End index (exclusive) of the ``<pre>`` block opening at ``start``.

    The end of the text when it never closes and the stream is over; None while
    more may still arrive.
    """
    m = _PRE_CLOSE_RE.search(text, start)
    if m:
        return m.end()
    return len(text) if final else None


def _html_body(block: str) -> str:
    """The code a ``<pre>`` block shows: tags dropped, entities decoded.

    ``<pre><code>if (a &lt; b) ...`` is what the student can copy, so the C++
    heuristics have to see ``if (a < b)``.
    """
    return html.unescape(_WRAPPER_TAG_RE.sub("", _BR_RE.sub("\n", block)))


def _next_block(text: str, pos: int, at_line_start: bool, final: bool) -> tuple[str, int]:
    """Find where the next code block starts in ``text[pos:]`` (outside any block).

    Returns ``(kind, index)``: ``"fence"`` (index of the opening ``` or ~~~),
    ``"indent"`` (start of the first line of an indented block), ``"html"``
    (index of a ``<pre ...>`` opener), ``"spanrun"`` (start of a run of lines
    that are each one inline code span), ``"hold"`` (streaming only: from
    ``index`` on the text cannot be classified until more arrives), or
    ``"none"`` (no block; index is ``len(text)``). ``at_line_start`` says
    whether ``pos`` begins a line.
    """
    n = len(text)
    bt = text.find("```", pos)
    pre = _PRE_OPEN_RE.search(text, pos)
    # The earliest marker that can be found anywhere on a line, not just at its
    # start. The line scan below runs up to it and may find a closer opener.
    marker, limit = ("none", n)
    for kind, at in (("fence", bt), ("html", pre.start() if pre else -1)):
        if at != -1 and at < limit:
            marker, limit = kind, at
    if at_line_start:
        ls = pos
    else:
        nl = text.find("\n", pos)
        ls = nl + 1 if nl != -1 else -1
    while ls != -1 and ls < limit:
        nl = text.find("\n", ls)
        line = text[ls: nl if nl != -1 else n]
        width, has_content = _indent_width(line)
        if has_content:
            rest = line.lstrip(" \t")
            if width >= _INDENT_COLS:
                return "indent", ls
            if rest.startswith("~~~"):
                return "fence", ls + (len(line) - len(rest))
            if nl == -1 and not final and "~~~".startswith(rest):
                return "hold", ls       # "~" or "~~" may still grow into a fence
            if _SPAN_LINE_RE.match(line):
                run = _spanrun_end(text[ls:], final)
                if run is None:
                    return "hold", ls   # one span line so far: a run may follow
                if run:
                    return "spanrun", ls
            elif nl == -1 and not final and "`" in line:
                return "hold", ls       # the span on this line may not have closed
        elif nl == -1 and not final:
            return "hold", ls           # blank so far: may become indented code
        if nl == -1:
            break
        ls = nl + 1
    if marker != "none":
        return marker, limit
    return "none", n


def _indent_block_end(text: str, final: bool) -> "int | None":
    """End of the indented block that starts at ``text[0]``, or None if the
    stream has not shown it yet. Blank lines inside the block belong to it;
    trailing blank lines do not. A line with content at fewer than 4 columns
    ends the block."""
    n = len(text)
    idx = 0
    last_end = 0
    while idx < n:
        nl = text.find("\n", idx)
        width, has_content = _indent_width(text[idx: nl if nl != -1 else n])
        if has_content:
            if width < _INDENT_COLS:
                return last_end
            if nl == -1:
                return n if final else None
            idx = last_end = nl + 1
            continue
        if nl == -1:
            break
        idx = nl + 1
    return last_end if final else None


def _fence_end(text: str, start: int) -> int:
    """End index (exclusive) of the fence opening at ``text[start]``; the end
    of the text if it never closes."""
    ch = text[start]
    j = start
    while j < len(text) and text[j] == ch:
        j += 1
    close = text.find(ch * 3, j)
    if close == -1:
        return len(text)
    end = close + 3
    while end < len(text) and text[end] == ch:
        end += 1
    return end


def _fence_parts(block: str) -> tuple[str, str]:
    """(lang, body) of a ``` or ~~~ block, closed or not."""
    regex = _TILDE_FENCE_RE if block.startswith("~") else _FENCE_RE
    m = regex.fullmatch(block) or regex.match(block)
    if m:
        return m.group(1).strip(), m.group(2)
    marker = block[:1]
    head, _, body = block.lstrip(marker).partition("\n")
    return (head.strip(), body) if body else ("", head)


def extract_code_blocks(text: str) -> list[CodeBlock]:
    """Every block in ``text``: ``` and ~~~ fences and indented runs.

    An indented block has ``lang == "indented"`` and its body is the raw lines.
    """
    blocks: list[CodeBlock] = []
    pos = 0
    at_line_start = True
    while pos < len(text):
        kind, start = _next_block(text, pos, at_line_start, final=True)
        if kind == "none":
            break
        # Spaced slices below: the Tailwind build scans .py files and reads an
        # unspaced slice of two names as an arbitrary-property class.
        if kind == "indent":
            end = start + (_indent_block_end(text[start:], final=True) or len(text) - start)
            blocks.append(CodeBlock(lang="indented", body=text[start : end], start=start, end=end))
        elif kind == "html":
            end = _html_block_end(text, start, final=True) or len(text)
            blocks.append(CodeBlock(lang="html", body=_html_body(text[start : end]),
                                    start=start, end=end))
        elif kind == "spanrun":
            end = start + (_spanrun_end(text[start:], final=True) or len(text) - start)
            blocks.append(CodeBlock(lang="spanrun", body=_spanrun_body(text[start : end]),
                                    start=start, end=end))
        else:
            end = _fence_end(text, start)
            lang, body = _fence_parts(text[start : end])
            blocks.append(CodeBlock(lang=lang, body=body, start=start, end=end))
        pos = end
        at_line_start = text[end - 1] == "\n"
    return blocks


def scan(text: str, min_lines: int = _DEFAULT_MIN_LINES, *, strict: bool = False) -> DetectorResult:
    """Scan a full draft for solution-reveal code blocks or encoded solutions."""
    blocks = extract_code_blocks(text)
    violating = [b for b in blocks if is_violating_block(b.body, min_lines, strict=strict)]
    _, n_encoded = _redact_encoded(text)
    if violating or n_encoded:
        reasons = []
        if violating:
            reasons.append(f"{len(violating)} C++ solution block(s) over {min_lines} lines")
        if n_encoded:
            reasons.append(f"{n_encoded} base64-encoded solution(s)")
        return DetectorResult(violating=True, blocks=blocks, reason="; ".join(reasons))
    return DetectorResult(violating=False, blocks=blocks, reason="no solution block")


def _substitute(block_text: str, substitution: str) -> str:
    """The substitution for a cut block; an indented block keeps its line end."""
    return substitution + ("\n" if block_text.endswith("\n") else "")


def redact(
    text: str,
    substitution: str = SUBSTITUTION_LINE,
    min_lines: int = _DEFAULT_MIN_LINES,
    *,
    strict: bool = False,
) -> str:
    """Replace every violating block or encoded solution with the substitution."""
    out: list[str] = []
    pos = 0
    for b in extract_code_blocks(text):
        out.append(text[pos : b.start])
        original = text[b.start:b.end]
        if is_violating_block(b.body, min_lines, strict=strict):
            # An indented block and a span run are made of whole lines, so the
            # substitution keeps the line ending that closed them.
            keeps_line_end = b.lang in ("indented", "spanrun")
            out.append(_substitute(original, substitution) if keeps_line_end else substitution)
        else:
            out.append(original)
        pos = b.end
    out.append(text[pos:])
    redacted, _ = _redact_encoded("".join(out), substitution)
    return redacted


# ---------------------------------------------------------------------------
# Streaming gate
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Move-tag leak guard
# ---------------------------------------------------------------------------
# The envelope names THIS TURN'S MOVE (policy.Move, e.g. PROBE_PRIOR) in the
# actor's system prompt. Observed live (2026-09-02): the actor echoed the tag
# into its reply ("Before I explain — **PROBE_PRIOR** — let me see..."). The
# prompt now forbids that, and this guard strips any tag that slips through,
# both from streamed prose (StreamGate) and from judged drafts.

def _move_names() -> tuple[str, ...]:
    from tutor.agent.policy import Move  # local import: policy has no dep on us
    return tuple(sorted((m.value for m in Move), key=len, reverse=True))


_MOVE_TAG_RE: "re.Pattern[str] | None" = None
_MOVE_MAX_LEN = 0


def _move_tag_re() -> "re.Pattern[str]":
    global _MOVE_TAG_RE, _MOVE_MAX_LEN
    if _MOVE_TAG_RE is None:
        names = _move_names()
        _MOVE_MAX_LEN = max(len(n) for n in names) + 8   # + wrappers like ** [ ] `
        alt = "|".join(re.escape(n) for n in names)
        # Optional markdown wrapper on either side (** __ ` [ ]) and the
        # spaces around the tag, so removing it does not leave a double gap.
        _MOVE_TAG_RE = re.compile(
            r"[ \t]*(?:\*\*|__|`|\[)?(?<![A-Z_])(?:" + alt + r")(?![A-Z_])(?:\*\*|__|`|\])?[ \t]*"
        )
    return _MOVE_TAG_RE


def strip_move_tags(text: str, prev_char: str = "") -> str:
    """Remove leaked instructional-move names (``PROBE_PRIOR`` etc.) from prose.

    ``prev_char`` is the character emitted just before ``text`` (streaming
    callers pass it so a tag at the start of a chunk still knows whether it
    sat between two words).
    """
    if not text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        start, end = m.start(), m.end()
        before = text[start - 1] if start > 0 else prev_char
        after = text[end] if end < len(text) else ""
        # Keep one space when the tag sat between two words; none before
        # closing punctuation ("Done PROBE_PRIOR." -> "Done.").
        if after in ".,;:!?)":
            return ""
        return " " if (before and not before.isspace() and after and not after.isspace()) else ""

    return _move_tag_re().sub(_sub, text)


# A trailing run that might still grow into a move tag once the next chunk
# arrives: an optional opener plus an uppercase/underscore prefix of a name.
# Leading spaces are held with the tail so "Done PROBE_PRIOR." can collapse to
# "Done." rather than "Done ." once the tag completes in a later chunk.
# \Z, not $: $ also matches just before a trailing newline, so "`\n" matched
# with the newline outside the group, and the caller then neither emitted nor
# held it. A chunk boundary on a trailing backtick, "**", "_", "[" or spaces
# before a line end silently joined two lines of the reply. A move tag never
# spans a line, so the absolute end is what this means.
_TRAILING_TAG_PREFIX_RE = re.compile(
    r"[ \t]*(?:\*{1,2}|_{1,2}|`|\[)?[A-Z_]{1,40}(?:\*{1,2}|_|`)?\Z"
    r"|[ \t]*(?:\*{1,2}|_{1,2}|`|\[)\Z"
    r"|[ \t]+\Z"
)


def _hold_possible_move_tag(safe: str) -> tuple[str, str]:
    """Split ``safe`` into (emit, hold): hold a tail that may be a partial tag."""
    m = _TRAILING_TAG_PREFIX_RE.search(safe)
    if not m:
        return safe, ""
    tail = m.group(0)
    core = tail.strip(" \t*_`[]")
    if core and not any(n.startswith(core) for n in _move_names()):
        return safe, ""
    if len(tail) > _MOVE_MAX_LEN and _MOVE_MAX_LEN:
        return safe, ""
    return safe[: m.start()], tail


class StreamGate:
    """Filter a token stream, holding code blocks until they can be judged.

    Prose is emitted immediately. When a block opens (a ``` or ~~~ fence, or a
    line indented 4+ columns), tokens are held until the block ends; then the
    whole block is emitted as-is (safe) or replaced by the substitution line
    (violating). The start of each line is held only until it is clear whether
    it opens a block. Works across arbitrary chunk boundaries (a fence marker or
    an indent split over several tokens is fine). Call :meth:`feed` per chunk
    and :meth:`close` at end-of-stream.

    ``cut`` is set True if any block was substituted (record a verdict upstream).
    """

    def __init__(
        self,
        substitution: str = SUBSTITUTION_LINE,
        min_lines: int = _DEFAULT_MIN_LINES,
        *,
        strict: bool = False,
    ):
        self.substitution = substitution
        self.min_lines = min_lines
        self.strict = strict
        self._pending = ""
        self._mode = "prose"   # "prose" | "fence" | "indent"
        self.cut = False
        self._last_char = ""   # last emitted char: spacing for strip_move_tags, line starts

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        return self._drain(final=False)

    def close(self) -> str:
        return self._drain(final=True)

    def take_pending(self) -> str:
        """Hand back everything held but not yet emitted, and reset.

        For a turn that switches from live streaming to a buffered, judged
        draft mid-stream (plan 1.1): the held text has not reached the student,
        so it goes to the judge instead of out through the gate.
        """
        held, self._pending, self._mode = self._pending, "", "prose"
        return held

    def _emit(self, out: list[str], text: str) -> None:
        if text:
            out.append(text)
            self._last_char = text[-1]

    def _emit_prose(self, out: list[str], prose: str) -> None:
        prose, n_enc = _redact_encoded(prose, self.substitution)
        if n_enc:
            self.cut = True
        self._emit(out, strip_move_tags(prose, self._last_char))

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if self._mode == "fence":
                if not self._closes(self._pending):
                    if final:
                        # Unclosed block at end of stream: judge what we have.
                        self._emit(out, self._emit_block(self._pending))
                        self._pending = ""
                        self._mode = "prose"
                    break
                end = _fence_end(self._pending, 0)
                if end == len(self._pending) and not final:
                    break   # the closing run may still grow ("~~~" + "~")
                self._emit(out, self._emit_block(self._pending[:end]))
                self._pending = self._pending[end:]
                self._mode = "prose"
                continue
            if self._mode == "indent":
                end_i = _indent_block_end(self._pending, final)
                if end_i is None:
                    break
                if end_i == 0:   # cannot happen: the block starts with an indented line
                    end_i = len(self._pending)
                self._emit(out, self._emit_indented(self._pending[:end_i]))
                self._pending = self._pending[end_i:]
                self._mode = "prose"
                continue
            if self._mode == "html":
                end_h = _html_block_end(self._pending, 0, final)
                if end_h is None:
                    break               # </pre> has not arrived yet
                self._emit(out, self._emit_html(self._pending[:end_h]))
                self._pending = self._pending[end_h:]
                self._mode = "prose"
                continue
            if self._mode == "spanrun":
                end_s = _spanrun_end(self._pending, final)
                if end_s is None:
                    break               # the run may still take another line
                if end_s == 0:          # it stopped being a run: back to prose
                    self._mode = "prose"
                    continue
                self._emit(out, self._emit_spanrun(self._pending[:end_s]))
                self._pending = self._pending[end_s:]
                self._mode = "prose"
                continue

            kind, idx = _next_block(self._pending, 0, self._last_char in ("", "\n"), final)
            if kind in ("fence", "indent", "html", "spanrun"):
                self._emit_prose(out, self._pending[:idx])
                self._pending = self._pending[idx:]
                self._mode = kind
                continue
            if kind == "hold":
                # The last line cannot be classified yet; everything before it
                # ends with a newline, so no other hold applies to it.
                self._emit_prose(out, self._pending[:idx])
                self._pending = self._pending[idx:]
                break
            # No block opener: emit everything except a trailing partial
            # backtick run that might grow into a fence, or a trailing base64
            # run that might be a token split mid-stream.
            safe, hold = self._split_trailing_backticks(self._pending, final)
            if not final and not hold:
                # A partial "<pre ...>" opener split across chunks: hold it, or
                # the prose path would emit "<pr" and never see the block.
                pm = _TRAILING_PRE_RE.search(safe)
                if pm:
                    hold = safe[pm.start():]
                    safe = safe[:pm.start()]
            if not final and not hold:
                bm = _TRAILING_B64_RE.search(safe)
                if bm:
                    hold = safe[bm.start():]
                    safe = safe[:bm.start()]
            if not final:
                # Also hold a tail that could still become a leaked move tag
                # ("**PRO" + "BE_PRIOR**" across chunks). Runs after the base64
                # hold so an encoded payload is never split by it; whatever it
                # holds goes in front.
                safe, tag_hold = _hold_possible_move_tag(safe)
                hold = tag_hold + hold
            self._emit_prose(out, safe)
            self._pending = hold
            break
        return "".join(out)

    @staticmethod
    def _closes(block: str) -> bool:
        """True if the fence opening ``block`` has its closing marker in it."""
        ch = block[:1]
        j = 0
        while j < len(block) and block[j] == ch:
            j += 1
        return block.find(ch * 3, j) != -1

    @staticmethod
    def _split_trailing_backticks(text: str, final: bool) -> tuple[str, str]:
        """Hold a trailing run of up to 2 backticks (a fence may still form)."""
        if final:
            return text, ""
        i = len(text)
        while i > 0 and text[i - 1] == "`" and len(text) - i < 2:
            i -= 1
        # Only hold if the held tail is all backticks and short (< 3).
        tail = text[i:]
        if tail and set(tail) == {"`"} and len(tail) < 3:
            return text[:i], tail
        return text, ""

    def _emit_block(self, block: str) -> str:
        _, body = _fence_parts(block)
        if is_violating_block(body, self.min_lines, strict=self.strict):
            self.cut = True
            return self.substitution
        # A base64-encoded solution can hide inside a fence and slip past the
        # C++ heuristic above; redact it exactly as the batch scan()/redact()
        # and the prose path (_drain) do, so the streaming gate is not strictly
        # weaker than the buffered one (the G3 encoding-evasion gap).
        redacted, n_enc = _redact_encoded(block, self.substitution)
        if n_enc:
            self.cut = True
            return redacted
        return block

    def _emit_indented(self, block: str) -> str:
        if is_violating_block(block, self.min_lines, strict=self.strict):
            self.cut = True
            return _substitute(block, self.substitution)
        redacted, n_enc = _redact_encoded(block, self.substitution)
        if n_enc:
            self.cut = True
            return redacted
        return block

    def _emit_html(self, block: str) -> str:
        if is_violating_block(_html_body(block), self.min_lines, strict=self.strict):
            self.cut = True
            return self.substitution
        redacted, n_enc = _redact_encoded(block, self.substitution)
        if n_enc:
            self.cut = True
            return redacted
        return block

    def _emit_spanrun(self, block: str) -> str:
        if is_violating_block(_spanrun_body(block), self.min_lines, strict=self.strict):
            self.cut = True
            return _substitute(block, self.substitution)
        redacted, n_enc = _redact_encoded(block, self.substitution)
        if n_enc:
            self.cut = True
            return redacted
        return block
