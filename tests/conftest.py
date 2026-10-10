"""Suite-wide isolation for pytest (plan step 0.10).

pytest imports this file before any test module, so the environment pinned
here is what ``tutor.config`` sees when a test first imports the app:

* ``.env`` loading is switched off (``PYTHON_DOTENV_DISABLED``). An
  operator's ``.env`` may point ``DATABASE_URL`` at a production database, and
  without this a bare ``pytest`` on that machine would open it.
* Every variable the code reads is cleared, so a local run sees the same
  environment as CI no matter what the shell exports. Only the three values
  ``tutor.config`` requires are set, to dummies, so a test that accidentally
  reaches a paid API fails with an auth error instead of spending money.
* The default SQLite database and the TTS cache live in a temp directory,
  never the repo root.
* The course content (courses.yaml, curriculum/, persona/, ...) is the frozen
  fixture in ``tests/fixtures/content`` (``TUTOR_CONTENT_DIR``), never the
  content at the repository root, and the generated decks and knowledge base
  are empty temp folders (``TUTOR_OUTPUT_DIR``, ``TUTOR_CHROMA_DIR``):
  replacing the example course with your own must not break a test.

Tests that need a different value still set it with ``monkeypatch``. The live
simulation (``python -m tests.sim.run_sim --live``) does not run under pytest
and is unaffected.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TEST_DIR = Path(tempfile.mkdtemp(prefix="tutor-tests-"))
atexit.register(shutil.rmtree, TEST_DIR, ignore_errors=True)

# Every environment variable read by tutor/, pipeline/ or scripts/ (grep for
# os.getenv / os.environ). Add new ones here so local runs keep matching CI.
_APP_ENV_VARS = (
    "ACTIVITY_EVENTS", "ACTIVITY_EVENTS_DAYS", "ACTIVITY_HEARTBEAT_S",
    "ACTIVITY_LEVEL", "ADMIN_EMAILS", "ADMIN_TOKEN", "ALERT_COOLDOWN_MINUTES", "API_DOCS",
    "BOOT_WARNINGS",
    "ALERT_EMAILS", "ALLOWED_HOSTS", "ANTHROPIC_API_KEY", "BASE_URL",
    "CLAUDE_MODEL", "CODE_EXEC_BACKEND", "CODE_EXEC_MEM_MB",
    "CODE_EXEC_TIMEOUT_SEC", "DAILY_EXCHANGE_LIMIT", "DATABASE_URL",
    "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_SIZE", "DB_POOL_TIMEOUT",
    "DISCORD_TOKEN", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
    "ENABLE_CODE_EXEC", "FROM_EMAIL", "GITHUB_TOKEN", "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET", "JWT_EXPIRY_DAYS", "LECTURE_AUDIO_DIR",
    "LOG_CONVERSATION_CONTENT", "LOG_LEVEL", "MAGIC_LINK_EXPIRY_MINUTES",
    "MONTHLY_COST_CAP_USD", "RENDER_EXTERNAL_HOSTNAME", "RENDER_GIT_COMMIT",
    "REPORT_EMAIL", "RESEND_API_KEY", "SECRET_KEY", "SENTRY_DSN", "SENTRY_ENV",
    "SLOW_TURN_MS", "SUPABASE_ACCESS_TOKEN", "SUPABASE_PROJECT_REF",
    "TTS_CACHE_DIR", "USE_CLAUDE_CLI", "TUTOR_DB", "CACHE_PREWARM",
    "TUTOR_CONTENT_DIR", "GITHUB_REPOSITORY", "TUTOR_OUTPUT_DIR", "TUTOR_CHROMA_DIR",
)

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _name in _APP_ENV_VARS:
    os.environ.pop(_name, None)
FIXTURE_CONTENT = Path(__file__).resolve().parent / "fixtures" / "content"

os.environ.update({
    "TUTOR_CONTENT_DIR": str(FIXTURE_CONTENT),
    # Generated artifacts too: decks you rendered and a store you built at
    # the repository root are never seen by a test. Tests that need a deck or
    # a store make one in tmp_path and point the config at it.
    "TUTOR_OUTPUT_DIR": str(TEST_DIR / "output"),
    "TUTOR_CHROMA_DIR": str(TEST_DIR / "chromadb"),
    "ANTHROPIC_API_KEY": "test-dummy-key",
    "SECRET_KEY": "test-dummy-secret",
    "ADMIN_TOKEN": "test-dummy-admin-token",
    "TUTOR_DB": str(TEST_DIR / "tutor-test.db"),
    "TTS_CACHE_DIR": str(TEST_DIR / "tts-cache"),
    # No warm-up call to the model from a test's page load.
    "CACHE_PREWARM": "false",
})


# The suite's SQLite refuses what production's Postgres refuses: an
# over-long VARCHAR write, an integer past int4, a NUL in text
# (R12; tests/postgres_strict.py).
import tutor.db.models  # noqa: E402,F401  (its NUL strip must run before the guard)
from tests import postgres_strict  # noqa: E402

postgres_strict.install()


# Modules that keep process state between requests, each with a
# ``_reset_for_tests`` hook: alert dedup, the self-heal cooldown, the stream
# buffers, the photo-transcription quota, the /healthz throttle, and the
# activity events' per-student cap. The hook in tutor.startup is left to the
# tests that need it: it forgets which
# databases were set up, keyed by URL, and a test's own database has its own
# URL, so clearing it everywhere would only set up the shared one again.
_RESET_HOOKS = ("tutor.alerts", "tutor.selfheal", "tutor.streambuf", "tutor.agent.vision",
                "tutor.routers.status", "tutor.activity_events", "tutor.agent.failures",
                "tutor.student_days", "tutor.decks")


@pytest.fixture(autouse=True)
def _isolate():
    """Process state no test may leave behind (plan step 6.2a, finding F17).

    Before and after every test: the cached config is dropped (a value set
    with monkeypatch would otherwise outlive its test). After every test: the
    app's dependency overrides, the per-address caches and limits, the
    in-flight turn claims, and the module state behind each reset hook are
    cleared. A module the test never imported is left alone. With this, the
    suite passes in any order (``pytest -p randomly``).
    """
    from tutor.config import get_config

    get_config.cache_clear()
    yield
    get_config.cache_clear()
    main = sys.modules.get("tutor.main")
    if main is not None:
        main.app.dependency_overrides.clear()
        main._gate_cache.clear()
    for name in _RESET_HOOKS:
        module = sys.modules.get(name)
        if module is not None:
            module._reset_for_tests()
    auth = sys.modules.get("tutor.routers.auth")
    if auth is not None:
        auth._magic_link_log.clear()
    turn = sys.modules.get("tutor.agent.turn")
    if turn is not None:
        turn._INFLIGHT.clear()


@pytest.fixture
def db():
    """An in-memory database with one student (id 1, stu@example.edu), shared by
    every thread (tests/helpers.make_session). A module that seeds more rows
    defines its own ``db``."""
    from tests.helpers import make_session
    from tutor.db.models import Student

    session = make_session(Student(id=1, email="stu@example.edu", name="Stu"))
    yield session
    session.close()


@pytest.fixture
def app_client(db):
    """A TestClient on the app whose requests use ``db``. Nobody is signed
    in: set a real session cookie (tests/helpers.login_cookie), or take
    ``student_client`` or ``admin_client``. The override is cleared after the
    test by ``_isolate``, even when the test fails."""
    from fastapi.testclient import TestClient

    from tutor.auth.deps import get_db
    from tutor.main import app

    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def student_client(app_client, db):
    """``app_client`` with student 1 signed in by a dependency override. The
    real cookie path has its own tests (tests/test_auth_flow.py)."""
    from tutor.auth.deps import get_current_student
    from tutor.db.models import Student
    from tutor.main import app

    student = db.get(Student, 1)
    app.dependency_overrides[get_current_student] = lambda: student
    return app_client


@pytest.fixture
def admin_client(app_client):
    """``app_client`` holding a token admin session, as /admin/login issues
    one (that route has its own tests in tests/test_route_coverage.py)."""
    from tutor.routers.admin import issue_admin_session

    app_client.cookies.set("admin_session", issue_admin_session())
    return app_client


@pytest.fixture
def no_strategist():
    """Keep a turn offline and move-free: run_turn consults the strategist
    (Session 9), which would call a model. A test that wants a move patches
    its own choice."""
    from unittest.mock import patch

    from tutor.agent.strategist import NO_MOVE

    with patch("tutor.agent.strategist.choose_move", return_value=NO_MOVE):
        yield


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Within each module, the browser tests run last.

    Playwright's sync API keeps its event loop running on this thread from
    the module's first browser test until the ``browser`` fixture stops at
    the end of the module, so a later test in that module that calls
    ``asyncio.run()`` fails. This runs after pytest-randomly's shuffle: the
    modules stay whole and each half keeps its order, so ``pytest -p
    randomly`` still varies everything else (plan step 6.2a).
    """
    halves: dict[str, tuple[list, list]] = {}
    for item in items:
        module = item.nodeid.split("::", 1)[0]
        halves.setdefault(module, ([], []))["browser" in getattr(item, "fixturenames", ())].append(item)
    items[:] = [item for plain, browser in halves.values() for item in plain + browser]


@pytest.fixture(scope="module")
def browser():
    """Headless Chromium for the browser tests (see tests/browser_support.py).

    Module-scoped, and stopped when the module ends: Playwright's sync API
    keeps an event loop on this thread while it runs, which later tests that
    start their own loop must not see. Skips the module where Playwright or a
    Chromium it can launch is missing (CI installs neither yet).
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    from tests.browser_support import launch

    pw = sync_api.sync_playwright().start()
    try:
        chromium = launch(pw)
    except Exception as exc:
        pw.stop()
        pytest.skip(f"no Chromium for Playwright: {exc}")
    yield chromium
    chromium.close()
    pw.stop()


@pytest.fixture
def tutor_log(caplog):
    """caplog for the app's ``tutor`` logger, whatever the test order.

    Importing tutor.main configures that logger with propagate=False (see
    tutor/logging_config.py), so caplog's root handler sees its records only
    in some orders. The app's configuration is applied here (it is
    idempotent) and the handler is attached to the logger itself, so each
    record is captured exactly once whatever ran before.
    """
    import logging

    from tutor.logging_config import configure_logging

    configure_logging()
    log = logging.getLogger("tutor")
    log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="tutor"):
            yield caplog
    finally:
        log.removeHandler(caplog.handler)
