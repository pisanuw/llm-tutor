"""The book's chapter narration: prose-only text per chapter, one clip per
chapter under the gated /lecture-audio/book/ path, a dry run that bills
nothing, and a player the public page shows only to a signed-in student."""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from click.testing import CliRunner

from pipeline.render import book_audio as ba

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"

SAMPLE = textwrap.dedent('''\
    ---
    title: "Seasons"
    ---

    # Seasons {#sec-ch03}

    > *Week 3, Session 1. AST 101.*

    ## Warmup

    [Sun Path Simulator](https://sims.example.edu/sun-path/) (Lab #3). Find the day the `noon Sun` is **highest** in the sky.

    ```cpp
    double noon_altitude(double lat) { /* code */ }
    ```

    ::: {.gotcha}
    **Red**: Forgetting the axial tilt leaves no seasons at all.
    :::

    | Latitude | Noon altitude |
    |------|---------|
    | 47 | 66 |

    1. Find the noon altitude $h(47)$ by hand.
    2. Then h(23).

    ![sun path diagram](img/sun-path.png)

    <div class="quiz" data-answer="b">
      <div class="question">1. Which latitude has the Sun overhead?</div>
      <ul><li data-choice="a"><label><strong>a.</strong> <code>h(90)</code></label></li>
          <li data-choice="b"><label><strong>b.</strong> <code>h(0)</code></label></li></ul>
      <div class="explain">The Sun is overhead at the equator on an equinox.</div>
    </div>

    The seasons come from the tilt of Earth's axis.[^1]

    [^1]: A footnote.
    ''')


def test_narration_text_reads_prose_only():
    text = ba.narration_text(SAMPLE)
    paragraphs = text.split("\n\n")
    assert paragraphs[0] == "Seasons."
    assert paragraphs[1] == "Week 3, Session 1. AST 101."
    assert "Warmup." in paragraphs
    assert "Sun Path Simulator (Lab #3). Find the day the noon Sun is highest in the sky." in paragraphs
    assert "Red: Forgetting the axial tilt leaves no seasons at all." in paragraphs
    assert "Find the noon altitude h(47) by hand. Then h(23)." in paragraphs
    assert "The seasons come from the tilt of Earth's axis." in paragraphs
    for gone in ("double noon_altitude", "```", ":::", "| Latitude", "img/sun-path.png",
                 "Which latitude has the Sun overhead", "The Sun is overhead", "h(0)",
                 "data-choice", "[^1]", "A footnote", "sec-ch03", "sims.example.edu", "**", "`", "$"):
        assert gone not in text, gone


def test_chapter_slugs_follow_the_quarto_chapter_list(tmp_path):
    # Chapters, then a part's chapters, then the appendices, in listed order.
    assert ba.chapter_slugs(_book(tmp_path) / "_quarto.yml") == ["a", "b", "c"]
    # The book in the repository: Quarto's index first, every chapter a file.
    slugs = ba.chapter_slugs(BOOK / "_quarto.yml")
    assert slugs[0] == "index" and len(slugs) == len(set(slugs)) > 1
    assert all((BOOK / f"{s}.qmd").exists() for s in slugs)


def _book(tmp_path: Path) -> Path:
    book = tmp_path / "book"
    book.mkdir()
    (book / "_quarto.yml").write_text(
        "book:\n  chapters:\n    - a.qmd\n    - part: P\n      chapters: [b.qmd]\n  appendices: [c.qmd]\n")
    (book / "a.qmd").write_text("# A\n\nOne two three.\n")
    (book / "b.qmd").write_text("# B\n\n" + "Word word word. " * 400 + "\n")   # more than one chunk
    (book / "c.qmd").write_text("# C\n\nShort.\n")
    return book


def test_a_spend_writes_one_clip_per_chapter_and_resumes(tmp_path, monkeypatch):
    book = _book(tmp_path)
    calls: list[str] = []

    def synth(text, *, api_key, voice_id, model, cache_dir):
        calls.append(text)
        return b"MP3:" + text[:6].encode()

    monkeypatch.setattr(ba, "_synthesize_chunk", synth)
    monkeypatch.setattr(ba, "_concat_mp3",
                        lambda parts, out: out.write_bytes(b"".join(p.read_bytes() for p in parts)))
    monkeypatch.setattr(ba, "_ffprobe_seconds", lambda p: 60.0)
    out = tmp_path / "out"
    kw = dict(api_key="k", voice_id="v", cache_dir=tmp_path / "cache", verbose=False)

    dry = ba.render_book_audio(book, out, **kw)
    assert calls == [] and not list(out.glob("*.mp3"))
    assert [(c["slug"], c["exists"]) for c in dry["chapters"]] == [("a", False), ("b", False), ("c", False)]

    first = ba.render_book_audio(book, out, spend=True, budget=100, **kw)     # a fits, b would not
    assert first["rendered"] == 1 and first["stopped"].startswith("budget of 100")
    assert [c["exists"] for c in first["chapters"]] == [True, False, False]   # rows past the stop too
    assert (out / "a.mp3").exists() and not (out / "b.mp3").exists()

    rest = ba.render_book_audio(book, out, spend=True, **kw)
    assert rest["skipped"] == 1 and rest["rendered"] == 2                       # a kept, b and c made
    assert (out / "b.mp3").read_bytes().count(b"MP3:") >= 2                     # chunked, then joined
    assert not list(out.glob(".*.mp3"))                                         # no temp parts left
    assert all(len(c) <= 4500 for c in calls)

    only = ba.render_book_audio(book, out, only=["c"], **kw)
    assert [c["slug"] for c in only["chapters"]] == ["c"]

    # The public index names the chapters with a clip, and only a spend writes it.
    assert json.loads((book / "narration-index.json").read_text()) == ["a", "b", "c"]
    (book / "narration-index.json").unlink()
    ba.render_book_audio(book, out, **kw)
    assert not (book / "narration-index.json").exists()


def test_the_cli_dry_run_bills_nothing(monkeypatch, tmp_path):
    from pipeline import __main__ as cli_mod

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    monkeypatch.setattr(ba, "_synthesize_chunk",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("billed")))
    _book(tmp_path)
    monkeypatch.setattr(cli_mod, "ROOT", tmp_path)                             # tmp_path/book, not the repository's
    result = CliRunner().invoke(cli_mod.cli, ["render-book-audio", "--chapter", "b", "--quiet",
                                             "--out", str(tmp_path / "out")])    # not the real output/
    assert result.exit_code == 0, result.output
    assert "1 chapters: 0 cached, 1 to render" in result.output and "Dry run" in result.output


def test_the_book_pages_load_the_gated_narration_player():
    import yaml

    quarto = (BOOK / "_quarto.yml").read_text()
    assert '<script src="narration.js"></script>' in quarto
    # Quarto copies only what the rendered pages reference; the script fetches
    # the index at runtime, so the index has to be a declared resource.
    assert "narration-index.json" in yaml.safe_load(quarto)["project"]["resources"]
    js = (BOOK / "narration.js").read_text()
    assert "/lecture-audio/book/" in js and "credentials: 'include'" in js and "401" in js
    assert js.index("narration-index.json") < js.index("method: 'HEAD'")   # the index gates the probe
    index = json.loads((BOOK / "narration-index.json").read_text())
    assert index == sorted(index) and set(index) <= set(ba.chapter_slugs(BOOK / "_quarto.yml"))
    assert not re.search(r"https?://", js)                                      # same origin only
    assert ".book-narration" in (BOOK / "styles.css").read_text()


def test_a_chunk_is_requested_at_half_the_default_bitrate(tmp_path, monkeypatch):
    """The clips share a disk with the store and the lecture narration."""
    import httpx

    from pipeline.render import audio_renderer as ar

    seen = {}

    def post(url, **kw):
        seen.update(kw)
        return httpx.Response(200, content=b"ID3", request=httpx.Request("POST", url))

    monkeypatch.setattr(ar.httpx, "post", post)
    ar._synthesize_chunk("Hello.", api_key="k", voice_id="v", model="m", cache_dir=tmp_path)
    assert seen["params"] == {"output_format": "mp3_44100_64"}
