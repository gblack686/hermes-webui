"""Coverage for the native sankey-explorer backend (plan B10).

The 9119 FastAPI plugin was ported into webui as the stdlib module
``api/sankey_explorer.py`` plus two GET branches in ``api/routes.py``
(``/api/plugins/sankey-explorer/{tables,chart}``). These tests assert the
PII-safe catalog is preserved, the aggregate core stays aggregate-only, the
chart renders from the vendored template, and the routes dispatch correctly.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from api import routes, sankey_explorer as se

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "api" / "sankey_explorer_data" / "fixtures"


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def header(self, key):
        for k, v in self.response_headers:
            if k.lower() == key.lower():
                return v
        return None

    def body_bytes(self):
        return self.wfile.getvalue()

    def body_text(self):
        return self.wfile.getvalue().decode("utf-8")

    def body_json(self):
        # Tolerate gzip (j() compresses bodies > 1KB).
        raw = self.wfile.getvalue()
        if self.header("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


# ── PII-safe catalog (PRESERVED) ──────────────────────────────────────────────

def test_catalog_is_pii_safe():
    cat = se.table_catalog()
    assert cat, "catalog must be non-empty"
    assert all(not se._is_excluded_table(t) for t in cat)
    # client-core never survives.
    assert not any("client" in t and "core" in t for t in cat)
    assert "host_job_runs" in cat and "prd_artifacts" in cat


def test_excluded_table_and_column_patterns_preserved():
    assert se._is_excluded_table("client_core_contacts")
    assert se._is_excluded_table("smoke_chat_messages")
    assert se._is_excluded_column("email")
    assert se._is_excluded_column("first_name")
    # job_name / status remain chartable (token match, not substring).
    assert not se._is_excluded_column("job_name")
    assert not se._is_excluded_column("status")


def test_synthetic_pii_table_is_filtered(monkeypatch):
    se._CATALOG["client_core_contacts"] = {"dims": ["client", "email"], "weights": []}
    try:
        assert "client_core_contacts" not in se.table_catalog()
    finally:
        del se._CATALOG["client_core_contacts"]


# ── Aggregate core is aggregate-only ──────────────────────────────────────────

def test_aggregate_offline_is_aggregate_only():
    fx = FIXTURES / "host_job_runs.sample.json"
    res = se.aggregate("host_job_runs", ["job_name", "host", "status"], fixture=fx)
    assert res["count"] > 0
    assert all(r["n"] > 0 for r in res["records"])
    # Records carry only dims + n — no raw columns leak through.
    assert all(set(r.keys()) == {"job_name", "host", "status", "n"}
               for r in res["records"])
    total = sum(r["n"] for r in res["records"])
    assert total == len(se.load_fixture(fx))


def test_aggregate_rejects_excluded_table_and_dim():
    fx = FIXTURES / "host_job_runs.sample.json"
    with pytest.raises(se.SankeyError):
        se.aggregate("client_core_contacts", ["client"], fixture=fx)
    with pytest.raises(se.SankeyError):
        se.aggregate("host_job_runs", ["secret_token"], fixture=fx)


# ── Chart render from the vendored template ───────────────────────────────────

def test_chart_html_substitutes_template(monkeypatch):
    monkeypatch.setenv("SANKEY_EXPLORER_MODE", "fixture")
    html = se.chart_html("host_job_runs", "job_name,host,status")
    assert html.lstrip().lower().startswith("<!doctype html>")
    # All placeholders substituted.
    for token in ("__DATA__", "__DIMS__", "__TITLE__", "__IMPORTMAP__", "__WEIGHT__"):
        assert token not in html
    assert "importmap" in html


def test_chart_requires_two_dims(monkeypatch):
    monkeypatch.setenv("SANKEY_EXPLORER_MODE", "fixture")
    with pytest.raises(se.SankeyError):
        se.chart_html("host_job_runs", "job_name")


# ── Route dispatch ────────────────────────────────────────────────────────────

def test_route_tables_returns_catalog():
    handler = _FakeHandler()
    handled = routes.handle_get(handler, urlparse("/api/plugins/sankey-explorer/tables"))
    assert handled is True
    assert handler.status == 200
    payload = handler.body_json()
    assert "tables" in payload and "host_job_runs" in payload["tables"]
    assert payload["tables"]["host_job_runs"]["has_fixture"] is True


def test_route_chart_returns_iframe_safe_html(monkeypatch):
    monkeypatch.setenv("SANKEY_EXPLORER_MODE", "fixture")
    handler = _FakeHandler()
    handled = routes.handle_get(
        handler,
        urlparse("/api/plugins/sankey-explorer/chart?table=host_job_runs&dims=job_name,host,status"),
    )
    assert handled is True
    assert handler.status == 200
    assert "text/html" in (handler.header("Content-Type") or "")
    # Embeddable: sandbox CSP set, and NO X-Frame-Options DENY.
    assert "sandbox" in (handler.header("Content-Security-Policy") or "")
    assert handler.header("X-Frame-Options") is None
    assert handler.body_text().lstrip().lower().startswith("<!doctype html>")


def test_route_chart_bad_request_is_400():
    handler = _FakeHandler()
    handled = routes.handle_get(
        handler, urlparse("/api/plugins/sankey-explorer/chart?table=host_job_runs"))
    assert handled is True
    assert handler.status == 400


def test_route_chart_unknown_table_is_404(monkeypatch):
    monkeypatch.setenv("SANKEY_EXPLORER_MODE", "fixture")
    handler = _FakeHandler()
    handled = routes.handle_get(
        handler,
        urlparse("/api/plugins/sankey-explorer/chart?table=client_core&dims=a,b"))
    assert handled is True
    assert handler.status == 404
