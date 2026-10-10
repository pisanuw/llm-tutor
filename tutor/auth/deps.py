"""FastAPI dependency for authenticated requests."""

from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, HTTPException, status, Request
from jose import JWTError
from sqlalchemy.orm import Session as DBSession

from tutor.auth.access import is_allowed
from tutor.auth.jwt_utils import decode_token
from tutor.db.models import Session as SessionModel, Student, app_engine, get_session_factory
from tutor.utctime import utcnow


def get_db():
    # The shared engine and its pool (plan step 2.6, C1), not a new engine
    # and connection per request.
    factory = get_session_factory(app_engine())
    db = factory()
    try:
        yield db
    finally:
        db.close()


def session_student(db: DBSession, token: Optional[str]) -> Optional[Student]:
    """The student behind a session cookie, or None: no cookie, a malformed
    or expired token, a revoked or expired session, a deleted student, or
    one whose access was removed. A bad token never raises.

    For pages that only change what they show to a signed-in reader (the
    help page, the root redirect, the admin's login check). Three routers
    used to re-implement this, each checking less (plan step 5.4, C23).
    """
    if not token:
        return None
    try:
        payload = decode_token(token)
        student_id = int(payload.get("sub", 0))
    except (JWTError, TypeError, ValueError):
        return None
    live = db.query(SessionModel.id).filter(
        SessionModel.jwt_jti == payload.get("jti"),
        SessionModel.revoked == False,  # noqa: E712
        SessionModel.expires_at > utcnow(),
    ).first()
    if live is None:
        return None
    student = db.get(Student, student_id)
    if student is None or not is_allowed(db, student.email):
        return None
    return student


def session_id_for(db: DBSession, token: Optional[str]) -> Optional[int]:
    """The ``sessions`` row id behind a cookie (one row per login), or None.

    For the activity events: a batch is stamped with the login that sent
    it. The caller has already passed get_current_student, so this only
    reads the id; a bad token is None, never an error.
    """
    if not token:
        return None
    try:
        jti = decode_token(token).get("jti")
    except JWTError:
        return None
    row = db.query(SessionModel.id).filter(SessionModel.jwt_jti == jti).first()
    return int(row[0]) if row else None


def get_current_student(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    db: DBSession = Depends(get_db),
) -> Student:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(session_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from None

    jti = payload.get("jti")
    student_id = int(payload.get("sub", 0))

    now = utcnow()
    sess = db.query(SessionModel).filter(
        SessionModel.jwt_jti == jti,
        SessionModel.revoked == False,  # noqa: E712
        SessionModel.expires_at > now,
    ).first()
    if not sess:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")

    student = db.query(Student).filter_by(id=student_id).first()
    if not student:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Access is re-checked on every request (plan step 2.9, finding C5;
    # decision 12's default: immediate lockout). Taking a student off the
    # whitelist, or turning the whitelist on, used to leave their session
    # working for up to JWT_EXPIRY_DAYS. The session is revoked as well.
    if not is_allowed(db, student.email):
        sess.revoked = True
        db.commit()
        # The one lockout with no trail until review step 0.6 (F31).
        from tutor.auth import attempts

        attempts.record(db, request, method=attempts.SESSION, outcome=attempts.ACCESS_REMOVED,
                        email=student.email, student_id=student.id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Access removed")

    return student
