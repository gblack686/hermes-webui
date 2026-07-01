"""Coverage for the tenant-scoped agent-profile catalog backend (plan B12).

Ported from 9119 into webui as the stdlib module ``api/gbauto_agent_profiles.py``.
These tests are hermetic: they never touch the network, real Supabase, or the
``gbauto-supabase`` CLI. They exercise the degrade paths (CLI absent -> the panel
gets a well-formed empty payload with ``available:false``), the ALLOWED_TENANTS
gate, the SQL-literal escaping, the CLI JSON parsing/error mapping, the cache, and
the catalog shape given a monkeypatched CLI response.
"""

from __future__ import annotations

import types

import pytest

from api import gbauto_agent_profiles as ap


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Override conftest's autouse server fixture.

    These are pure, hermetic unit tests: they never hit the network, real
    Supabase, or the ``gbauto-supabase`` CLI, so they must NOT boot the shared
    test server (which also tries to symlink real skills -- impossible on a
    Windows box without the symlink privilege). Shadowing the fixture by name
    keeps this file unit-only and green on any machine.
    """
    yield None


@pytest.fixture(autouse=True)
def _clear_cache():
    """Keep every test independent of the module-local poll cache."""
    ap._CACHE.clear()
    yield
    ap._CACHE.clear()


def _completed(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ── cli_available ─────────────────────────────────────────────────────────────

def test_cli_available_reflects_path(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    assert ap.cli_available() is False
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    assert ap.cli_available() is True


# ── get_profile_catalog degrade: CLI absent (the PC case) ─────────────────────

def test_get_catalog_degrades_when_cli_absent(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)

    # If the CLI were shelled out to, this would blow up -- proving we degrade
    # before touching subprocess.
    def _boom(*a, **k):
        raise AssertionError("subprocess must not run when CLI is absent")

    monkeypatch.setattr(ap.subprocess, "run", _boom)

    payload = ap.get_profile_catalog("gbautomation")
    assert payload["ok"] is False
    assert payload["available"] is False
    assert payload["tenant"] == "gbautomation"
    assert payload["source"] == "supabase:agent_profile_catalog"
    assert payload["teams"] == []
    assert payload["profiles"] == []
    assert payload["routes"] == []
    # No error key on the plain mini_pending / CLI-absent path.
    assert "error" not in payload


def test_get_catalog_defaults_tenant_to_gbautomation(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    payload = ap.get_profile_catalog(None)
    assert payload["tenant"] == "gbautomation"
    assert payload["available"] is False


# ── ALLOWED_TENANTS gate ──────────────────────────────────────────────────────

def test_get_catalog_rejects_disallowed_tenant(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    payload = ap.get_profile_catalog("evil-corp")
    assert payload["ok"] is False
    assert payload["error"] == "tenant not allowed"
    assert payload["tenant"] == "evil-corp"
    assert payload["teams"] == [] and payload["profiles"] == [] and payload["routes"] == []


def test_disallowed_tenant_available_tracks_cli_presence(monkeypatch):
    # available mirrors cli_available() even on the rejection path.
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    payload = ap.get_profile_catalog("nope")
    assert payload["error"] == "tenant not allowed"
    assert payload["available"] is True


def test_load_catalog_raises_on_disallowed_tenant():
    with pytest.raises(ValueError) as exc:
        ap.load_profile_catalog("not-a-tenant")
    assert "tenant not allowed" in str(exc.value)


def test_tenant_is_normalized_before_gate(monkeypatch):
    # Upper-case / whitespace tenant is allowed once normalized.
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    payload = ap.get_profile_catalog("  GBAutomation  ")
    assert payload["tenant"] == "gbautomation"
    assert "error" not in payload
    assert payload["available"] is False


def test_allowed_tenants_membership():
    assert {"smoke-client", "gbautomation", "jid5274", "ecom"} == ap.ALLOWED_TENANTS


# ── SQL-literal escaping ──────────────────────────────────────────────────────

def test_sql_literal_escapes_quotes_and_percent():
    assert ap._sql_literal("smoke-client") == "'smoke-client'"
    assert ap._sql_literal("o'brien") == "'o''brien'"
    assert ap._sql_literal("50%") == "'50%%'"
    assert ap._sql_literal("a'b%c") == "'a''b%%c'"


# ── _run_cli parsing / error mapping ──────────────────────────────────────────

def test_run_cli_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc:
        ap._run_cli("select 1")
    assert "not on PATH" in str(exc.value)


def test_run_cli_parses_plain_list(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    monkeypatch.setattr(
        ap.subprocess, "run",
        lambda *a, **k: _completed(stdout='[{"a": 1}, {"a": 2}, "skip-me"]'),
    )
    rows = ap._run_cli("select a")
    # Non-dict rows filtered out.
    assert rows == [{"a": 1}, {"a": 2}]


def test_run_cli_unwraps_value_envelope(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    monkeypatch.setattr(
        ap.subprocess, "run",
        lambda *a, **k: _completed(stdout='{"ok": true, "value": [{"x": 9}]}'),
    )
    assert ap._run_cli("select x") == [{"x": 9}]


def test_run_cli_maps_nonzero_exit_to_runtime_error(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    monkeypatch.setattr(
        ap.subprocess, "run",
        lambda *a, **k: _completed(stderr="boom on the mini", returncode=1),
    )
    with pytest.raises(RuntimeError) as exc:
        ap._run_cli("select 1")
    assert "boom on the mini" in str(exc.value)


def test_run_cli_maps_ok_false_envelope_to_error(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    monkeypatch.setattr(
        ap.subprocess, "run",
        lambda *a, **k: _completed(stdout='{"ok": false, "error": "relation missing"}'),
    )
    with pytest.raises(RuntimeError) as exc:
        ap._run_cli("select 1")
    assert "relation missing" in str(exc.value)


def test_run_cli_rejects_unexpected_shape(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    monkeypatch.setattr(
        ap.subprocess, "run",
        lambda *a, **k: _completed(stdout='"just a string"'),
    )
    with pytest.raises(RuntimeError) as exc:
        ap._run_cli("select 1")
    assert "unexpected response shape" in str(exc.value)


# ── Catalog shape given a monkeypatched CLI ───────────────────────────────────

def test_get_catalog_shape_from_monkeypatched_cli(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")

    def fake_run_cli(sql, *, timeout=90):
        if "agent_profile_teams" in sql:
            return [{"team_id": "ecom-team", "tenant": "ecom", "display_name": "Ecom"}]
        if "agent_profile_catalog" in sql:
            return [{"profile_id": "lead", "tenant": "ecom", "role": "orchestrator"}]
        if "agent_profile_routes" in sql:
            return [{"route_name": "escalate", "tenant": "ecom"}]
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)

    payload = ap.get_profile_catalog("ecom")
    assert payload["ok"] is True
    assert payload["available"] is True
    assert payload["tenant"] == "ecom"
    assert payload["source"] == "supabase:agent_profile_catalog"
    assert payload["teams"] == [{"team_id": "ecom-team", "tenant": "ecom", "display_name": "Ecom"}]
    assert payload["profiles"] == [{"profile_id": "lead", "tenant": "ecom", "role": "orchestrator"}]
    assert payload["routes"] == [{"route_name": "escalate", "tenant": "ecom"}]


def test_load_catalog_filters_each_relation_to_tenant(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    seen = []

    def fake_run_cli(sql, *, timeout=90):
        seen.append(sql)
        return []

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)
    ap.load_profile_catalog("smoke-client")
    assert len(seen) == 3
    # Every relation query is tenant-scoped with the escaped literal.
    assert all("where tenant = 'smoke-client'" in sql for sql in seen)


# ── Query failure degrades (CLI present but query throws) ─────────────────────

def test_get_catalog_degrades_on_query_failure(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")

    def fake_run_cli(sql, *, timeout=90):
        raise RuntimeError("supabase DNS blocked from PC")

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)

    payload = ap.get_profile_catalog("gbautomation")
    assert payload["ok"] is False
    assert payload["available"] is True  # CLI present, query failed.
    assert "supabase DNS blocked from PC" in payload["error"]
    assert payload["teams"] == [] and payload["profiles"] == [] and payload["routes"] == []


def test_get_catalog_error_message_is_truncated(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    long = "x" * 5000

    def fake_run_cli(sql, *, timeout=90):
        raise RuntimeError(long)

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)
    payload = ap.get_profile_catalog("gbautomation")
    assert len(payload["error"]) <= 500


# ── Poll cache ────────────────────────────────────────────────────────────────

def test_get_catalog_caches_successful_payload(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    calls = {"n": 0}

    def fake_run_cli(sql, *, timeout=90):
        calls["n"] += 1
        return []

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)

    first = ap.get_profile_catalog("ecom")
    n_after_first = calls["n"]
    second = ap.get_profile_catalog("ecom")
    # Second call served from cache -> no additional CLI shells.
    assert calls["n"] == n_after_first
    assert first is second


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/opt/bin/gbauto-supabase")
    calls = {"n": 0}

    def fake_run_cli(sql, *, timeout=90):
        calls["n"] += 1
        return []

    monkeypatch.setattr(ap, "_run_cli", fake_run_cli)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ap.time, "time", lambda: clock["t"])

    ap.get_profile_catalog("ecom")
    n1 = calls["n"]
    # Advance beyond the TTL window -> cache miss, re-shell.
    clock["t"] += ap._CACHE_TTL_S + 1
    ap.get_profile_catalog("ecom")
    assert calls["n"] > n1
