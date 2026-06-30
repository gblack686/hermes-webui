"""Langfuse trace bridge for the GBAutomation Langfuse panel (plan B9b).

Thin wrapper over ``hermes_cli.gbauto_supabase_logs.get_traces`` -- the same
sanctioned read path the 9119 dashboard used for its Langfuse view -- following the
``api/kanban_bridge`` precedent (lazy import the Mini-only ``hermes_cli`` inside the
call, degrade gracefully when it is unavailable).

PC / offline degradation
------------------------
``hermes_cli`` is not importable on the PC (Supabase is DNS-blocked and the
``gbauto-supabase`` CLI is Mini-only), so :func:`get_langfuse` returns a
well-formed empty payload (``ok=False, available=False``) whenever the import or
query fails. The panel then renders the committed snapshot fixture's
``langfuse_traces`` table only -- live traces are Mini-only (``mini_pending``).

Tenant safety
-------------
The ``tenant`` argument maps to ``get_traces(client=...)``, which validates it
against the Mini's ``ALLOWED_CLIENTS`` allowlist and scopes traces to that tenant.
Output is sanitized trace metadata (cost / tokens / agent / profile) -- no PII.
"""

from __future__ import annotations

from typing import Any, Optional


# Mirror of the 9119 read path's allowlist so the route can validate before
# touching the Mini-only CLI.
ALLOWED_TENANTS = {"smoke-client", "gbautomation", "jid5274", "ecom"}

MAX_DAYS = 30
MAX_LIMIT = 500


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _empty_payload(tenant: str, days: int, limit: int, *, available: bool,
                   error: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "available": available,
        "tenant": tenant,
        "days": days,
        "limit": limit,
        "rows": [],
        "gaps": [],
        "coverage": [],
        "summary": {
            "trace_count": 0,
            "agent_count": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        },
    }
    if error:
        payload["error"] = error
    return payload


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_cost = 0.0
    total_tokens = 0
    agents: set[str] = set()
    for row in rows:
        try:
            total_cost += float(row.get("total_cost") or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_tokens += int(row.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
        agent = row.get("agent")
        if agent:
            agents.add(str(agent))
    return {
        "trace_count": len(rows),
        "agent_count": len(agents),
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
    }


def get_langfuse(tenant: Optional[str] = None, *, days: Any = 7, limit: Any = 100,
                 search: Optional[str] = None) -> dict[str, Any]:
    """Degrade-safe Langfuse trace read for the panel.

    Returns sanitized trace rows + stat-card summary. Degrades to an empty
    payload (``available=False``) on the PC where ``hermes_cli`` cannot import.
    """
    selected = (tenant or "gbautomation").strip().lower()
    days_i = _clamp_int(days, 7, 1, MAX_DAYS)
    limit_i = _clamp_int(limit, 100, 1, MAX_LIMIT)

    if selected not in ALLOWED_TENANTS:
        return _empty_payload(selected, days_i, limit_i, available=True, error="tenant not allowed")

    try:
        # Lazy import: hermes_cli is Mini-only. ImportError on the PC -> degrade.
        from hermes_cli.gbauto_supabase_logs import get_traces  # type: ignore
    except Exception as exc:  # ImportError or transitive failure
        return _empty_payload(selected, days_i, limit_i, available=False, error=str(exc)[:300])

    try:
        result = get_traces(days=days_i, limit=limit_i, search=search, client=selected)
    except Exception as exc:  # CLI present but query failed -- degrade, don't crash.
        return _empty_payload(selected, days_i, limit_i, available=True, error=str(exc)[:500])

    if not isinstance(result, dict) or not result.get("ok"):
        err = (result or {}).get("error") if isinstance(result, dict) else None
        return _empty_payload(selected, days_i, limit_i, available=True, error=str(err or "")[:500] or None)

    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    return {
        "ok": True,
        "available": True,
        "tenant": selected,
        "days": result.get("days", days_i),
        "limit": result.get("limit", limit_i),
        "rows": rows,
        "gaps": result.get("gaps") if isinstance(result.get("gaps"), list) else [],
        "coverage": result.get("coverage") if isinstance(result.get("coverage"), list) else [],
        "summary": _summarize(rows),
    }
