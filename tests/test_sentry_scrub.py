"""Sentry events carry no student text or credentials (plan step 0.8, C10).

send_default_pii=False does not stop the SDK from attaching request bodies,
and SQLAlchemy exception messages echo bound parameters, so main.py scrubs
every event before it leaves the process.
"""

from __future__ import annotations

from tutor.main import _scrub_sentry_event


def _event():
    return {
        "request": {
            "url": "https://tutor.example.edu/auth/magic/SeCrEtToKeN123",
            "method": "POST",
            "data": {"comment": "I'm Jane, student id jdoe, this was wrong"},
            "cookies": {"session_token": "eyJ..."},
            "query_string": "student=jane%40example.edu&token=ADMIN",
            "headers": {"Cookie": "session_token=eyJ...", "Authorization": "Bearer x",
                        "X-Admin-Token": "ADMIN", "User-Agent": "Safari"},
        },
        "exception": {"values": [{
            "type": "DataError",
            "value": "(psycopg.errors.StringDataRightTruncation) value too long\n"
                     "[SQL: INSERT INTO messages (content) VALUES (%(content)s)]\n"
                     "[parameters: {'content': 'my student id is jdoe'}]",
        }]},
    }


def test_request_body_cookies_query_and_auth_headers_are_dropped():
    req = _scrub_sentry_event(_event(), None)["request"]
    assert "data" not in req and "cookies" not in req and "query_string" not in req
    assert req["headers"] == {"User-Agent": "Safari"}


def test_magic_link_token_is_redacted_from_the_url():
    req = _scrub_sentry_event(_event(), None)["request"]
    assert req["url"] == "https://tutor.example.edu/auth/magic/[redacted]"


def test_sql_and_parameters_are_cut_from_exception_messages():
    value = _scrub_sentry_event(_event(), None)["exception"]["values"][0]["value"]
    assert "jdoe" not in value and "INSERT" not in value
    assert value.startswith("(psycopg.errors.StringDataRightTruncation) value too long")


def test_events_without_a_request_pass_through():
    event = {"message": "boom"}
    assert _scrub_sentry_event(event, None) == {"message": "boom"}
