"""Plan step 6.2a (finding F15): the consent notice, word for word.

The notice students accept at login is approved text, and two sentences
added since are kept apart as pending that approval: one on instructor access
to a student's conversations and one saying per-page and per-slide usage is
recorded (activity analytics, decision 1). The old tests checked
two substrings, which pass for the approved text, for the approved text plus
anything, and for any page that happens to contain those phrases. Here the
approved paragraphs (tests/data/consent_notice_approved.txt) and the pending
sentences (tests/data/consent_instructor_pending.txt, then
tests/data/consent_activity_pending.txt) are kept apart: the rendered notice
must be exactly the approved text with the pending sentences in their place,
so editing any of them, or dropping one, fails a test. The help page's "Your
data" section is pinned the same way.

Changing the wording is a deliberate act: update the file here in the same
change, and say in the commit whether whoever approves the notice did.
"""
from __future__ import annotations

import html
import pathlib
import re

from fastapi.testclient import TestClient

DATA = pathlib.Path(__file__).resolve().parent / "data"


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _plain(fragment: str) -> str:
    """The text a reader sees: tags out, entities decoded, whitespace
    collapsed, and no space left before punctuation that followed a tag."""
    text = _normalized(html.unescape(re.sub(r"<[^>]+>", " ", fragment)))
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _notice(page: str) -> str:
    start = page.index("<!-- Consent / disclosure notice")
    end = page.index("retention and consent notice.", start) + len("retention and consent notice.")
    return _plain(page[start:end].split("-->", 1)[1])


def _login_page() -> str:
    from tutor.main import app

    resp = TestClient(app).get("/auth/login")
    assert resp.status_code == 200
    return resp.text


def _pending() -> str:
    """The sentences awaiting approval, in the order they appear."""
    return " ".join(_normalized((DATA / name).read_text())
                    for name in ("consent_instructor_pending.txt", "consent_activity_pending.txt"))


def test_the_login_notice_is_the_approved_text_plus_the_pending_sentences():
    notice = _notice(_login_page())
    pending = _pending()
    approved = _normalized((DATA / "consent_notice_approved.txt").read_text())
    assert notice.count(pending) == 1, "a pending sentence was edited, dropped, or moved"
    assert _normalized(notice.replace(pending, " ")) == approved, "the approved consent wording changed"


def test_the_pending_sentences_sit_after_what_is_recorded():
    notice = _notice(_login_page())
    before, after = notice.split(_pending())
    assert before.rstrip().endswith("progress are recorded.") and after.lstrip().startswith("It is a learning tool")


def test_the_help_page_says_the_same_about_data():
    from tutor.main import app

    page = TestClient(app).get("/help").text
    section = page[page.index(">Your data<") - len("<h3 class=\"font-semibold text-gray-900 mb-1\""):]
    section = section[: section.index("</section>")]
    assert _plain(section) == _normalized((DATA / "help_your_data.txt").read_text())
    assert "read your conversations for that class" in _plain(section)


def test_a_one_word_edit_is_caught():
    """The comparison itself: a change of one word in the approved text, or
    in the pending sentence, is a mismatch."""
    approved = _normalized((DATA / "consent_notice_approved.txt").read_text())
    pending = _normalized((DATA / "consent_instructor_pending.txt").read_text())
    rendered = approved.replace("progress are recorded.", f"progress are recorded. {pending}")
    edited = rendered.replace("one year", "two years")
    assert _normalized(edited.replace(pending, " ")) != approved
    reworded = rendered.replace(pending, pending.replace("read your", "review your"))
    assert reworded.count(pending) == 0
