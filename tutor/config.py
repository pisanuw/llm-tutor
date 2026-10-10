"""Central configuration for the tutor: reads from .env and project paths."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

from dotenv import load_dotenv
from tutor.paths import PERSONA_DIR, PROFILE_DIR

# Project root = parent of tutor/
ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def get_config() -> dict:
    use_cli = os.getenv("USE_CLAUDE_CLI", "false").lower() == "true"
    # CLI mode runs WITHOUT the supervisor pipeline (no classifier, detector,
    # judge, or gate). It is a local dev convenience only: refuse to combine
    # it with a production database so it can never serve real students.
    if use_cli and os.getenv("DATABASE_URL", "").strip():
        raise RuntimeError(
            "USE_CLAUDE_CLI=true is an unsupervised local dev mode and cannot "
            "run against a production DATABASE_URL. Unset one of them."
        )
    return {
        # Claude
        "use_claude_cli": use_cli,
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "") if use_cli else _require("ANTHROPIC_API_KEY"),
        "claude_model": os.getenv("CLAUDE_MODEL", _default_actor_model()),
        # Warm the actor's prompt cache when a student opens the chat
        # (agent/turn_stages/cache_warm.py); about $0.27 per warm-up, at most
        # one per 50 minutes. Off in the test suite.
        "cache_prewarm": os.getenv("CACHE_PREWARM", "true").lower() == "true",

        # Auth
        "secret_key": _require("SECRET_KEY"),
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "google_client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "resend_api_key": os.getenv("RESEND_API_KEY", ""),
        "from_email": os.getenv("FROM_EMAIL", "tutor@yourdomain.com"),
        "admin_token": _warn_if_absent("ADMIN_TOKEN"),
        # Emails granted admin access; always allowed to log in (bypass whitelist).
        "admin_emails": _email_set(os.getenv("ADMIN_EMAILS", "")),

        # Ops: DB self-heal + error alerting (tutor/selfheal.py, tutor/alerts.py).
        # The Supabase pair is the same values as the GitHub Actions secrets;
        # unset disables in-app restore (the scheduled db-keepalive workflow
        # still covers keep-alive). Alert recipients fall back to admin_emails.
        "supabase_access_token": os.getenv("SUPABASE_ACCESS_TOKEN", ""),
        "supabase_project_ref": os.getenv("SUPABASE_PROJECT_REF", ""),
        "alert_emails": _email_set(os.getenv("ALERT_EMAILS", "")),
        "alert_cooldown_minutes": int(os.getenv("ALERT_COOLDOWN_MINUTES", "60")),

        # Voice (ElevenLabs TTS) — optional; feature is disabled if either is unset
        "elevenlabs_api_key": os.getenv("ELEVENLABS_API_KEY", ""),
        "elevenlabs_voice_id": os.getenv("ELEVENLABS_VOICE_ID", ""),
        "tts_cache_dir": ROOT / os.getenv("TTS_CACHE_DIR", "tts-cache"),

        # Code execution sandbox (Session 19, handoff Task 6). OFF by default:
        # no untrusted student code runs unless an operator opts in. Backend is
        # a local subprocess sandbox now; "e2b" is reserved for before-pilot.
        "enable_code_exec": os.getenv("ENABLE_CODE_EXEC", "false").lower() == "true",
        "code_exec_backend": os.getenv("CODE_EXEC_BACKEND", "subprocess"),
        "code_exec_timeout_sec": int(os.getenv("CODE_EXEC_TIMEOUT_SEC", "5")),
        "code_exec_mem_mb": int(os.getenv("CODE_EXEC_MEM_MB", "256")),

        # Browser activity events:
        # track.js and deck-track.js post what a student does to /api/events.
        # ACTIVITY_EVENTS=off leaves the scripts out of every page; the
        # heartbeat is how often an open, visible page or deck reports in;
        # ACTIVITY_EVENTS_DAYS, when set, is how long the retention sweep
        # keeps raw rows (unset: they age out with the one-year anonymization).
        "activity_events": os.getenv("ACTIVITY_EVENTS", "on").strip().lower()
                           not in ("off", "false", "0", "no"),
        "activity_heartbeat_s": _int_env("ACTIVITY_HEARTBEAT_S", 60, floor=10),
        "activity_events_days": _int_env("ACTIVITY_EVENTS_DAYS", 0, floor=0) or None,

        # App
        "base_url": os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        # Hostnames login is allowed to anchor on (see auth._origin). Trusting
        # an arbitrary request Host turns the magic-link email URL into a
        # host-header-injection / token-exfiltration vector, so login is only
        # host-sticky for hosts we operate.
        "allowed_hosts": _allowed_hosts(),
        "jwt_expiry_days": int(os.getenv("JWT_EXPIRY_DAYS", "7")),
        "magic_link_expiry_minutes": int(os.getenv("MAGIC_LINK_EXPIRY_MINUTES", "30")),

        # Paths
        "db_path": ROOT / os.getenv("TUTOR_DB", "tutor.db"),
        # Generated artifacts. Overridable so the test suite never reads a
        # deck or a store you built (tests/conftest.py points both at empty
        # temp folders); a relative path is relative to the repository.
        "output_dir": ROOT / (os.getenv("TUTOR_OUTPUT_DIR") or "output"),
        # Per-slide narration clips: the persistent disk in production
        # (LECTURE_AUDIO_DIR), output/ locally, where render-audio writes them.
        # The /lecture-audio mount and the /lectures catalog both read this.
        "lecture_audio_dir": ROOT / (os.getenv("LECTURE_AUDIO_DIR") or os.getenv("TUTOR_OUTPUT_DIR") or "output"),
        "chroma_dir": ROOT / (os.getenv("TUTOR_CHROMA_DIR") or "knowledge-base/chromadb"),
        "instructor_profile_dir": PROFILE_DIR,
        "persona_dir": PERSONA_DIR,
    }


def _default_actor_model() -> str:
    """Default actor model id from the registry (S21). Lazy import so the
    config module stays importable before the agent package."""
    from tutor.agent.models import ACTOR
    return ACTOR.id


def _allowed_hosts() -> frozenset[str]:
    """Hostnames (lowercased, no port) that login (auth._origin) may anchor on.

    Always includes the configured BASE_URL host plus localhost. Render injects
    RENDER_EXTERNAL_HOSTNAME for every service, so the *.onrender.com host is
    auto-trusted without manual config; ALLOWED_HOSTS (comma-separated) adds any
    others. A request whose Host is not in this set falls back to BASE_URL.
    """
    hosts = {"localhost", "127.0.0.1"}
    base = urlparse(os.getenv("BASE_URL", "http://localhost:8000")).hostname
    if base:
        hosts.add(base.lower())
    render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
    if render_host:
        hosts.add(render_host)
    for h in os.getenv("ALLOWED_HOSTS", "").split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return frozenset(hosts)


def _int_env(key: str, default: int, floor: int = 0) -> int:
    """An integer setting, ``default`` when unset or unreadable, never
    under ``floor`` (a heartbeat of 0 would flood the database)."""
    try:
        value = int(os.getenv(key, "").strip() or default)
    except ValueError:
        value = default
    return max(floor, value)


def _warn_if_absent(key: str) -> str:
    val = os.getenv(key, "")
    if not val:
        _log.warning("ADMIN_TOKEN is not set; token-based admin access is disabled.")
    return val


def _require(key: str) -> str:
    val = os.getenv(key, "")
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _email_set(raw: str) -> frozenset[str]:
    """Parse a comma-separated email list into a lowercased frozenset."""
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
