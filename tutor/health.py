"""System health / diagnostics for the admin dashboard.

Answers the operational questions that come up when the live site "stops
responding": is the Anthropic key valid, is the (Supabase) database reachable,
is the knowledge base loaded, and which optional features are wired. Every
check is defensive — a single failing dependency must never 500 the page — and
no secret value is ever returned to the template (only presence + a short,
non-reversible prefix for the API key so operators can tell *which* key is live).

The Anthropic check makes one real 1-token call, so it costs a fraction of a
cent per dashboard load. It only runs when the Health tab is opened.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from tutor.config import get_config


class _HealthNS:
    """Lightweight namespace so the Jinja template can use dot access."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name: str) -> Any:
        # Only reached for a name __init__ did not set; declares the dynamic
        # attributes to the type checker.
        raise AttributeError(name)


def _ns(**kw) -> "_HealthNS":
    return _HealthNS(**kw)


def _mask_key(key: str) -> str:
    """A non-reversible fingerprint: prefix + length. Never the full secret."""
    if not key:
        return "(unset)"
    prefix = key[:14] if len(key) > 14 else key[:4]
    return f"{prefix}… (len {len(key)})"


def check_anthropic() -> "_HealthNS":
    """Live 1-token ping against the actor model. Distinguishes a bad/expired
    key (auth error) from a reachable-but-erroring API."""
    cfg = get_config()
    key = cfg.get("anthropic_api_key", "")
    model = cfg.get("claude_model", "")
    if not key:
        return _ns(ok=False, status="key-missing", detail="ANTHROPIC_API_KEY is not set",
                   key=_mask_key(key), model=model, latency_ms=None)

    try:
        from tutor.agent.clients import supervisor_client
    except ImportError:
        return _ns(ok=False, status="sdk-missing", detail="anthropic SDK not importable",
                   key=_mask_key(key), model=model, latency_ms=None)

    t0 = time.perf_counter()
    try:
        client = supervisor_client(api_key=key, timeout=15.0)
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        latency = round((time.perf_counter() - t0) * 1000)
        return _ns(ok=True, status="ok", detail="key valid, model reachable",
                   key=_mask_key(key), model=model, latency_ms=latency)
    except Exception as exc:  # noqa: BLE001 — surface any failure as a status row
        latency = round((time.perf_counter() - t0) * 1000)
        name = type(exc).__name__
        # AuthenticationError / PermissionDeniedError => the key itself is bad.
        auth = "authentication" in name.lower() or "permission" in name.lower()
        billing = "credit" in str(exc).lower() or "billing" in str(exc).lower()
        status = "key-invalid" if auth else ("billing" if billing else "error")
        return _ns(ok=False, status=status, detail=f"{name}: {str(exc)[:200]}",
                   key=_mask_key(key), model=model, latency_ms=latency)


def check_database(db: DBSession) -> "_HealthNS":
    """`SELECT 1` round-trip. The pool pings a connection before handing it
    out and reconnects a dropped one, so this is a true reachability test for
    the (Supabase) database. Reports the backend and a masked host so a
    paused/moved DB is obvious."""
    raw_url = os.getenv("DATABASE_URL", "").strip()
    if raw_url:
        parsed = urlparse(raw_url)
        backend = "postgresql (Supabase)" if "supabase" in (parsed.hostname or "") else "postgresql"
        host = parsed.hostname or "?"
        db_name = (parsed.path or "").lstrip("/") or "?"
        target = f"{host}/{db_name}"
    else:
        backend = "sqlite (local dev)"
        target = str(get_config().get("db_path", "?"))

    t0 = time.perf_counter()
    try:
        val = db.execute(text("SELECT 1")).scalar()
        latency = round((time.perf_counter() - t0) * 1000)
        ok = val == 1
        return _ns(ok=ok, status="ok" if ok else "unexpected",
                   detail="connected" if ok else f"SELECT 1 returned {val!r}",
                   backend=backend, target=target, latency_ms=latency)
    except Exception as exc:  # noqa: BLE001
        latency = round((time.perf_counter() - t0) * 1000)
        # The admin page goes on using this session (plan step 5.6).
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - the database is down; nothing to undo
            pass
        return _ns(ok=False, status="unreachable",
                   detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                   backend=backend, target=target, latency_ms=latency)


def check_chromadb() -> "_HealthNS":
    """Confirm the vector store loads and report per-collection doc counts. The
    agent's find_topic is useless if this is empty, so a 0-doc store is a
    silent-failure mode worth surfacing."""
    chroma_dir = get_config().get("chroma_dir")
    # The store file, not just the folder: opening a client on an empty
    # folder would create an empty store there (plan step 5.7, A18).
    if not chroma_dir or not os.path.isfile(os.path.join(str(chroma_dir), "chroma.sqlite3")):
        return _ns(ok=False, status="missing",
                   detail=f"no chroma store at {chroma_dir}",
                   collections=[], total=0)
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(chroma_dir))
        cols = client.list_collections()
        rows = []
        total = 0
        for c in cols:
            try:
                n = c.count()
            except Exception:  # noqa: BLE001
                n = -1
            total += max(n, 0)
            rows.append(_ns(name=c.name, count=n))
        rows.sort(key=lambda r: r.name)
        return _ns(ok=total > 0, status="ok" if total > 0 else "empty",
                   detail=f"{len(rows)} collections, {total} docs",
                   collections=rows, total=total)
    except Exception as exc:  # noqa: BLE001
        return _ns(ok=False, status="error",
                   detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                   collections=[], total=0)


def slides_status() -> str:
    """The retrieval store in one word, for /healthz (plan step 2.7, E5).

    "ok" when any knowledge-base collection (tutor/kb.py) has documents;
    otherwise "missing" (no store on disk), "empty" (no documents), or
    "error" (the store did not open). Never raises, and never creates a store
    where there is none.
    """
    chroma_dir = get_config().get("chroma_dir")
    if not chroma_dir or not os.path.isfile(os.path.join(str(chroma_dir), "chroma.sqlite3")):
        return "missing"
    try:
        import chromadb

        from tutor.kb import COLLECTIONS

        client = chromadb.PersistentClient(path=str(chroma_dir))
        for collection in client.list_collections():
            if collection.name in COLLECTIONS and collection.count() > 0:
                return "ok"
        return "empty"
    except Exception:  # noqa: BLE001
        return "error"


def check_features() -> list:
    """Presence-only view of optional integrations. Booleans, never values, so
    an operator can see at a glance what is wired without exposing secrets."""
    from tutor.agent import run_code

    cfg = get_config()
    google = bool(cfg.get("google_client_id") and cfg.get("google_client_secret"))
    eleven = bool(cfg.get("elevenlabs_api_key") and cfg.get("elevenlabs_voice_id"))
    return [
        _ns(name="Google OAuth login", on=google,
            detail="configured" if google else "magic-link only"),
        _ns(name="Resend email (magic links)", on=bool(cfg.get("resend_api_key")),
            detail="live send" if cfg.get("resend_api_key") else "dev mode (link printed)"),
        _ns(name="ElevenLabs TTS (🔊 button)", on=eleven,
            detail="enabled" if eleven else "disabled (env unset)"),
        _ns(name="Admin token", on=bool(cfg.get("admin_token")),
            detail="set" if cfg.get("admin_token") else "UNSET — token access disabled"),
        _ns(name="Code execution sandbox", on=bool(cfg.get("enable_code_exec")),
            detail=run_code.status_line()),
        _ns(name="Claude CLI mode (unsupervised)", on=bool(cfg.get("use_claude_cli")),
            detail="ON — dev only" if cfg.get("use_claude_cli") else "off (supervised pipeline)"),
    ]


def collect_diagnostics(db: DBSession) -> "_HealthNS":
    """Gather every health check into one object for the admin template."""
    cfg = get_config()
    anthropic_row = check_anthropic()
    database_row = check_database(db)
    chroma_row = check_chromadb()
    features = check_features()
    overall_ok = anthropic_row.ok and database_row.ok
    return _ns(
        anthropic=anthropic_row,
        database=database_row,
        chromadb=chroma_row,
        features=features,
        overall_ok=overall_ok,
        base_url=cfg.get("base_url", ""),
        actor_model=cfg.get("claude_model", ""),
    )
