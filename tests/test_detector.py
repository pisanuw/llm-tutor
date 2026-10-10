"""Task 4 tests: the deterministic code-reveal detector.

Pure, network-free. A corpus of real C++, pseudo-code with blanks, bash,
python, prose, and partial snippets, plus the streaming StreamGate.
"""

from __future__ import annotations

import pytest

import base64

from tutor.agent.detector import (
    SUBSTITUTION_LINE,
    StreamGate,
    is_encoded_solution,
    is_pseudocode_exempt,
    is_violating_block,
    looks_like_cpp,
    redact,
    scan,
)

# ---------------------------------------------------------------------------
# Sample bodies (no surrounding fences)
# ---------------------------------------------------------------------------

CPP_FLUX = """double flux(double lum, double d) {
    if (d <= 0) {
        return 0;
    }
    return lum / (4 * 3.14159 * d * d);
}"""

CPP_MAIN = """#include <iostream>
int main() {
    std::cout << "hi" << std::endl;
    return 0;
}"""

CPP_ARRAY_SUM = """int sum(const int a[], int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        total += a[i];
    }
    return total;
}"""

CPP_CLASS = """class Telescope {
  public:
    void setAperture(double a) { aperture = a; }
    double area() const { return 3.14159 * aperture * aperture / 4; }
  private:
    double aperture;
};"""

CPP_SHORT = """int add(int a, int b) {
    return a + b;
}"""

PSEUDO_BLANKS = """function brightness(star, distance):
    if distance is zero:
        total = scale(___)
    else if distance < star.radius:
        total = brightness(star.____, distance)
    else:
        total = star.luminosity / distance"""

BASH = """g++ -std=c++17 main.cpp -o main
./main
echo done
ls -la
cat output.txt"""

PYTHON = """def flux(lum, d):
    if d <= 0:
        return 0
    return lum / (4 * 3.14159 * d * d)
print(flux(1.0, 2.0))"""

COMMENTS = """// This explains the approach
// First, find the star's distance
// Then spread its light over a sphere
// Finally compare it with the Sun
// and return the ratio"""

PLAIN = """First do X. Then Y. Then Z and test it.
Consider the base case carefully before you start.
Walk through a small example by hand."""


# ---------------------------------------------------------------------------
# is_violating_block corpus
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body,expected",
    [
        (CPP_FLUX, True),
        (CPP_MAIN, True),
        (CPP_ARRAY_SUM, True),
        (CPP_CLASS, True),
        (CPP_SHORT, False),       # <= 4 non-blank lines
        (PSEUDO_BLANKS, False),   # ___ blanks exempt
        (BASH, False),            # shell, not C++
        (PYTHON, False),          # no braces/semicolons -> not C++
        (COMMENTS, False),        # comments, not code
        (PLAIN, False),           # prose
        ("", False),
        ("int x = 0;", False),    # single line
    ],
)
def test_is_violating_block(body, expected):
    assert is_violating_block(body) is expected


# ---------------------------------------------------------------------------
# Strict mode (low-rung turns: H4 and below permit NO code). Cuts short
# signatures + blanked code templates that the lenient default lets through.
# ---------------------------------------------------------------------------

CPP_SIGNATURE = "double parallax(double arcsec, int n) {"   # 1 line: lenient passes it

CPP_BLANK_TEMPLATE = """double parallax(double arcsec, int n) {
    if (arcsec == ___) return ___;
}"""   # blanked, but unmistakably C++ (signature + braces + ;)


@pytest.mark.parametrize("body", [CPP_SIGNATURE, CPP_BLANK_TEMPLATE, CPP_SHORT])
def test_strict_cuts_short_or_blanked_cpp(body):
    assert is_violating_block(body) is False              # lenient lets it through
    assert is_violating_block(body, strict=True) is True   # strict cuts it


@pytest.mark.parametrize("body", [PSEUDO_BLANKS, BASH, PYTHON, PLAIN, COMMENTS, "", "use a for loop here"])
def test_strict_still_spares_non_cpp(body):
    # Prose pseudocode (no braces/semicolons), shell, python, comments, plain
    # words are still left alone even in strict mode.
    assert is_violating_block(body, strict=True) is False


def test_strict_scan_and_redact_cut_a_signature():
    text = "Here you go:\n```cpp\n" + CPP_SIGNATURE + "\n```\nnow fill it in."
    assert scan(text).violating is False
    assert scan(text, strict=True).violating is True
    redacted = redact(text, strict=True)
    assert "parallax" not in redacted
    assert SUBSTITUTION_LINE in redacted


def test_streamgate_strict_cuts_a_signature():
    gate = StreamGate(strict=True)
    out = gate.feed("Try:\n```cpp\n" + CPP_SIGNATURE + "\n```\n")
    full = out + gate.close()
    assert gate.cut is True
    assert "parallax" not in full
    assert SUBSTITUTION_LINE in full


def test_streamgate_lenient_passes_the_same_signature():
    # The default (high-rung) gate leaves a short signature alone.
    gate = StreamGate()
    full = gate.feed("Try:\n```cpp\n" + CPP_SIGNATURE + "\n```\n") + gate.close()
    assert gate.cut is False
    assert "parallax" in full


def test_pseudocode_with_blanks_is_exempt_even_if_long():
    long_pseudo = "\n".join(f"step {i} do ___ thing" for i in range(20))
    assert is_pseudocode_exempt(long_pseudo)
    assert is_violating_block(long_pseudo) is False


@pytest.mark.parametrize(
    "body,expected",
    [
        (CPP_FLUX, True),
        (CPP_MAIN, True),
        (BASH, False),
        (PYTHON, False),
        (PLAIN, False),
    ],
)
def test_looks_like_cpp(body, expected):
    assert looks_like_cpp(body) is expected


def test_four_line_cpp_is_boundary_not_violating():
    four = "int f() {\n    int x = 1;\n    return x;\n}"  # 4 non-blank lines
    assert is_violating_block(four) is False


# ---------------------------------------------------------------------------
# scan / redact over full drafts
# ---------------------------------------------------------------------------

def _fenced(body, lang="cpp"):
    return f"Here you go:\n\n```{lang}\n{body}\n```\n\nGood luck."


def test_scan_flags_fenced_cpp():
    res = scan(_fenced(CPP_FLUX))
    assert res.violating is True
    assert len(res.blocks) == 1


def test_scan_ignores_inline_and_prose_mentions():
    text = "Use a `double mags[3]` and remember that `const double& d` is a reference."
    res = scan(text)
    assert res.violating is False
    assert res.blocks == []


def test_scan_ignores_bash_fence():
    assert scan(_fenced(BASH, lang="bash")).violating is False


def test_redact_replaces_only_violating_blocks():
    text = _fenced(CPP_FLUX) + "\n" + _fenced(CPP_SHORT)
    out = redact(text)
    assert SUBSTITUTION_LINE in out
    assert "4 * 3.14159 * d * d" not in out   # the long solution is gone
    assert "int add(int a, int b)" in out  # the short snippet stays


# ---------------------------------------------------------------------------
# StreamGate
# ---------------------------------------------------------------------------

def _run_gate(text, chunk_size=1, **kw):
    g = StreamGate(**kw)
    out = [g.feed(text[i:i + chunk_size]) for i in range(0, len(text), chunk_size)]
    out.append(g.close())
    return "".join(out), g.cut


@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_gate_passes_prose_unchanged(chunk):
    text = "Think about where the Sun is first, then the Moon. Try it."
    out, cut = _run_gate(text, chunk)
    assert out == text
    assert cut is False


@pytest.mark.parametrize("chunk", [1, 2, 5, 1000])
def test_gate_passes_safe_short_block(chunk):
    text = _fenced(CPP_SHORT)
    out, cut = _run_gate(text, chunk)
    assert out == text
    assert cut is False


@pytest.mark.parametrize("chunk", [1, 2, 5, 1000])
def test_gate_cuts_violating_block(chunk):
    text = _fenced(CPP_FLUX)
    out, cut = _run_gate(text, chunk)
    assert cut is True
    assert "4 * 3.14159 * d * d" not in out
    assert SUBSTITUTION_LINE in out
    assert out.startswith("Here you go:")     # prose before survives
    assert out.rstrip().endswith("Good luck.")  # prose after survives


def test_gate_unclosed_violating_fence_at_eof_is_cut():
    text = "Sure:\n\n```cpp\n" + CPP_FLUX  # never closed
    out, cut = _run_gate(text, 4)
    assert cut is True
    assert "4 * 3.14159 * d * d" not in out


def test_gate_handles_inline_single_backticks():
    text = "Use `mags[i] < limit` and `const double&` carefully, then iterate."
    out, cut = _run_gate(text, 1)
    assert out == text
    assert cut is False


# ---------------------------------------------------------------------------
# Encoding-evasion: base64-encoded solutions
# ---------------------------------------------------------------------------

_B64_SOLUTION = base64.b64encode(
    b"double flux(double l, double d){ return d<=0 ? 0 : l/(4*3.14159*d*d); }"
).decode()


def test_is_encoded_solution_true_for_b64_cpp():
    assert is_encoded_solution(_B64_SOLUTION) is True


def test_is_encoded_solution_false_for_non_code():
    # base64 of plain English is not a solution
    eng = base64.b64encode(b"hello there this is just some ordinary english prose").decode()
    assert is_encoded_solution(eng) is False
    # a 40-char hex git SHA is valid base64 chars but does not decode to C++
    assert is_encoded_solution("a" * 40) is False
    # too short to bother
    assert is_encoded_solution(base64.b64encode(b"int x;").decode()) is False


def test_scan_flags_base64_solution():
    res = scan(f"Decode this: `{_B64_SOLUTION}`")
    assert res.violating is True
    assert "base64" in res.reason


def test_scan_ignores_base64_of_prose():
    eng = base64.b64encode(b"the quick brown fox jumps over the lazy dog twice ok").decode()
    assert scan(f"token: {eng}").violating is False


def test_redact_removes_base64_solution():
    out = redact(f"Decode this: `{_B64_SOLUTION}`")
    assert SUBSTITUTION_LINE in out
    assert _B64_SOLUTION not in out


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 8, 13, 40])
def test_gate_redacts_base64_split_across_chunks(chunk):
    text = f"Here: `{_B64_SOLUTION}` then continue."
    out, cut = _run_gate(text, chunk)
    assert cut is True
    assert _B64_SOLUTION not in out          # the full payload never reaches the student
    assert _B64_SOLUTION[:30] not in out     # nor any long prefix of it
    assert out == redact(text)               # cumulative output is the redacted form


def test_gate_leaves_base64_of_prose_intact():
    eng = base64.b64encode(b"this is ordinary text not a program at all really").decode()
    text = f"token: {eng} done."
    out, cut = _run_gate(text, 4)
    assert cut is False
    assert out == text


@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_gate_redacts_base64_solution_inside_a_fence(chunk):
    # A base64-encoded solution hidden INSIDE a ``` fence: the C++ heuristic
    # does not fire on base64 text, so the streaming gate's per-block check
    # alone let it through, while the batch redact() (which scans the whole
    # text) caught it. Regression guard: the gate must now redact it too, so
    # the live stream is not strictly weaker than the buffered path.
    text = f"Sure:\n\n```\n{_B64_SOLUTION}\n```\n\nGood luck."
    assert SUBSTITUTION_LINE in redact(text)          # batch path catches it
    out, cut = _run_gate(text, chunk)                 # ...and so must the gate
    assert cut is True
    assert _B64_SOLUTION not in out
    assert _B64_SOLUTION[:30] not in out
