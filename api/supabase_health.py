"""Tenant-scoped Supabase data health for the GBAutomation Supabase panel (plan B9a).

Ported from 9119's ``hermes_cli.gbauto_supabase_health`` into webui. All database
access goes through the ``gbauto-supabase`` CLI (the only sanctioned Supabase write
path -- see the GBAuto MEMORY). The dashboard exposes a single fixed read endpoint
(``GET /api/gbauto/supabase-health``); callers never provide raw SQL.

PC / offline degradation
------------------------
Supabase is DNS-blocked from the PC and the ``gbauto-supabase`` CLI is Mini-only,
so :func:`get_supabase_health` degrades to an empty-but-well-formed payload
(``ok=False, available=False``) whenever the CLI is absent or a query fails. The
panel then renders the committed snapshot fixture only -- live health is Mini-only
(``mini_pending``).

Tenant safety
-------------
Every relation is filtered to the active tenant's ``client_slug`` / ``tenant`` /
``profile`` marker AT QUERY TIME via :data:`RELATION_SPECS`. The tenant must be in
:data:`ALLOWED_TENANTS` or the read is rejected, mirroring 9119's tenant model.
Output is aggregate-only (row counts / bad-row counts / latest timestamp) -- no PII
rows ever leave this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Dict, Optional


# Mirror of 9119's ALLOWED_TENANTS (hermes_cli.gbauto_chat). Self-contained so the
# module imports cleanly on the PC where hermes_cli is unavailable.
ALLOWED_TENANTS = {"smoke-client", "gbautomation", "jid5274", "ecom"}

# Cheap process-local cache so a panel that polls does not re-shell the CLI.
_CACHE: Dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 30


def _sql_literal(value: str) -> str:
    # Matches 9119: single-quote escape + ``%`` doubling for the CLI's format layer.
    return "'" + value.replace("'", "''").replace("%", "%%") + "'"


RELATION_SPECS: list[dict[str, str]] = [
    {"name": "agent_sessions", "kind": "base_table", "family": "sessions", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "0"},
    {"name": "v_agent_session_history", "kind": "read_view", "family": "sessions", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "ts", "bad": "0"},
    {"name": "chat_messages", "kind": "base_table", "family": "sessions", "filter": "lower(coalesce(tenant::text, '')) = {tenant}", "latest": "created_at", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked'))"},
    {"name": "agent_log_artifacts", "kind": "base_table", "family": "logs", "filter": "lower(coalesce(client_slug::text, '')) = {tenant} or lower(coalesce(repo_slug::text, '')) = {tenant}", "latest": "last_synced_at", "bad": "0"},
    {"name": "agent_runs", "kind": "base_table", "family": "runs", "filter": "lower(coalesce(client_slug::text, '')) = {tenant} or lower(coalesce(repo_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked','partial'))"},
    {"name": "ops_run_timeline", "kind": "read_view", "family": "runs", "filter": "lower(coalesce(client_slug::text, '')) = {tenant} or lower(coalesce(repo_slug::text, '')) = {tenant}", "latest": "started_at", "bad": "count(*) filter (where lower(coalesce(status_family::text, '')) = 'failed')"},
    {"name": "ops_recent_failures", "kind": "read_view", "family": "runs", "filter": "lower(coalesce(client_slug::text, '')) = {tenant} or lower(coalesce(repo_slug::text, '')) = {tenant}", "latest": "started_at", "bad": "count(*)"},
    {"name": "agent_profiles", "kind": "base_table", "family": "profiles", "filter": "lower(coalesce(tenant::text, '')) = {tenant}", "latest": "null::timestamptz", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked','inactive'))"},
    {"name": "agent_profile_teams", "kind": "base_table", "family": "profiles", "filter": "lower(coalesce(tenant::text, '')) = {tenant}", "latest": "null::timestamptz", "bad": "0"},
    {"name": "langfuse_traces", "kind": "base_table", "family": "observability", "filter": "lower(coalesce(profile::text, '')) in ('carlos','jason-va','jason-operations','jason-marketing')", "latest": "trace_timestamp", "bad": "0"},
    {"name": "app_error_events", "kind": "base_table", "family": "errors", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "count(*)"},
    {"name": "kanban_tasks", "kind": "base_table", "family": "kanban", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked'))"},
    {"name": "host_job_runs", "kind": "base_table", "family": "runs", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked','partial'))"},
    {"name": "prd_artifacts", "kind": "base_table", "family": "prds", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "0"},
    {"name": "github_pr_lifecycle_receipts", "kind": "base_table", "family": "github", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "updated_at", "bad": "count(*) filter (where lower(coalesce(status::text, '')) in ('failed','error','blocked','partial'))"},
    {"name": "tag_observations", "kind": "base_table", "family": "observability", "filter": "lower(coalesce(client_slug::text, '')) = {tenant}", "latest": "created_at", "bad": "0"},
]


TERMINOLOGY = [
    {"term": "base_table", "label": "Base table", "meaning": "Durable writable Supabase table; backfills and runtime jobs write here."},
    {"term": "read_view", "label": "Read view", "meaning": "Dashboard projection or rollup over one or more base tables."},
    {"term": "smoke_view", "label": "Smoke view", "meaning": "Anon-safe health projection reused by smoke clients and dashboards."},
    {"term": "compat_view", "label": "Compatibility view", "meaning": "Stable facade over renamed or converging schemas."},
]


def cli_available() -> bool:
    """True when the ``gbauto-supabase`` CLI is on PATH (Mini-only in practice)."""
    return shutil.which("gbauto-supabase") is not None


def _run_cli(sql: str, *, timeout: int = 90) -> list[dict[str, Any]]:
    binary = shutil.which("gbauto-supabase")
    if not binary:
        raise RuntimeError("gbauto-supabase CLI is not on PATH")

    proc = subprocess.run(
        [binary, "--json", "query", sql],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "gbauto-supabase query failed").strip()
        raise RuntimeError(message[:1200])

    data = json.loads(proc.stdout or "[]")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(str(data.get("error") or "gbauto-supabase query failed"))
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        data = data["value"]
    if not isinstance(data, list):
        raise RuntimeError("gbauto-supabase returned an unexpected response shape")
    return [row for row in data if isinstance(row, dict)]


def _relation_sql(spec: dict[str, str], tenant_sql: str) -> str:
    latest = spec["latest"]
    latest_expr = latest if latest.startswith("null::") else f"max({latest})"
    where = spec["filter"].format(tenant=tenant_sql)
    return (
        "select "
        f"{_sql_literal(spec['name'])} as relation, "
        f"{_sql_literal(spec['kind'])} as kind, "
        f"{_sql_literal(spec['family'])} as family, "
        "count(*)::int as rows, "
        f"coalesce(({spec['bad']}), 0)::int as bad_rows, "
        f"{latest_expr} as latest "
        f"from public.{spec['name']} where {where}"
    )


def _empty_payload(tenant: str, *, available: bool, error: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "available": available,
        "tenant": tenant,
        "terminology": TERMINOLOGY,
        "summary": {
            "relations": 0,
            "empty_relations": 0,
            "attention_relations": 0,
            "total_rows": 0,
            "bad_rows": 0,
            "by_kind": {},
            "by_family": {},
        },
        "relations": [],
    }
    if error:
        payload["error"] = error
    return payload


def load_supabase_health(tenant: Optional[str] = None) -> dict[str, Any]:
    """Live tenant-scoped Supabase health via the CLI (raises if CLI/query fails)."""
    selected = (tenant or "gbautomation").strip().lower()
    if selected not in ALLOWED_TENANTS:
        raise ValueError(f"tenant not allowed: {selected}")

    tenant_sql = _sql_literal(selected)
    sql = "\nunion all\n".join(_relation_sql(spec, tenant_sql) for spec in RELATION_SPECS)
    rows = _run_cli(sql, timeout=90)
    for row in rows:
        row["empty"] = int(row.get("rows") or 0) == 0
        row["status"] = "empty" if row["empty"] else ("attention" if int(row.get("bad_rows") or 0) else "ok")

    by_kind: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for row in rows:
        by_kind[str(row["kind"])] = by_kind.get(str(row["kind"]), 0) + int(row.get("rows") or 0)
        by_family[str(row["family"])] = by_family.get(str(row["family"]), 0) + int(row.get("rows") or 0)

    return {
        "ok": True,
        "available": True,
        "tenant": selected,
        "terminology": TERMINOLOGY,
        "summary": {
            "relations": len(rows),
            "empty_relations": sum(1 for row in rows if row["empty"]),
            "attention_relations": sum(1 for row in rows if row["status"] == "attention"),
            "total_rows": sum(int(row.get("rows") or 0) for row in rows),
            "bad_rows": sum(int(row.get("bad_rows") or 0) for row in rows),
            "by_kind": by_kind,
            "by_family": by_family,
        },
        "relations": rows,
    }


def get_supabase_health(tenant: Optional[str] = None) -> dict[str, Any]:
    """Degrade-safe wrapper used by the route handler.

    Returns a well-formed payload even when the CLI is missing (PC) or a query
    fails. Callers always get ``relations`` / ``summary`` keys so the panel can
    render without special-casing the error path.
    """
    selected = (tenant or "gbautomation").strip().lower()
    if selected not in ALLOWED_TENANTS:
        return _empty_payload(selected, available=cli_available(), error="tenant not allowed")

    if not cli_available():
        return _empty_payload(selected, available=False)

    key = f"health:{selected}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]

    try:
        payload = load_supabase_health(selected)
    except Exception as exc:  # CLI present but query failed -- degrade, don't crash.
        payload = _empty_payload(selected, available=True, error=str(exc)[:500])
    _CACHE[key] = (now, payload)
    return payload
