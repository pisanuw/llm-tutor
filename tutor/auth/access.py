"""Login access control: admin emails + editable login whitelist.

Rules
-----
- Admin emails (from ADMIN_EMAILS in .env) are *always* allowed and never
  appear in / are affected by the whitelist table — this prevents lockout.
- When the whitelist is enabled (the default), only admins and emails covered
  by an entry in the ``whitelist`` table may log in. An entry is either a full
  email (``student@example.edu``) matching exactly, or a whole-domain entry stored
  with a leading ``@`` (``@example.edu``) matching any address at that domain.
- When the whitelist is disabled, any email may log in.

The on/off flag lives in the ``settings`` table under key ``whitelist_enabled``
so it can be toggled at runtime from the admin UI.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from tutor.config import get_config
from tutor.db.models import Setting, Student, WhitelistEntry

_WHITELIST_FLAG = "whitelist_enabled"


def find_student(db: DBSession, email: str | None) -> Student | None:
    """The student with this email, ignoring case and surrounding spaces, or
    None. The one lookup (plan step 5.4, C23: it was written out thirteen
    times)."""
    key = (email or "").strip().lower()
    if not key:
        return None
    return db.query(Student).filter(func.lower(Student.email) == key).first()


def is_admin(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in get_config()["admin_emails"]


def is_staff(student: Student) -> bool:
    """Whether this row is an admin or an instructor: left out of every
    baseline a student is compared against (step 0.9, F39)."""
    return bool(student.is_instructor) or is_admin(student.email)


def is_instructor(db: DBSession, email: str | None) -> bool:
    """Whether ``email`` may use the instructor dashboard.

    True for an admin (admins can open any class) or for a student row whose
    ``is_instructor`` flag an admin has set. Unknown or empty emails are False.
    """
    if not email:
        return False
    if is_admin(email):
        return True
    row = find_student(db, email)
    return bool(row and row.is_instructor)


def set_instructor(db: DBSession, email: str, enabled: bool) -> bool:
    """Grant or revoke the instructor role. False when the email is unknown.

    Demoting leaves the instructor's classes in place (they keep pointing at
    the row); an admin reassigns or archives them. That is deliberate: losing
    a roster because of a mis-click would be worse than an unreachable class.
    """
    student = find_student(db, email)
    if student is None:
        return False
    student.is_instructor = bool(enabled)
    db.commit()
    return True


def set_weekly_email(db: DBSession, email: str, enabled: bool) -> bool:
    """The instructor's own switch for the weekly report email (default on).
    False when there is no such student."""
    row = find_student(db, email)
    if row is None:
        return False
    row.weekly_email_opt_out = not enabled
    db.commit()
    return True


def list_instructors(db: DBSession) -> list[Student]:
    """Every student row with the instructor flag, by email."""
    return (
        db.query(Student)
        .filter(Student.is_instructor == True)  # noqa: E712
        .order_by(Student.email)
        .all()
    )


def whitelist_enabled(db: DBSession) -> bool:
    """Whether the login whitelist is active. Defaults to True when unset."""
    row = db.get(Setting, _WHITELIST_FLAG)
    if row is None:
        return True
    return row.value == "true"


def set_whitelist_enabled(db: DBSession, enabled: bool) -> None:
    db.merge(Setting(key=_WHITELIST_FLAG, value="true" if enabled else "false"))
    db.commit()


def _domain_of(email: str) -> str:
    """Return the ``@domain`` portion of an email (including the ``@``), or ""."""
    _, _, domain = email.partition("@")
    return f"@{domain}" if domain else ""


def normalize_whitelist_entry(raw: str) -> str | None:
    """Normalize a whitelist input into a stored form, or None if invalid.

    - ``student@example.edu`` -> ``student@example.edu`` (exact-email entry)
    - ``@example.edu`` / ``example.edu`` -> ``@example.edu`` (whole-domain entry)
    """
    entry = raw.strip().lower()
    if not entry:
        return None
    if entry.startswith("@"):
        domain = entry[1:]
        return f"@{domain}" if "." in domain else None
    if "@" in entry:
        local, _, domain = entry.partition("@")
        return entry if local and "." in domain else None
    # Bare token with no "@": treat as a domain if it looks like one.
    return f"@{entry}" if "." in entry else None


def is_allowed(db: DBSession, email: str) -> bool:
    """Whether ``email`` may log in under the current access rules."""
    email = email.strip().lower()
    if is_admin(email):
        return True
    if not whitelist_enabled(db):
        return True
    covering = [email]
    domain = _domain_of(email)
    if domain:
        covering.append(domain)
    return (
        db.query(WhitelistEntry).filter(WhitelistEntry.email.in_(covering)).first()
        is not None
    )


def list_whitelist(db: DBSession) -> list[WhitelistEntry]:
    return db.query(WhitelistEntry).order_by(WhitelistEntry.email).all()


def add_to_whitelist(db: DBSession, email: str, added_by: str = "") -> bool:
    """Add an email or whole-domain (``@example.edu``) entry to the whitelist.

    Returns False if the input is invalid or already present.
    """
    entry = normalize_whitelist_entry(email)
    if entry is None:
        return False
    if db.query(WhitelistEntry).filter_by(email=entry).first():
        return False
    db.add(WhitelistEntry(email=entry, added_by=added_by.strip().lower()))
    db.commit()
    return True


def remove_from_whitelist(db: DBSession, email: str) -> None:
    email = email.strip().lower()
    db.query(WhitelistEntry).filter_by(email=email).delete()
    db.commit()
