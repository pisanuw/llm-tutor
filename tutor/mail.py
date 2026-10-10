"""Report email for the scheduled scripts (plan step 5.7, finding B27).

The daily digest, the thumbs-down report, and the weekly instructor report
each carried a copy of the Resend send and of the recipient fallback. They
share these two functions now. The app's own mail (the magic link in
routers/auth.py, the alerts in alerts.py) reads its settings through
tutor.config and keeps its own path.

The scripts run in CI and from a shell, so what happened is printed, not
logged, and a mail failure never raises: it must not fail the job or lose
the report files already written.
"""

from __future__ import annotations

import base64
import os
from typing import Optional, Sequence

DEFAULT_FROM = "tutor@yourdomain.com"


def report_recipient(explicit: Optional[str] = None) -> Optional[str]:
    """The explicit address, else ``REPORT_EMAIL``, else the first
    ``ADMIN_EMAILS``; None when there is none."""
    return ((explicit or "").strip() or os.getenv("REPORT_EMAIL", "").strip()
            or os.getenv("ADMIN_EMAILS", "").split(",")[0].strip() or None)


def send_report(to: Optional[str], subject: str, *, text: str, html: str, tag: str,
                attachments: Optional[Sequence[tuple[str, bytes]]] = None,
                bcc: Optional[str] = None) -> bool:
    """Send one report through Resend; True when it was sent.

    ``tag`` (the script's name) prefixes every printed line. Without
    ``RESEND_API_KEY`` the text is printed instead of sent. ``attachments``
    are ``(filename, bytes)`` pairs; ``bcc`` is a blind copy (the admin's
    copy of an instructor's email), dropped when it is the recipient.
    """
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        print(f"{tag}: no RESEND_API_KEY, printing instead of emailing\n")
        print(text)
        return False
    if not to:
        print(f"{tag}: no recipient (set --email, REPORT_EMAIL, or ADMIN_EMAILS), not sending")
        return False
    try:
        import resend

        resend.api_key = key
        params: resend.Emails.SendParams = {"from": os.getenv("FROM_EMAIL", DEFAULT_FROM), "to": to,
                                            "subject": subject, "html": html, "text": text}
        if attachments:
            params["attachments"] = [{"filename": name, "content": base64.b64encode(data).decode("ascii")}
                                     for name, data in attachments]
        copy = (bcc or "").strip()
        if copy and copy.lower() != to.strip().lower():
            params["bcc"] = copy
        else:
            copy = ""
        resend.Emails.send(params)
    except Exception as exc:  # one bad address or a mail outage must not kill the run
        print(f"{tag}: email to {to} failed ({exc})")
        return False
    print(f"{tag}: emailed {to}" + (f" (copy to {copy})" if copy else ""))
    return True
