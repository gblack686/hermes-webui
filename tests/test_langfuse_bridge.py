"""Hermetic coverage for the Langfuse trace bridge (plan B9b).

``api/langfuse_bridge.get_langfuse`` is a thin, degrade-safe wrapper over the
Mini-only ``hermes_cli.gbauto_supabase_logs.get_traces``. On the PC that import
fails, so these tests exercise:

* the DEGRADE path (import failure -> ``available=False``),
* param clamping (days/limit) and the tenant allowlist,
* the summary aggregation shape given a monkeypatched ``get_traces``,
* error mapping when the CLI is present but the query fails / returns junk.

No network, no Supabase, no real ``hermes_cli`` -- the lazy import is monkeypatched.
"""

from __future__ import annotations

import sys
import types

import pytest

from api import langfuse_bridge as lb


# ── helpers ───────────────────────────────────────────────────────────────────

def _install_fake_get_traces(monkeypatch, fn):
    """Install a fake ``hermes_cli.gbauto_supabase_logs`` module exposing get_traces."""
    pkg = types.ModuleType("hermes_cli")
    sub = types.ModuleType("hermes_cli.gbauto_supabase_logs")
    sub.get_traces = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.gbauto_supabase_logs", sub)


def _force_import_failure(monkeypatch):
    """Ensure the lazy ``from hermes_cli.gbauto_supabase_logs import get_traces`` fails."""
    # Block the submodule so the lazy import raises ImportError.
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.gbauto_supabase_logs", None)


# ── pure logic: clamping ──────────────────────────────────────────────────────

def test_clamp_int_parses_bounds_and_defaults():
    assert lb._clamp_int("5", 7, 1, 30) == 5
    assert lb._clamp_int(999, 7, 1, 30) == 30   # clamped high
    assert lb._clamp_int(0, 7, 1, 30) == 1      # clamped low
    assert lb._clamp_int("nope", 7, 1, 30) == 7  # unparseable -> default
    assert lb._clamp_int(None, 7, 1, 30) == 7    # None -> default


# ── pure logic: summarize ─────────────────────────────────────────────────────

def test_summarize_aggregates_cost_tokens_and_distinct_agents():
    rows = [
        {"total_cost": "0.5", "total_tokens": "100", "agent": "gelby"},
        {"total_cost": 1.25, "total_tokens": 50, "agent": "gelby"},
        {"total_cost": 0.25, "total_tokens": 25, "agent": "jerry"},
    ]
    summary = lb._summarize(rows)
    assert summary["trace_count"] == 3
    assert summary["agent_count"] == 2          # distinct agents
    assert summary["total_tokens"] == 175
    assert summary["total_cost"] == 2.0


def test_summarize_tolerates_bad_values_and_missing_agent():
    rows = [
        {"total_cost": None, "total_tokens": None},
        {"total_cost": "junk", "total_tokens": "junk", "agent": ""},
        {"agent": "x"},
    ]
    summary = lb._summarize(rows)
    assert summary["trace_count"] == 3
    assert summary["total_cost"] == 0.0
    assert summary["total_tokens"] == 0
    assert summary["agent_count"] == 1          # empty-string agent excluded


def test_summarize_empty():
    summary = lb._summarize([])
    assert summary == {
        "trace_count": 0,
        "agent_count": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
    }


# ── tenant allowlist ──────────────────────────────────────────────────────────

def test_disallowed_tenant_is_rejected_without_touching_cli(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("get_traces must not be called for a disallowed tenant")

    _install_fake_get_traces(monkeypatch, boom)
    out = lb.get_langfuse("evil-tenant")
    assert out["ok"] is False
    assert out["available"] is True          # rejection is not a degrade
    assert out["error"] == "tenant not allowed"
    assert out["tenant"] == "evil-tenant"
    assert out["rows"] == []
    assert out["summary"]["trace_count"] == 0


def test_default_tenant_is_gbautomation(monkeypatch):
    captured = {}

    def fake(*, days, limit, search, client):
        captured["client"] = client
        return {"ok": True, "rows": []}

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse(None)
    assert captured["client"] == "gbautomation"
    assert out["tenant"] == "gbautomation"


def test_tenant_is_normalized_lowercase_and_stripped(monkeypatch):
    captured = {}

    def fake(*, days, limit, search, client):
        captured["client"] = client
        return {"ok": True, "rows": []}

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("  Smoke-Client  ")
    assert captured["client"] == "smoke-client"
    assert out["tenant"] == "smoke-client"


# ── DEGRADE path: import failure ──────────────────────────────────────────────

def test_import_failure_degrades_to_available_false(monkeypatch):
    _force_import_failure(monkeypatch)
    out = lb.get_langfuse("gbautomation", days=7, limit=100)
    assert out["ok"] is False
    assert out["available"] is False
    assert out["rows"] == []
    assert out["summary"]["trace_count"] == 0
    assert out["summary"]["total_cost"] == 0.0
    assert "error" in out


def test_import_failure_still_echoes_clamped_params(monkeypatch):
    _force_import_failure(monkeypatch)
    out = lb.get_langfuse("ecom", days=999, limit=99999)
    assert out["available"] is False
    assert out["days"] == lb.MAX_DAYS
    assert out["limit"] == lb.MAX_LIMIT


# ── happy path: summary shape from a monkeypatched get_traces ──────────────────

def test_happy_path_summary_shape_and_param_passthrough(monkeypatch):
    captured = {}

    def fake(*, days, limit, search, client):
        captured.update(days=days, limit=limit, search=search, client=client)
        return {
            "ok": True,
            "days": days,
            "limit": limit,
            "rows": [
                {"total_cost": 1.0, "total_tokens": 10, "agent": "a"},
                {"total_cost": 2.0, "total_tokens": 20, "agent": "b"},
            ],
            "gaps": [{"day": "2026-06-01"}],
            "coverage": [{"day": "2026-06-02"}],
        }

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("jid5274", days=3, limit=25, search="err")

    # params clamped-then-passed
    assert captured == {"days": 3, "limit": 25, "search": "err", "client": "jid5274"}
    assert out["ok"] is True
    assert out["available"] is True
    assert out["tenant"] == "jid5274"
    assert out["days"] == 3
    assert out["limit"] == 25
    assert len(out["rows"]) == 2
    assert out["gaps"] == [{"day": "2026-06-01"}]
    assert out["coverage"] == [{"day": "2026-06-02"}]
    assert out["summary"]["trace_count"] == 2
    assert out["summary"]["agent_count"] == 2
    assert out["summary"]["total_tokens"] == 30
    assert out["summary"]["total_cost"] == 3.0


def test_days_and_limit_are_clamped_before_calling_cli(monkeypatch):
    captured = {}

    def fake(*, days, limit, search, client):
        captured.update(days=days, limit=limit)
        return {"ok": True, "rows": []}

    _install_fake_get_traces(monkeypatch, fake)
    lb.get_langfuse("gbautomation", days=0, limit=0)
    assert captured["days"] == 1     # clamped to low bound
    assert captured["limit"] == 1


# ── error mapping: CLI present but misbehaves ──────────────────────────────────

def test_query_exception_degrades_but_stays_available(monkeypatch):
    def fake(*, days, limit, search, client):
        raise RuntimeError("supabase timeout xyz")

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("gbautomation")
    assert out["ok"] is False
    assert out["available"] is True          # CLI present -> not a PC degrade
    assert "supabase timeout xyz" in out["error"]
    assert out["rows"] == []


def test_result_not_ok_is_mapped_to_empty_payload(monkeypatch):
    def fake(*, days, limit, search, client):
        return {"ok": False, "error": "no such client"}

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("gbautomation")
    assert out["ok"] is False
    assert out["available"] is True
    assert out["error"] == "no such client"


def test_result_non_dict_is_handled(monkeypatch):
    def fake(*, days, limit, search, client):
        return ["not", "a", "dict"]

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("gbautomation")
    assert out["ok"] is False
    assert out["available"] is True
    assert out["rows"] == []


def test_rows_non_list_falls_back_to_empty(monkeypatch):
    def fake(*, days, limit, search, client):
        return {"ok": True, "rows": "oops", "gaps": "no", "coverage": None}

    _install_fake_get_traces(monkeypatch, fake)
    out = lb.get_langfuse("gbautomation")
    assert out["ok"] is True
    assert out["rows"] == []
    assert out["gaps"] == []
    assert out["coverage"] == []
    assert out["summary"]["trace_count"] == 0
