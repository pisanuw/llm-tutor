"""Tests for the datetime modernization (tutor.utctime.utcnow).

The app migrated ~25 deprecated ``datetime.datetime.utcnow()`` call sites and
the ORM column defaults to a single ``tutor.utctime.utcnow()`` helper. These
tests pin the two properties that matter:

  1. utcnow() returns a *naive* UTC instant (so it does not introduce a
     naive/aware mismatch against the naive ``DateTime`` columns), and matches
     the wall-clock of the old utcnow() it replaced.
  2. the security-relevant expiry comparisons (DB Session.expires_at, MagicLink
     .expires_at) still decide valid-vs-expired correctly when both sides are
     produced by the new helper and persisted through the ORM round-trip.
"""
from __future__ import annotations

import datetime as _dt

from tutor.db.models import MagicLink, Session as SessionModel, Student
from tutor.utctime import utcnow
from tests.helpers import make_session


def test_utcnow_is_naive_utc():
    now = utcnow()
    # Naive: no tzinfo (matches the ORM's plain DateTime columns).
    assert now.tzinfo is None
    # Same wall-clock as the deprecated utcnow it replaced (within a small skew).
    reference = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    assert abs((reference - now).total_seconds()) < 5


def test_utcnow_comparable_without_tz_crash():
    """A future-vs-now comparison must not raise the naive/aware TypeError."""
    later = utcnow() + _dt.timedelta(minutes=15)
    earlier = utcnow() - _dt.timedelta(minutes=15)
    assert earlier < utcnow() < later  # would TypeError if one side were aware


def _mem_session():
    return make_session()


def test_session_expiry_roundtrip():
    """A live session passes the deps.py expiry filter; an expired one does not.

    Mirrors the WHERE clause in tutor/auth/deps.get_current_student:
    SessionModel.expires_at > utcnow().
    """
    db = _mem_session()
    student = Student(email="exp@example.com", name="Exp")
    db.add(student)
    db.flush()

    live = SessionModel(
        student_id=student.id,
        jwt_jti="live",
        expires_at=utcnow() + _dt.timedelta(days=1),
        revoked=False,
    )
    expired = SessionModel(
        student_id=student.id,
        jwt_jti="expired",
        expires_at=utcnow() - _dt.timedelta(days=1),
        revoked=False,
    )
    db.add_all([live, expired])
    db.commit()

    now = utcnow()
    valid = (
        db.query(SessionModel)
        .filter(SessionModel.revoked == False, SessionModel.expires_at > now)  # noqa: E712
        .all()
    )
    jtis = {s.jwt_jti for s in valid}
    assert jtis == {"live"}

    # The persisted value survives the round-trip as a naive datetime, so the
    # comparison above never hits a naive/aware TypeError.
    reread = db.query(SessionModel).filter_by(jwt_jti="live").one()
    assert reread.expires_at.tzinfo is None
    db.close()


def test_magic_link_expiry_roundtrip():
    """MagicLink.expires_at vs utcnow() (the check in routers/auth.magic_link_login)."""
    db = _mem_session()
    student = Student(email="ml@example.com", name="ML")
    db.add(student)
    db.flush()

    fresh = MagicLink(
        student_id=student.id, token="fresh", expires_at=utcnow() + _dt.timedelta(minutes=15)
    )
    stale = MagicLink(
        student_id=student.id, token="stale", expires_at=utcnow() - _dt.timedelta(minutes=1)
    )
    db.add_all([fresh, stale])
    db.commit()

    now = utcnow()
    assert db.query(MagicLink).filter_by(token="fresh").one().expires_at >= now
    assert db.query(MagicLink).filter_by(token="stale").one().expires_at < now
    db.close()


def test_model_default_is_naive_via_helper():
    """ORM created_at default (now tutor.utctime.utcnow) populates a naive UTC value."""
    db = _mem_session()
    student = Student(email="def@example.com", name="Def")
    db.add(student)
    db.commit()
    db.refresh(student)
    assert student.created_at is not None
    assert student.created_at.tzinfo is None
    db.close()
