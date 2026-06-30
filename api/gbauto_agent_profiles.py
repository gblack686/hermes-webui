"""Tenant-scoped Supabase agent-profile catalog for the Profiles Catalog panel (plan B12).

Ported from 9119's ``hermes_cli.gbauto_agent_profiles`` into webui. The source of
truth is the neutral ``agent_profile_*`` schema (teams / catalog / routes). All
database access goes through the ``gbauto-supabase`` CLI -- the only sanctioned
Supabase read/write path (see the GBAuto MEMORY). The dashboard exposes a single
fixed read endpoint (``GET /api/gbauto/agent-profiles``); callers never provide
raw SQL.

PC / offline degradation
------------------------
Supabase is DNS-blocked from the PC and the ``gbauto-supabase`` CLI is Mini-only,
so :func:`get_profile_catalog` degrades to an empty-but-well-formed payload
(``ok=False, available=False``) whenever the CLI is absent or a query fails. The
panel then falls back to the committed snapshot fixture
(``static/gbauto/agent-profiles/catalog.json`` served by ``api/snapshot.py``);
live catalog is Mini-only (``mini_pending``).

Tenant safety
-------------
Every relation is filtered to the active tenant's ``tenant`` marker AT QUERY TIME.
The tenant must be in :data:`ALLOWED_TENANTS` or the read is rejected, mirroring
9119's tenant model. Output is aggregate profile metadata only (ids / labels /
roles / models / counts) -- no chat content or PII rows ever leave this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional


# Mirror of 9119's ALLOWED_TENANTS (hermes_cli.gbauto_chat). Self-contained so the
# module imports cleanly on the PC where hermes_cli is unavailable.
ALLOWED_TENANTS = {"smoke-client", "gbautomation", "jid5274", "ecom"}

# Cheap process-local cache so a panel that polls does not re-shell the CLI.
_CACHE: Dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 30


def _sql_literal(value: str) -> str:
    # Matches 9119: single-quote escape + ``%`` doubling for the CLI's format layer.
    return "'" + value.replace("'", "''").replace("%", "%%") + "'"


_TEAMS_SQL = (
    "select team_key, tenant, team_id, display_name, runtime, purpose, "
    "canonical_agent_team, canonical_profile_team, orchestrator_profile, lead_profile, "
    "specialist_profiles, existing_specialist_profiles, source_path, indexed_at, metadata "
    "from public.agent_profile_teams "
    "where tenant = {tenant} "
    "order by display_name, team_id"
)

_PROFILES_SQL = (
    "select profile_key, tenant, profile_id, profile_type, runtime, display_name, role, "
    "status, model, provider, source_path, source_kind, deployment_target, deploy_user, "
    "deploy_path, team_id, team_display_name, route_keys, suggested_skills, "
    "prompt_manifest_path, package_path, indexed_at, skill_count, route_count "
    "from public.agent_profile_catalog "
    "where tenant = {tenant} "
    "order by coalesce(team_id, ''), profile_type, profile_id"
)

_ROUTES_SQL = (
    "select profile_route_key, tenant, route_name, route_kind, source_profile_key, "
    "target_type, target_profile_key, target_profile_id, target_team_id, source_path, "
    "metadata, indexed_at "
    "from public.agent_profile_routes "
    "where tenant = {tenant} "
    "order by source_path, route_name, target_profile_id, target_team_id"
)


def cli_available() -> bool:
    """True when the ``gbauto-supabase`` CLI is on PATH (Mini-only in practice)."""
    return shutil.which("gbauto-supabase") is not None


def _run_cli(sql: str, *, timeout: int = 90) -> List[dict[str, Any]]:
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


def _empty_payload(tenant: str, *, available: bool, error: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "available": available,
        "source": "supabase:agent_profile_catalog",
        "tenant": tenant,
        "teams": [],
        "profiles": [],
        "routes": [],
    }
    if error:
        payload["error"] = error
    return payload


def load_profile_catalog(tenant: Optional[str] = None) -> dict[str, Any]:
    """Live tenant-scoped agent-profile catalog via the CLI (raises on CLI/query failure)."""
    selected = (tenant or "gbautomation").strip().lower()
    if selected not in ALLOWED_TENANTS:
        raise ValueError(f"tenant not allowed: {selected}")

    tenant_sql = _sql_literal(selected)
    teams = _run_cli(_TEAMS_SQL.format(tenant=tenant_sql))
    profiles = _run_cli(_PROFILES_SQL.format(tenant=tenant_sql))
    routes = _run_cli(_ROUTES_SQL.format(tenant=tenant_sql))
    return {
        "ok": True,
        "available": True,
        "source": "supabase:agent_profile_catalog",
        "tenant": selected,
        "teams": teams,
        "profiles": profiles,
        "routes": routes,
    }


def get_profile_catalog(tenant: Optional[str] = None) -> dict[str, Any]:
    """Degrade-safe wrapper used by the route handler.

    Returns a well-formed payload even when the CLI is missing (PC) or a query
    fails. Callers always get ``teams`` / ``profiles`` / ``routes`` keys so the
    panel can render (or fall back to the committed snapshot) without special-
    casing the error path.
    """
    selected = (tenant or "gbautomation").strip().lower()
    if selected not in ALLOWED_TENANTS:
        return _empty_payload(selected, available=cli_available(), error="tenant not allowed")

    if not cli_available():
        return _empty_payload(selected, available=False)

    key = f"catalog:{selected}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]

    try:
        payload = load_profile_catalog(selected)
    except Exception as exc:  # CLI present but query failed -- degrade, don't crash.
        payload = _empty_payload(selected, available=True, error=str(exc)[:500])
    _CACHE[key] = (now, payload)
    return payload
