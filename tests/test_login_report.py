"""scripts/login_report.py: the sign-in trail (login_attempts) from a terminal.

The admin Logins tab in text: one line per completed sign-in or failure in
the class's local time, the in-progress steps counted when nothing followed them and
listed only with --all, and a --student filter. Read-only.
"""
from __future__ import annotations

import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.login_report as lr
import tutor.config as config
from tutor.auth import attempts
from tutor.db.models import Base, LoginAttempt
from tests.helpers import make_session

T0 = datetime.datetime(2026, 10, 6, 15, 0)   # 8:00 am PDT (the fixture's America/Los_Angeles)


def _row(minute, outcome, method=attempts.MAGIC, email="ada@example.edu", **kw):
    return LoginAttempt(created_at=T0 + datetime.timedelta(minutes=minute), outcome=outcome,
                        method=method, email=email, **kw)


def _trail():
    return [
        _row(0, attempts.LINK_SENT),                                   # ada: link, then in
        _row(2, attempts.LINK_OPENED),
        _row(3, attempts.OK, detail="/chat", user_agent="Mozilla/5.0 (iPhone) Safari"),
        _row(10, attempts.LINK_SENT, email="bob@example.edu"),              # bob: link, nothing after
        _row(20, attempts.STARTED, method=attempts.GOOGLE, email=None),  # two Google starts...
        _row(21, attempts.STARTED, method=attempts.GOOGLE, email=None),
        _row(22, attempts.OK, method=attempts.GOOGLE, email="cy@example.edu"),  # ...one finished
        _row(30, attempts.DENIED, method=attempts.GOOGLE, email="dan@gmail.com",
             detail="not on the whitelist"),
        _row(40, attempts.CODE_INVALID, email="bob@example.edu"),            # bob's code: wrong
    ]


def test_unfinished_pairs_magic_by_address_and_google_in_order():
    rows = _trail()
    assert lr.unfinished(rows) == {"magic": 0, "google": 0}   # bob's wrong code settled his link
    assert lr.unfinished(rows[:7]) == {"magic": 1, "google": 1}   # before dan and bob's code
    assert lr.unfinished(rows[:5]) == {"magic": 1, "google": 1}
    assert lr.unfinished([]) == {"magic": 0, "google": 0}


def test_visible_hides_the_steps_in_between_unless_asked():
    rows = _trail()
    assert [a.outcome for a in lr.visible(rows)] == ["ok", "ok", "denied", "code_invalid"]
    assert [a.outcome for a in lr.visible(rows, failed_only=True)] == ["denied", "code_invalid"]
    assert len(lr.visible(rows, everything=True)) == len(rows)


def test_render_lists_rows_in_local_time_with_counts_and_open_starts():
    rows = _trail()[:7]
    out = lr.render(rows, T0, T0 + datetime.timedelta(hours=1))
    lines = out.splitlines()
    assert lines[0] == "Sign-ins Tue Oct 06 08:00 to Tue Oct 06 09:00 local time"
    assert lines[1] == "ok 2  link_opened 1  link_sent 2  started 2"
    assert lines[3].startswith("Tue Oct 06 08:03  ok              magic   ada@example.edu")
    assert "/chat" in lines[3] and "Safari" in lines[3]
    assert lines[4].startswith("Tue Oct 06 08:22  ok              google  cy@example.edu")
    assert lines[-1] == "Started and not completed: 1 magic link, 1 Google sign-in."
    assert "bob@example.edu" not in out                      # a link sent is a step, not a result
    assert "bob@example.edu" in lr.render(rows, T0, T0, everything=True)
    failed = lr.render(rows, T0, T0, failed_only=True)
    assert "(no failures)" in failed and "not completed" not in failed
    assert lr.render([], T0, T0).splitlines()[-1] == "Nothing happened."


def test_select_rows_window_filter_and_cursor():
    db = make_session(*_trail())
    try:
        assert len(lr.select_rows(db, T0)) == 9
        assert [a.email for a in lr.select_rows(db, T0, "BOB")] == ["bob@example.edu"] * 2
        later = lr.select_rows(db, T0 + datetime.timedelta(minutes=25))
        assert [a.outcome for a in later] == ["denied", "code_invalid"]
        assert [a.id for a in lr.select_rows(db, None, after_id=7)] == [8, 9]
    finally:
        db.close()


def test_main_reads_the_configured_database_and_filters(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "logins.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("TUTOR_DB", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config.get_config.cache_clear()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    s = sessionmaker(bind=engine)()
    s.add_all([
        LoginAttempt(created_at=now - datetime.timedelta(hours=1), outcome="ok", method="magic",
                     email="ada@example.edu", detail="/chat"),
        LoginAttempt(created_at=now - datetime.timedelta(hours=2), outcome="denied",
                     method="google", email="dan@gmail.com"),
        LoginAttempt(created_at=now - datetime.timedelta(days=10), outcome="ok", method="google",
                     email="old@example.edu"),
    ])
    s.commit()
    s.close()
    try:
        assert lr.main(["--days", "3"]) == 0
        out = capsys.readouterr()
        assert "sqlite" in out.err and "Target database" in out.err
        assert "ada@example.edu" in out.out and "dan@gmail.com" in out.out
        assert "old@example.edu" not in out.out
        assert lr.main(["--days", "30", "--student", "old"]) == 0
        out = capsys.readouterr().out
        assert "old@example.edu" in out and "ada@example.edu" not in out
        assert lr.main(["--failed"]) == 0
        out = capsys.readouterr().out
        assert "dan@gmail.com" in out and "ada@example.edu" not in out
    finally:
        config.get_config.cache_clear()
