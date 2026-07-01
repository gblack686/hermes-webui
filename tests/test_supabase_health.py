"""Hermetic coverage for the GBAutomation Supabase-health backend (plan B9a).

``api/supabase_health.py`` was ported from 9119's
``hermes_cli.gbauto_supabase_health``. All DB access is via the ``gbauto-supabase``
CLI, which is Mini-only and DNS-blocked from the PC, so the module is
degrade-safe: :func:`get_supabase_health` must always return a well-formed,
aggregate-only payload even when the CLI is absent or a query fails.

These tests exercise the DEGRADE path (CLI absent -> ``available:false`` with
empty relations), the tenant allow-list, the ``RELATION_SPECS`` shape, the SQL
builders, and the summary aggregation given canned CLI JSON rows -- all without
the session ``test_server`` fixture and without touching the network / Supabase.
"""

from __future__ import annotations

import pytest

from api import supabase_health as sh


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Override conftest's server-spawning fixture.

    These are hermetic unit tests: they exercise ``api.supabase_health``
    in-process and never touch the HTTP server, the network, or Supabase. The
    conftest ``test_server`` fixture spawns a subprocess and symlinks the real
    skills dir into a temp home -- the latter needs Windows symlink privilege and
    is entirely unnecessary here. Overriding it by name keeps this file unit-only.
    """
    yield None


@pytest.fixture(autouse=True)
def _clear_cache():
    """Keep the module-local poll cache from bleeding across tests."""
    sh._CACHE.clear()
    yield
    sh._CACHE.clear()


# ── cli_available() reflects PATH ─────────────────────────────────────────────

def test_cli_available_reflects_path(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: None)
    assert sh.cli_available() is False

    monkeypatch.setattr(sh.shutil, "which", lambda name: "/usr/local/bin/gbauto-supabase")
    assert sh.cli_available() is True


# ── DEGRADE: CLI absent -> available:false, empty relations ───────────────────

def test_get_health_degrades_when_cli_absent(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: False)

    def _boom(*a, **k):  # the query path must never be reached when CLI is absent
        raise AssertionError("CLI should not be invoked when unavailable")

    monkeypatch.setattr(sh, "_run_cli", _boom)

    payload = sh.get_supabase_health("gbautomation")

    assert payload["ok"] is False
    assert payload["available"] is False
    assert payload["tenant"] == "gbautomation"
    assert payload["relations"] == []
    assert "error" not in payload  # plain unavailability is not an error
    summary = payload["summary"]
    assert summary["relations"] == 0
    assert summary["total_rows"] == 0
    assert summary["bad_rows"] == 0
    assert summary["by_kind"] == {}
    assert summary["by_family"] == {}
    # Terminology is always present so the panel can render the legend offline.
    assert payload["terminology"] == sh.TERMINOLOGY


def test_get_health_payload_is_well_formed_keys(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: False)
    payload = sh.get_supabase_health(None)
    assert set(payload) >= {"ok", "available", "tenant", "terminology", "summary", "relations"}
    assert set(payload["summary"]) == {
        "relations", "empty_relations", "attention_relations",
        "total_rows", "bad_rows", "by_kind", "by_family",
    }


# ── Tenant allow-list + normalization ─────────────────────────────────────────

def test_disallowed_tenant_is_rejected_with_error(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: False)
    payload = sh.get_supabase_health("evil-tenant")
    assert payload["ok"] is False
    assert payload["available"] is False
    assert payload["error"] == "tenant not allowed"
    assert payload["relations"] == []
    assert payload["tenant"] == "evil-tenant"


def test_disallowed_tenant_reports_cli_availability(monkeypatch):
    # available mirrors cli_available() even on the rejection path.
    monkeypatch.setattr(sh, "cli_available", lambda: True)
    payload = sh.get_supabase_health("nope")
    assert payload["available"] is True
    assert payload["error"] == "tenant not allowed"


def test_tenant_defaults_to_gbautomation(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: False)
    assert sh.get_supabase_health(None)["tenant"] == "gbautomation"
    assert sh.get_supabase_health("")["tenant"] == "gbautomation"


def test_tenant_is_normalized_case_and_whitespace(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: False)
    payload = sh.get_supabase_health("  ECOM  ")
    assert payload["tenant"] == "ecom"
    assert payload["available"] is False  # normalized tenant is allowed, CLI is not


def test_allowed_tenants_membership():
    assert sh.ALLOWED_TENANTS == {"smoke-client", "gbautomation", "jid5274", "ecom"}


# ── RELATION_SPECS shape ──────────────────────────────────────────────────────

def test_relation_specs_shape():
    assert sh.RELATION_SPECS, "specs must be non-empty"
    seen = set()
    allowed_kinds = {"base_table", "read_view", "smoke_view", "compat_view"}
    for spec in sh.RELATION_SPECS:
        assert set(spec) == {"name", "kind", "family", "filter", "latest", "bad"}
        assert spec["name"] not in seen, f"duplicate relation {spec['name']}"
        seen.add(spec["name"])
        assert spec["kind"] in allowed_kinds
        # Every filter must be tenant-parameterized (or fixed to a profile list).
        assert "{tenant}" in spec["filter"] or "profile" in spec["filter"]
        assert spec["family"]
        assert spec["latest"]
        assert spec["bad"]


def test_terminology_terms_cover_spec_kinds():
    terms = {t["term"] for t in sh.TERMINOLOGY}
    spec_kinds = {s["kind"] for s in sh.RELATION_SPECS}
    assert spec_kinds <= terms


# ── SQL builders (pure logic) ─────────────────────────────────────────────────

def test_sql_literal_escapes_quotes_and_percent():
    assert sh._sql_literal("ecom") == "'ecom'"
    # single quotes are doubled
    assert sh._sql_literal("o'brien") == "'o''brien'"
    # percent is doubled for the CLI's format layer
    assert sh._sql_literal("50%") == "'50%%'"


def test_relation_sql_wraps_latest_and_injects_tenant():
    tenant_sql = sh._sql_literal("ecom")
    # A spec whose latest is a real column -> wrapped in max(...)
    col_spec = next(s for s in sh.RELATION_SPECS if not s["latest"].startswith("null::"))
    sql = sh._relation_sql(col_spec, tenant_sql)
    assert sql.startswith("select ")
    assert f"from public.{col_spec['name']}" in sql
    assert f"max({col_spec['latest']}) as latest" in sql
    assert "'ecom'" in sql
    assert "count(*)::int as rows" in sql

    # A spec whose latest is a null literal -> passed through, NOT wrapped.
    null_spec = next(s for s in sh.RELATION_SPECS if s["latest"].startswith("null::"))
    null_sql = sh._relation_sql(null_spec, tenant_sql)
    assert f"{null_spec['latest']} as latest" in null_sql
    assert f"max({null_spec['latest']})" not in null_sql


# ── Aggregation over canned CLI rows ──────────────────────────────────────────

CANNED_ROWS = [
    {"relation": "agent_sessions", "kind": "base_table", "family": "sessions",
     "rows": 5, "bad_rows": 0, "latest": "2026-01-01T00:00:00Z"},
    {"relation": "chat_messages", "kind": "base_table", "family": "sessions",
     "rows": 3, "bad_rows": 2, "latest": "2026-01-02T00:00:00Z"},
    {"relation": "v_agent_session_history", "kind": "read_view", "family": "sessions",
     "rows": 0, "bad_rows": 0, "latest": None},
    {"relation": "agent_runs", "kind": "base_table", "family": "runs",
     "rows": 4, "bad_rows": 1, "latest": "2026-01-03T00:00:00Z"},
]


def test_load_health_aggregates_canned_rows(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/usr/local/bin/gbauto-supabase")
    monkeypatch.setattr(sh, "_run_cli", lambda sql, timeout=90: [dict(r) for r in CANNED_ROWS])

    result = sh.load_supabase_health("gbautomation")

    assert result["ok"] is True
    assert result["available"] is True
    assert result["tenant"] == "gbautomation"

    summary = result["summary"]
    assert summary["relations"] == 4
    assert summary["empty_relations"] == 1               # the 0-row view
    assert summary["attention_relations"] == 2           # chat_messages + agent_runs
    assert summary["total_rows"] == 12                   # 5 + 3 + 0 + 4
    assert summary["bad_rows"] == 3                       # 0 + 2 + 0 + 1
    assert summary["by_kind"] == {"base_table": 12, "read_view": 0}
    assert summary["by_family"] == {"sessions": 8, "runs": 4}

    by_name = {r["relation"]: r for r in result["relations"]}
    assert by_name["agent_sessions"]["status"] == "ok"
    assert by_name["agent_sessions"]["empty"] is False
    assert by_name["chat_messages"]["status"] == "attention"
    assert by_name["v_agent_session_history"]["status"] == "empty"
    assert by_name["v_agent_session_history"]["empty"] is True
    assert by_name["agent_runs"]["status"] == "attention"


def test_load_health_rejects_disallowed_tenant():
    with pytest.raises(ValueError):
        sh.load_supabase_health("not-a-tenant")


def test_get_health_returns_full_payload_when_cli_present(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: True)
    monkeypatch.setattr(sh, "_run_cli", lambda sql, timeout=90: [dict(r) for r in CANNED_ROWS])

    payload = sh.get_supabase_health("ecom")
    assert payload["ok"] is True
    assert payload["available"] is True
    assert payload["tenant"] == "ecom"
    assert payload["summary"]["total_rows"] == 12


# ── DEGRADE: CLI present but query fails -> available:true + error ─────────────

def test_get_health_degrades_on_query_failure(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: True)

    def _fail(sql, timeout=90):
        raise RuntimeError("gbauto-supabase exploded: relation missing")

    monkeypatch.setattr(sh, "_run_cli", _fail)

    payload = sh.get_supabase_health("gbautomation")
    assert payload["ok"] is False
    assert payload["available"] is True          # CLI is present, just the query failed
    assert payload["relations"] == []
    assert "gbauto-supabase exploded" in payload["error"]
    assert payload["summary"]["relations"] == 0


def test_get_health_error_is_truncated(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: True)
    long_msg = "x" * 5000

    def _fail(sql, timeout=90):
        raise RuntimeError(long_msg)

    monkeypatch.setattr(sh, "_run_cli", _fail)
    payload = sh.get_supabase_health("gbautomation")
    assert len(payload["error"]) <= 500


# ── Caching: a successful result is served from the process-local cache ────────

def test_get_health_serves_from_cache(monkeypatch):
    monkeypatch.setattr(sh, "cli_available", lambda: True)
    calls = {"n": 0}

    def _once(sql, timeout=90):
        calls["n"] += 1
        return [dict(r) for r in CANNED_ROWS]

    monkeypatch.setattr(sh, "_run_cli", _once)

    first = sh.get_supabase_health("gbautomation")
    assert first["ok"] is True
    assert calls["n"] == 1

    # Second call within TTL must NOT re-shell the CLI, even if it would now fail.
    def _boom(sql, timeout=90):
        raise AssertionError("cached result should have been reused")

    monkeypatch.setattr(sh, "_run_cli", _boom)
    second = sh.get_supabase_health("gbautomation")
    assert second == first
    assert calls["n"] == 1


# ── _run_cli error mapping (mock subprocess; module shells out) ────────────────

def _fake_completed(returncode=0, stdout="", stderr=""):
    class _CP:
        pass
    cp = _CP()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_run_cli_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        sh._run_cli("select 1")


def test_run_cli_maps_nonzero_exit_to_runtime_error(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/bin/gbauto-supabase")
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: _fake_completed(returncode=1, stderr="boom on the mini"),
    )
    with pytest.raises(RuntimeError, match="boom on the mini"):
        sh._run_cli("select 1")


def test_run_cli_maps_ok_false_envelope_to_error(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/bin/gbauto-supabase")
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: _fake_completed(stdout='{"ok": false, "error": "denied"}'),
    )
    with pytest.raises(RuntimeError, match="denied"):
        sh._run_cli("select 1")


def test_run_cli_unwraps_value_envelope_and_filters_non_dict_rows(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/bin/gbauto-supabase")
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: _fake_completed(
            stdout='{"value": [{"relation": "agent_sessions", "rows": 2}, 7, "junk"]}'
        ),
    )
    rows = sh._run_cli("select 1")
    assert rows == [{"relation": "agent_sessions", "rows": 2}]


def test_run_cli_rejects_unexpected_shape(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/bin/gbauto-supabase")
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: _fake_completed(stdout='{"unexpected": true}'),
    )
    with pytest.raises(RuntimeError, match="unexpected response shape"):
        sh._run_cli("select 1")


def test_run_cli_returns_bare_list(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: "/bin/gbauto-supabase")
    monkeypatch.setattr(
        sh.subprocess, "run",
        lambda *a, **k: _fake_completed(stdout='[{"relation": "kanban_tasks", "rows": 9}]'),
    )
    assert sh._run_cli("select 1") == [{"relation": "kanban_tasks", "rows": 9}]
