"""The suite-wide isolation in tests/conftest.py holds (plan step 0.10).

These guard the property that matters: no test can open the production
database or spend on a paid API, whatever the developer's .env or shell says.
"""

from __future__ import annotations

import os
from pathlib import Path

from tutor.config import ROOT, get_config
from tutor.db.models import get_engine


def test_dotenv_loading_is_disabled_for_the_suite():
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"


def test_database_is_local_sqlite_outside_the_repo():
    assert not os.environ.get("DATABASE_URL")
    assert get_engine().url.get_backend_name() == "sqlite"
    db_path = Path(get_config()["db_path"]).resolve()
    assert ROOT.resolve() not in db_path.parents


def test_paid_service_credentials_are_dummies_or_absent():
    assert os.environ["ANTHROPIC_API_KEY"] == "test-dummy-key"
    for name in ("RESEND_API_KEY", "ELEVENLABS_API_KEY", "SUPABASE_ACCESS_TOKEN",
                 "SENTRY_DSN", "GOOGLE_CLIENT_SECRET", "GITHUB_TOKEN"):
        assert not os.environ.get(name), name


def test_tts_cache_is_outside_the_repo():
    cache = Path(get_config()["tts_cache_dir"]).resolve()
    assert ROOT.resolve() not in cache.parents
