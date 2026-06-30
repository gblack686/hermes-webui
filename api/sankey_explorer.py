"""sankey_explorer.py — native (FastAPI-free) backend for the sankey-explorer
dashboard plugin, ported from the gbautomation ``sankey3js`` skill (plan B10).

This is the consolidation of the three vendored engine scripts that ran behind
the 9119 FastAPI dashboard plugin (``aggregate_source.py`` + ``from_supabase.py``
+ ``build_sankey.py``) into a single stdlib-only module. It exposes two helpers
consumed by ``api/routes.py``:

  * :func:`tables_payload` -> the PII-safe table catalog (metadata only, no rows).
  * :func:`chart_html`     -> a single self-contained 3D-Sankey HTML document for
    ``(table, dims, weight)``, suitable for embedding in an ``<iframe>``.

PII-safety (PRESERVED from the source engine, do NOT relax)
-----------------------------------------------------------
All output is AGGREGATE-ONLY by construction. The shared :func:`aggregate` core
pushes ``GROUP BY dims -> count(*) [, sum(weight)]`` into SQL on the live path
(raw rows never leave Supabase) or aggregates a committed fixture in-process on
the offline path; only the grouped tuples are ever returned. A PII-safe
``table_catalog`` enumerates the tables that may be charted and an explicit
EXCLUDE-LIST removes ``client-core`` and any PII-bearing table/column.

Hard rules honoured (see gbautomation MEMORY)
---------------------------------------------
  * EVERY Supabase read goes through the ``gbauto-supabase`` CLI ONLY
    (read-only ``query``); never psycopg2 / PostgREST / supabase-py.
  * No secrets are ever printed; the CLI resolves its own credentials.
  * Identifiers are validated as plain SQL identifiers before reaching SQL.
  * Aggregate-only output; never raw rows; PII tables/columns excluded.

Host split (Supabase is DNS-blocked from the dev PC, Mini-only)
--------------------------------------------------------------
  * ``SANKEY_EXPLORER_MODE=live``    -> always query live via gbauto-supabase.
  * ``SANKEY_EXPLORER_MODE=fixture`` -> always read committed fixtures.
  * unset (default) -> ``fixture`` when a committed fixture exists for the table
    (developable on the PC), otherwise ``live`` (the Mini path).
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Vendored assets (template + offline fixtures live next to this module).
# --------------------------------------------------------------------------- #
_DATA = Path(__file__).resolve().parent / "sankey_explorer_data"
TEMPLATE = _DATA / "template.html"
VENDOR = _DATA / "vendor"
_FIXTURES = _DATA / "fixtures"

THREE_VERSION = "0.160.0"
THREE_CDN = f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js"
ORBIT_CDN = f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/controls/OrbitControls.js"

# Default (CDN) importmap — byte-identical to the historic template block so the
# rendered chart matches the original 9119 plugin output.
CDN_IMPORTMAP = (
    '<script type="importmap">\n'
    '{ "imports": {\n'
    f'  "three": "{THREE_CDN}",\n'
    f'  "three/addons/": "https://unpkg.com/three@{THREE_VERSION}/examples/jsm/"\n'
    '}}\n'
    '</script>'
)


class SankeyError(Exception):
    """Raised for a bad request (caller maps to HTTP 4xx) or render failure."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Identifier guards / CLI read path (from from_supabase.py)
# --------------------------------------------------------------------------- #
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_ROW_KEYS = ("rows", "data", "result", "records", "items")


def supabase_bin() -> str:
    return os.environ.get("GBAUTO_SUPABASE_BIN", "gbauto-supabase")


def _ident(name: str, kind: str) -> str:
    """Validate a bare SQL identifier (a dim key / weight / distinct-on field)."""
    if not _IDENT.match(name or ""):
        raise SankeyError(
            f"{kind} '{name}' is not a plain SQL identifier "
            "(letters, digits, underscore; must start with a letter/underscore)")
    return name


def _table(name: str) -> str:
    if not _TABLE.match(name or ""):
        raise SankeyError(f"table '{name}' is not a valid identifier (schema.table allowed)")
    return name


def _parse_cli_json(stdout: str):
    """Parse the CLI's --json stdout, tolerating a human line before the blob."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
    return None


def extract_rows(parsed) -> list[dict]:
    """Pull a list-of-dicts out of whatever shape the CLI / fixture returned."""
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return [r for r in parsed if isinstance(r, dict)]
    if isinstance(parsed, dict):
        for key in _ROW_KEYS:
            value = parsed.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        if "ok" not in parsed and "error" not in parsed:
            return [parsed]
    return []


def fetch_live(sql: str, project: str = "gbauto") -> list[dict]:
    """Run a read-only SELECT through ``gbauto-supabase --json query``."""
    cmd = [supabase_bin(), "--project", project, "--json", "query", sql]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180, creationflags=creationflags,
        )
    except FileNotFoundError as err:
        raise SankeyError(
            f"{supabase_bin()} not found on PATH. All Supabase access must go "
            "through the gbauto-supabase CLI (export PATH=$HOME/.local/bin on "
            "Mini cron) or set GBAUTO_SUPABASE_BIN.", status=500) from err
    except subprocess.TimeoutExpired as err:
        raise SankeyError("gbauto-supabase query timed out after 180s", status=500) from err

    parsed = _parse_cli_json(proc.stdout)
    err = (proc.stderr or "").strip()
    if isinstance(parsed, dict) and (parsed.get("ok") is False or parsed.get("error")):
        raise SankeyError(
            f"query failed: {parsed.get('error') or parsed.get('message')}", status=500)
    if proc.returncode != 0:
        # NOTE: never echo argv (no secrets are in it, but keep output clean).
        raise SankeyError(
            f"gbauto-supabase exited {proc.returncode}: "
            f"{err or proc.stdout.strip() or 'no output'}", status=500)
    return extract_rows(parsed)


def load_fixture(path: Path) -> list[dict]:
    if not path.exists():
        raise SankeyError(f"fixture {path} not found", status=404)
    return extract_rows(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# PII-SAFE TABLE CATALOG (PRESERVED verbatim from aggregate_source.py)
# --------------------------------------------------------------------------- #
EXCLUDE_TABLE_PATTERNS: tuple[str, ...] = (
    "client-core",
    "client_core",
    "clientcore",
    "_pii",
    "pii_",
    "messages",        # raw message bodies
    "message_bodies",
    "transcripts",     # raw transcript text
    "chat_messages",
    "smoke_chat",      # chat content (anon read elsewhere, not for aggregation here)
    "contacts",
    "leads",
    "people",
    "users",
    "recipients",
    "emails",
)

EXCLUDE_COLUMN_TOKENS: frozenset[str] = frozenset({
    "email", "phone", "address", "ssn", "dob", "password", "secret",
    "body", "content", "text", "message", "transcript", "prompt",
    "completion", "snippet", "payload", "raw", "subject", "recipient",
    "recipients", "apikey",
})
EXCLUDE_COLUMN_FULLNAMES: frozenset[str] = frozenset({
    "name", "first_name", "last_name", "full_name", "display_name",
    "contact_name", "client_name", "person_name", "user_name", "username",
    "sender", "api_key", "access_token", "auth_token",
})

# table -> {dims, weights, distinct_on, order_by, unit, title}
_CATALOG: dict[str, dict] = {
    "prd_artifacts": {
        "dims": ["client", "status", "kind", "prd_type", "priority",
                 "tac_status", "agent_team"],
        "weights": [],
        "unit": "PRDs",
        "title": "PRD Artifacts",
    },
    "host_job_runs": {
        "dims": ["job_name", "host", "status", "scheduler"],
        "weights": ["duration_ms"],
        "unit": "runs",
        "title": "Host Job Runs",
    },
    "agent_runs": {
        "dims": ["agent", "status", "team", "exit_reason"],
        "weights": [],
        # Kanban mirror inserts one row per poll -> de-dupe to latest per task.
        "distinct_on": "task_id",
        "order_by": "created_at desc",
        "unit": "runs",
        "title": "Agent Runs",
    },
    "app_error_events": {
        "dims": ["source", "severity", "service"],
        "weights": [],
        "unit": "errors",
        "title": "App Error Events",
    },
    "tag_observations": {
        "dims": ["namespace", "tag", "source_repo"],
        "weights": [],
        "unit": "observations",
        "title": "Tag Observations",
    },
    "agent_feedback_events": {
        # channel/sentiment/slug are aggregate-safe (no free text emitted).
        "dims": ["channel", "sentiment", "plan_slug"],
        "weights": [],
        "unit": "feedback",
        "title": "Agent Feedback Events",
    },
    # Per-domain schemas (shop-intelligence vs mall-scanner — do NOT conflate).
    "ecom_observations": {
        "dims": ["brand", "lane", "gate", "production_green"],
        "weights": [],
        "unit": "observations",
        "title": "Ecom Observations",
    },
    "mall_observations": {
        "dims": ["store", "category", "status"],
        "weights": [],
        "unit": "observations",
        "title": "Mall Observations",
    },
}


def _is_excluded_table(table: str) -> bool:
    low = table.lower()
    return any(pat in low for pat in EXCLUDE_TABLE_PATTERNS)


def _is_excluded_column(col: str) -> bool:
    low = col.lower()
    if low in EXCLUDE_COLUMN_FULLNAMES:
        return True
    tokens = set(low.replace("-", "_").split("_"))
    return bool(tokens & EXCLUDE_COLUMN_TOKENS)


def table_catalog() -> dict[str, dict]:
    """Return the PII-safe catalog: {table: {dims, weights, ...}}.

    Tables matching the EXCLUDE-LIST are removed, and any dim/weight that matches
    an excluded column pattern is stripped, so the catalog is PII-safe by
    construction even if a future edit adds a hazardous column.
    """
    safe: dict[str, dict] = {}
    for table, meta in _CATALOG.items():
        if _is_excluded_table(table):
            continue
        dims = [d for d in meta.get("dims", []) if not _is_excluded_column(d)]
        weights = [w for w in meta.get("weights", []) if not _is_excluded_column(w)]
        entry = dict(meta)
        entry["dims"] = dims
        entry["weights"] = weights
        safe[table] = entry
    return safe


def allowed_tables() -> list[str]:
    return sorted(table_catalog().keys())


def excluded_tables() -> list[str]:
    """Catalog entries that are present in source but filtered out (audit aid)."""
    return sorted(t for t in _CATALOG if _is_excluded_table(t))


# --------------------------------------------------------------------------- #
# AGGREGATION (from aggregate_source.py — aggregate-only, never raw rows)
# --------------------------------------------------------------------------- #
def _norm(v) -> str:
    return "(none)" if v is None or v == "" else str(v)


def _to_num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_aggregate_sql(table: str, dims: list[str], *, weight: Optional[str],
                        where: Optional[str], distinct_on: Optional[str],
                        order_by: Optional[str]) -> str:
    """Construct a read-only GROUP BY SELECT — aggregate-only, never raw rows."""
    dim_cols = ", ".join(dims)
    measures = "count(*) as n"
    if weight:
        measures += f", coalesce(sum({weight}), 0) as {weight}"

    if distinct_on:
        inner_cols = ", ".join(dict.fromkeys([*dims, *([weight] if weight else []), distinct_on]))
        ob_terms = [distinct_on]
        ob = (order_by or "").strip()
        if ob and not ob.lower().startswith(distinct_on.lower()):
            ob_terms.append(ob)
        inner = (f"select distinct on ({distinct_on}) {inner_cols} from {table}"
                 + (f" where {where}" if where else "")
                 + " order by " + ", ".join(ob_terms))
        return (f"select {dim_cols}, {measures} from ({inner}) as deduped "
                f"group by {dim_cols}")

    sql = f"select {dim_cols}, {measures} from {table}"
    if where:
        sql += f" where {where}"
    sql += f" group by {dim_cols}"
    return sql


def aggregate_rows(rows: list[dict], dims: list[str], weight: Optional[str]) -> list[dict]:
    """Aggregate raw rows in-process -> aggregate-only records (offline path)."""
    counts: dict[tuple, int] = defaultdict(int)
    sums: dict[tuple, float] = defaultdict(float)
    for r in rows:
        key = tuple(_norm(r.get(d)) for d in dims)
        counts[key] += 1
        if weight:
            sums[key] += _to_num(r.get(weight))
    out: list[dict] = []
    for key, n in counts.items():
        rec = {d: key[i] for i, d in enumerate(dims)}
        rec["n"] = n
        if weight:
            rec[weight] = sums[key]
        out.append(rec)
    out.sort(key=lambda d: d["n"], reverse=True)
    return out


def aggregate(table: str, dims: list[str], *, weight: Optional[str] = None,
              where: Optional[str] = None, fixture: Optional[Path] = None,
              project: str = "gbauto") -> dict:
    """Produce AGGREGATE-ONLY records for (table, dims, weight[, where]).

    Validates the request against the PII-safe catalog and identifier rules.
    On the live path the GROUP BY runs in SQL (raw rows never leave Supabase);
    offline it aggregates the committed fixture in-process.
    """
    table = _table(table)
    if _is_excluded_table(table):
        raise SankeyError(
            f"table '{table}' is on the PII exclude-list and cannot be charted")
    catalog = table_catalog()
    if table not in catalog:
        raise SankeyError(
            f"table '{table}' is not in the PII-safe catalog "
            f"(allowed: {', '.join(catalog)})", status=404)

    meta = catalog[table]
    if not dims or len(dims) < 1:
        raise SankeyError("need at least 1 dim to aggregate")
    dim_keys: list[str] = []
    for d in dims:
        d = _ident(d, "dim")
        if _is_excluded_column(d):
            raise SankeyError(f"dim '{d}' matches a PII column pattern and is forbidden")
        if d not in meta["dims"]:
            raise SankeyError(
                f"dim '{d}' not allowed for {table} "
                f"(allowed dims: {', '.join(meta['dims'])})")
        dim_keys.append(d)

    w = None
    if weight:
        w = _ident(weight, "weight")
        if _is_excluded_column(w):
            raise SankeyError(f"weight '{w}' matches a PII column pattern and is forbidden")
        if w in dim_keys:
            raise SankeyError(f"weight '{w}' is also a dim; pick a numeric column not in dims")
        if w not in meta.get("weights", []):
            raise SankeyError(
                f"weight '{w}' not allowed for {table} "
                f"(allowed weights: {', '.join(meta.get('weights', [])) or 'none'})")

    distinct_on = meta.get("distinct_on")
    order_by = meta.get("order_by")

    if fixture is not None:
        raw = load_fixture(Path(fixture))
        records = aggregate_rows(raw, dim_keys, w)
        src = f"fixture {fixture}"
    else:
        sql = build_aggregate_sql(table, dim_keys, weight=w, where=where,
                                  distinct_on=distinct_on, order_by=order_by)
        agg = fetch_live(sql, project)
        records = []
        for r in agg:
            rec = {d: _norm(r.get(d)) for d in dim_keys}
            rec["n"] = int(_to_num(r.get("n")))
            if w:
                rec[w] = _to_num(r.get(w))
            records.append(rec)
        records.sort(key=lambda d: d["n"], reverse=True)
        src = f"{table} (live via {supabase_bin()})"

    return {
        "table": table,
        "dims": dim_keys,
        "weight": w,
        "unit": meta.get("unit", "rows"),
        "title": meta.get("title", table),
        "count": len(records),
        "source": src,
        "records": records,
    }


# --------------------------------------------------------------------------- #
# RENDER (from build_sankey.py — template substitution + importmap)
# --------------------------------------------------------------------------- #
def _fetch_lib(url: str, cache_name: str) -> str:
    """Return library source, preferring a local vendor cache, else fetching once."""
    cache = VENDOR / cache_name
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    import urllib.request
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 (trusted CDN)
        src = resp.read().decode("utf-8")
    VENDOR.mkdir(parents=True, exist_ok=True)
    cache.write_text(src, encoding="utf-8")
    return src


def build_importmap(inline: bool) -> str:
    """CDN importmap by default; inlined data-URI modules when *inline* is True.

    Degrades gracefully: if the libs cannot be fetched/vendored we fall back to
    the CDN importmap so the chart still renders (needs network when viewed).
    """
    if not inline:
        return CDN_IMPORTMAP
    try:
        three_src = _fetch_lib(THREE_CDN, f"three-{THREE_VERSION}.module.js")
        orbit_src = _fetch_lib(ORBIT_CDN, f"OrbitControls-{THREE_VERSION}.js")
    except Exception:
        return CDN_IMPORTMAP

    def uri(s: str) -> str:  # base64 data URI (JSON-safe alphabet)
        return "data:text/javascript;base64," + base64.b64encode(s.encode("utf-8")).decode("ascii")

    return (
        '<script type="importmap">\n'
        '{ "imports": {\n'
        f'  "three": "{uri(three_src)}",\n'
        f'  "three/addons/controls/OrbitControls.js": "{uri(orbit_src)}"\n'
        '}}\n'
        '</script>'
    )


def _render_html(result: dict, dims: list[str], weight: Optional[str],
                 *, inline_three: bool = False) -> str:
    """Render aggregate records into a self-contained Sankey HTML document.

    Records are already aggregate-only ({**dims, "n": count[, "<weight>": sum]}).
    Flows are sized by the requested weight column when given, else by ``n``.
    """
    unit = result.get("unit", "rows")
    title = f"{result.get('title', result['table'])} — Sankey"

    dim_specs = [{"key": k, "label": k.replace("_", " ").title()} for k in dims]

    if weight:
        weight_key = weight
        weight_label = weight.replace("_", " ").title()
    else:
        weight_key = "n"
        weight_label = unit.title()

    default = list(dims[:3])
    while len(default) < 3:
        default.append(dims[len(default) % len(dims)])

    records = result["records"]
    payload = {"count": len(records), "records": records}

    if not TEMPLATE.is_file():
        raise SankeyError(f"chart template missing at {TEMPLATE}", status=500)
    html = TEMPLATE.read_text(encoding="utf-8")
    repl = {
        "__DATA__": json.dumps(payload, separators=(",", ":")),
        "__DIMS__": json.dumps(dim_specs),
        "__DEFAULTS__": json.dumps(default[:3]),
        "__TITLE__": title,
        "__COUNT__": str(len(records)),
        "__UNIT__": unit,
        "__WEIGHT__": json.dumps({"key": weight_key, "label": weight_label}),
        "__IMPORTMAP__": build_importmap(inline_three),
    }
    for token, val in repl.items():
        html = html.replace(token, val)
    leftover = [tok for tok in repl if tok in html]
    if leftover:
        raise SankeyError(
            f"chart render failed: unsubstituted placeholders {leftover}", status=500)
    return html


# --------------------------------------------------------------------------- #
# Host-split source resolution + public helpers consumed by api/routes.py
# --------------------------------------------------------------------------- #
def _mode() -> str:
    m = (os.environ.get("SANKEY_EXPLORER_MODE") or "").strip().lower()
    return m if m in ("live", "fixture") else "auto"


def _fixture_for(table: str) -> Optional[Path]:
    """Return the committed fixture path for a table, if one exists."""
    fx = _FIXTURES / f"{table}.sample.json"
    return fx if fx.exists() else None


def _resolve_source(table: str) -> Optional[Path]:
    """Decide live (None) vs fixture (Path) per the host-split policy."""
    mode = _mode()
    fx = _fixture_for(table)
    if mode == "live":
        return None
    if mode == "fixture":
        if fx is None:
            raise SankeyError(
                f"no committed fixture for table '{table}' "
                "(SANKEY_EXPLORER_MODE=fixture)", status=404)
        return fx
    # auto: prefer fixture when present (PC-developable), else live (Mini).
    return fx


def tables_payload() -> dict:
    """Return the PII-safe catalog for the /tables endpoint (metadata only)."""
    catalog = table_catalog()
    out: dict[str, dict] = {}
    for table, meta in catalog.items():
        out[table] = {
            "dims": meta.get("dims", []),
            "weights": meta.get("weights", []),
            "unit": meta.get("unit", "rows"),
            "title": meta.get("title", table),
            "has_fixture": _fixture_for(table) is not None,
        }
    return {
        "mode": _mode(),
        "tables": out,
        "excluded": excluded_tables(),
    }


def chart_html(table: str, dims: str, weight: Optional[str] = None,
               *, inline_three: bool = False) -> str:
    """Render a self-contained 3D-Sankey HTML for (table, dims, weight).

    *dims* is the comma-separated dim spec (2-3 dims). Raises :class:`SankeyError`
    (with ``.status``) on any bad request or render failure so the caller can map
    it to a clean HTTP status without crashing the server.
    """
    dim_list = [d.strip() for d in (dims or "").split(",") if d.strip()]
    if len(dim_list) < 2:
        raise SankeyError("need at least 2 dims")
    if len(dim_list) > 3:
        raise SankeyError("at most 3 dims (left/middle/right)")

    # Reject unknown / PII-excluded tables up front (independent of data mode)
    # so the error is "not in catalog" rather than "no fixture".
    if table not in table_catalog():
        raise SankeyError(
            f"table '{table}' is not in the PII-safe catalog", status=404)

    fixture = _resolve_source(table)
    result = aggregate(table, dim_list, weight=weight or None, fixture=fixture)
    return _render_html(result, dim_list, weight or None, inline_three=inline_three)
