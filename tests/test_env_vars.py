"""Hermetic unit coverage for the Keys/Env backend (``api/env_vars.py``).

This module is the ported ``/api/env`` family (9119 -> WebUI, plan B3). It
lists the ``OPTIONAL_ENV_VARS`` catalog with *masked* previews, and offers
set / remove / reveal restricted to catalog keys (PII-safety / anti-injection
allowlist). All disk / os.environ mutations route through the active profile's
dotenv via lazily-imported helpers.

These tests are hermetic: they do NOT use the ``test_server`` fixture and do
NOT hit the network, real Supabase, or a real profile. Writes are redirected
to a ``tmp_path`` dotenv, and the best-effort dotenv reload is stubbed. They
cover: value masking (never leaking a full secret in list output), the
allowlist / degrade paths for set/remove/reveal, error mapping, the reveal
rate limiter, and reload swallowing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from api import env_vars as ev


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def test_server():
    """No-op override of the conftest session server fixture.

    These are pure, hermetic unit tests: they never touch the HTTP server, so
    booting it (and symlinking real skills, which needs elevation on Windows)
    is unnecessary. Overriding the fixture by name for this module keeps the
    file runnable standalone without the network/server dependency.
    """
    yield None

@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Redirect env_vars at an isolated tmp dotenv and restore os.environ.

    ``api.providers._write_env_file`` mutates ``os.environ`` directly (not via
    monkeypatch), so we snapshot/restore manually. The best-effort dotenv
    reload is stubbed so no real profile is ever touched.
    """
    env_path = tmp_path / ".env"
    monkeypatch.setattr(ev, "_active_env_path", lambda: env_path)
    monkeypatch.setattr(ev, "_reload_dotenv_best_effort", lambda: None)
    before = dict(os.environ)
    try:
        yield env_path
    finally:
        for k in list(os.environ):
            if k not in before:
                del os.environ[k]
        for k, v in before.items():
            os.environ[k] = v


# ── redact_key: masking never leaks the full secret ───────────────────────────

def test_redact_key_none_and_empty_are_none():
    assert ev.redact_key(None) is None
    assert ev.redact_key("") is None


def test_redact_key_short_values_fully_masked():
    # <= 8 chars collapses to a constant mask (no first4/last4 which would
    # leak the whole thing for short secrets).
    assert ev.redact_key("short") == "***"
    assert ev.redact_key("12345678") == "***"


def test_redact_key_long_value_shows_only_edges():
    assert ev.redact_key("123456789") == "1234...6789"
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    preview = ev.redact_key(secret)
    assert preview == "sk-p...6789"
    # The full secret must never be recoverable from its preview.
    assert secret not in preview
    assert len(preview) < len(secret)


# ── _known_keys allowlist ─────────────────────────────────────────────────────

def test_known_keys_is_exactly_the_catalog():
    known = ev._known_keys()
    assert set(ev.OPTIONAL_ENV_VARS).issubset(known)
    # A representative catalog key is present; an arbitrary key is not.
    assert "OPENROUTER_API_KEY" in known
    assert "TOTALLY_FAKE_KEY" not in known


# ── get_env_vars: full catalog, masked, never leaks raw secret ────────────────

def test_get_env_vars_masks_set_values_and_never_leaks(monkeypatch):
    secret = "sk-secret-abcdefghXYZ"
    monkeypatch.setattr(
        ev, "_load_env",
        lambda: {"OPENROUTER_API_KEY": secret, "AWS_REGION": "us-east-1"},
    )
    res = ev.get_env_vars()

    # Every catalog key is represented (list shape mirrors 9119).
    assert set(res) == set(ev.OPTIONAL_ENV_VARS)

    entry = res["OPENROUTER_API_KEY"]
    assert entry["is_set"] is True
    assert entry["redacted_value"] == "sk-s...hXYZ"
    assert entry["is_password"] is True
    assert entry["tools"] == ["vision_analyze", "mixture_of_agents"]

    # The raw secret must not appear anywhere in the serialized list output.
    assert secret not in json.dumps(res)


def test_get_env_vars_entry_shape_is_stable(monkeypatch):
    monkeypatch.setattr(ev, "_load_env", lambda: {"AWS_REGION": "us-east-1"})
    res = ev.get_env_vars()
    entry = res["AWS_REGION"]
    assert set(entry) == {
        "is_set", "redacted_value", "description", "url",
        "category", "is_password", "tools", "advanced",
    }
    assert entry["is_set"] is True
    # AWS_REGION is not a password field; still no raw echo beyond preview.
    assert entry["is_password"] is False


def test_get_env_vars_unset_keys_are_null(monkeypatch):
    monkeypatch.setattr(ev, "_load_env", lambda: {})
    res = ev.get_env_vars()
    unset = res["FAL_KEY"]
    assert unset["is_set"] is False
    assert unset["redacted_value"] is None


# ── set_env_var: validation + allowlist ───────────────────────────────────────

def test_set_env_var_rejects_empty_key():
    out = ev.set_env_var("", "value")
    assert out["ok"] is False
    assert out["error"] == "key is required"


def test_set_env_var_rejects_unknown_key():
    out = ev.set_env_var("TOTALLY_FAKE_KEY", "value")
    assert out["ok"] is False
    assert "unknown env var" in out["error"]


def test_set_env_var_rejects_empty_value():
    out = ev.set_env_var("OPENROUTER_API_KEY", "   ")
    assert out["ok"] is False
    assert out["error"] == "value is required"


def test_set_env_var_round_trip_writes_and_masks(tmp_env):
    out = ev.set_env_var("OPENROUTER_API_KEY", "sk-roundtrip-123456789")
    assert out == {"ok": True, "key": "OPENROUTER_API_KEY"}

    # Persisted to the tmp dotenv.
    contents = tmp_env.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-roundtrip-123456789" in contents

    # And the listing reflects it as set-but-masked (no raw leak).
    listed = ev.get_env_vars()["OPENROUTER_API_KEY"]
    assert listed["is_set"] is True
    assert "sk-roundtrip-123456789" not in json.dumps(ev.get_env_vars())


def test_set_env_var_maps_valueerror_from_writer(tmp_env):
    # A newline in the value triggers the real writer's ValueError guard
    # (.env injection protection) -> mapped to {ok:False, error}.
    out = ev.set_env_var("OPENROUTER_API_KEY", "line1\nline2")
    assert out["ok"] is False
    assert "newline" in out["error"].lower()


def test_set_env_var_maps_generic_write_failure(tmp_env, monkeypatch):
    import api.providers as providers

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(providers, "_write_env_file", boom)
    out = ev.set_env_var("OPENROUTER_API_KEY", "sk-value-123456789")
    assert out["ok"] is False
    assert out["error"].startswith("failed to write:")


# ── remove_env_var ────────────────────────────────────────────────────────────

def test_remove_env_var_rejects_empty_and_unknown():
    assert ev.remove_env_var("")["error"] == "key is required"
    unknown = ev.remove_env_var("TOTALLY_FAKE_KEY")
    assert unknown["ok"] is False
    assert "unknown env var" in unknown["error"]


def test_remove_env_var_missing_is_404(tmp_env):
    out = ev.remove_env_var("OPENROUTER_API_KEY")
    assert out["ok"] is False
    assert out["status"] == 404
    assert "not found" in out["error"]


def test_remove_env_var_round_trip(tmp_env):
    ev.set_env_var("EXA_API_KEY", "exa-secret-123456789")
    assert "EXA_API_KEY" in ev._load_env()
    out = ev.remove_env_var("EXA_API_KEY")
    assert out == {"ok": True, "key": "EXA_API_KEY"}
    assert "EXA_API_KEY" not in ev._load_env()


# ── reveal_env_var ────────────────────────────────────────────────────────────

def test_reveal_env_var_rejects_empty_and_unknown():
    assert ev.reveal_env_var("")["error"] == "key is required"
    unknown = ev.reveal_env_var("TOTALLY_FAKE_KEY")
    assert unknown["ok"] is False
    assert "unknown env var" in unknown["error"]


def test_reveal_env_var_missing_is_404(tmp_env):
    out = ev.reveal_env_var("OPENROUTER_API_KEY")
    assert out["ok"] is False
    assert out["status"] == 404


def test_reveal_env_var_returns_full_value(tmp_env):
    ev.set_env_var("OPENROUTER_API_KEY", "sk-reveal-123456789")
    out = ev.reveal_env_var("OPENROUTER_API_KEY")
    # reveal is the one path that intentionally returns the unmasked value
    # (gated by auth + rate-limit in api/routes.py).
    assert out == {"ok": True, "key": "OPENROUTER_API_KEY", "value": "sk-reveal-123456789"}


# ── reveal rate limiter ───────────────────────────────────────────────────────

def test_reveal_allowed_caps_at_max_per_window(monkeypatch):
    monkeypatch.setattr(ev, "_reveal_timestamps", [])
    allowed = [ev._reveal_allowed() for _ in range(ev._REVEAL_MAX_PER_WINDOW)]
    assert all(allowed)
    # The next call within the window is denied.
    assert ev._reveal_allowed() is False


def test_reveal_allowed_prunes_expired_window(monkeypatch):
    import time as _time

    now = _time.time()
    stale = now - ev._REVEAL_WINDOW_SECONDS - 5
    # Fill the window entirely with stale timestamps.
    monkeypatch.setattr(
        ev, "_reveal_timestamps",
        [stale] * ev._REVEAL_MAX_PER_WINDOW,
    )
    # Stale entries are pruned, so the call is allowed again.
    assert ev._reveal_allowed() is True
    # And only the fresh timestamp remains.
    assert len(ev._reveal_timestamps) == 1
    assert ev._reveal_timestamps[0] > stale


# ── _reload_dotenv_best_effort swallows errors ────────────────────────────────

def test_reload_dotenv_best_effort_swallows_exceptions(monkeypatch):
    import api.profiles as profiles

    def boom(_home):
        raise RuntimeError("reload failed")

    monkeypatch.setattr(profiles, "_reload_dotenv", boom)
    # Must not raise despite the underlying reload failing.
    assert ev._reload_dotenv_best_effort() is None
