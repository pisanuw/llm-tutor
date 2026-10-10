"""auth._origin host-allowlist (magic-link host-header-poisoning guard).

The origin returned here is baked into the emailed magic-link URL, which is a
bearer login credential. _origin must therefore only honour Host headers for
hosts we operate, falling back to the trusted BASE_URL for anything else — an
attacker who could anchor a victim's magic link on attacker.com would be able
to exfiltrate the token and take over the account.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import tutor.config as config
from tutor.routers.auth import _origin


def _req(host):
    """Minimal stand-in for a Request: _origin only reads request.headers."""
    headers = {} if host is None else {"host": host}
    return SimpleNamespace(headers=headers)


@pytest.fixture
def cfg_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
    monkeypatch.setenv("BASE_URL", "https://tutor.example.edu")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "my-tutor-a1b2.onrender.com")
    monkeypatch.setenv("ALLOWED_HOSTS", "extra.example.edu")
    config.get_config.cache_clear()
    yield
    config.get_config.cache_clear()


def test_allowed_base_host_is_sticky(cfg_env):
    assert _origin(_req("tutor.example.edu")) == "https://tutor.example.edu"


def test_render_host_auto_trusted(cfg_env):
    assert (
        _origin(_req("my-tutor-a1b2.onrender.com"))
        == "https://my-tutor-a1b2.onrender.com"
    )


def test_extra_allowed_host(cfg_env):
    assert _origin(_req("extra.example.edu")) == "https://extra.example.edu"


def test_spoofed_host_falls_back_to_base_url(cfg_env):
    # The poisoning attempt: an attacker-controlled Host must NOT leak into the
    # magic-link URL; we anchor on the trusted BASE_URL instead.
    assert _origin(_req("evil.com")) == "https://tutor.example.edu"


def test_spoofed_host_with_port_falls_back(cfg_env):
    assert _origin(_req("evil.com:1234")) == "https://tutor.example.edu"


def test_missing_host_falls_back_to_base_url(cfg_env):
    assert _origin(_req(None)) == "https://tutor.example.edu"


def test_localhost_allowed_and_http(cfg_env):
    assert _origin(_req("localhost:8000")) == "http://localhost:8000"
