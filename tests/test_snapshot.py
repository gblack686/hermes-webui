"""Hermetic coverage for the gbauto static-snapshot serving module (plan B0a).

``api/snapshot.py`` clones ``routes._serve_static`` into a sandboxed
:func:`serve_snapshot` rooted at a configurable ``HERMES_SNAPSHOT_ROOT``. These
tests exercise the pure logic (env resolution, prefix allowlist,
``is_snapshot_path``) and the serving path (fixture serve from a tmp root, 404
on traversal / missing files, gbauto-documents index-only gating, ETag/304 and
gzip negotiation) without the session ``test_server`` fixture, the network, or
real Supabase/GCS.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from api import snapshot


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Neutralise conftest's session server fixture.

    These are pure unit tests -- they never touch the HTTP server, network,
    Supabase or GCS. Overriding the autouse ``test_server`` fixture here keeps
    the file hermetic (and avoids the Windows symlink privilege the real
    fixture needs to mirror ~/.hermes/skills).
    """
    yield


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for serve_snapshot / j."""

    def __init__(self, request_headers=None):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        # serve_snapshot + helpers.j read handler.headers.get(...).
        self.headers = dict(request_headers or {})

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for k, v in self.sent_headers:
            if k.lower() == name.lower():
                return v
        return None

    def raw_body(self):
        return bytes(self.body)

    def json_body(self):
        raw = bytes(self.body)
        if self.header("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def _parsed(url):
    return urlparse(url)


# ── snapshot_root() env resolution ────────────────────────────────────────────

def test_snapshot_root_defaults_to_repo_static_gbauto(monkeypatch):
    monkeypatch.delenv("HERMES_SNAPSHOT_ROOT", raising=False)
    root = snapshot.snapshot_root()
    assert root == (ROOT / "static" / "gbauto").resolve()
    assert root.is_absolute()


def test_snapshot_root_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    assert snapshot.snapshot_root() == tmp_path.resolve()


def test_snapshot_root_env_is_resolved_absolute(monkeypatch, tmp_path):
    # A relative-ish path with a redundant segment is normalised via resolve().
    target = tmp_path / "snap"
    target.mkdir()
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path / "snap" / "." ))
    assert snapshot.snapshot_root() == target.resolve()


def test_snapshot_root_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", "")
    assert snapshot.snapshot_root() == (ROOT / "static" / "gbauto").resolve()


# ── SNAPSHOT_PREFIXES allowlist + is_snapshot_path() ──────────────────────────

def test_snapshot_prefixes_are_the_expected_allowlist():
    assert snapshot.SNAPSHOT_PREFIXES == (
        "/gbauto-supabase/",
        "/gbauto-lineage/",
        "/gbauto-documents/",
        "/repos/",
        "/prds/",
        "/observability/",
        "/agent-profiles/",
        "/profile-art/",
        "/profile-reports/",
    )
    # Every prefix is anchored and slash-delimited (no accidental substrings).
    assert all(p.startswith("/") and p.endswith("/") for p in snapshot.SNAPSHOT_PREFIXES)


@pytest.mark.parametrize(
    "path",
    [
        "/gbauto-supabase/snapshot.json",
        "/gbauto-lineage/index.json",
        "/gbauto-documents/index.json",
        "/repos/manifest.json",
        "/prds/prds-manifest.json",
        "/observability/rollup.json",
        "/agent-profiles/catalog.json",
        "/profile-art/x.png",
        "/profile-reports/x.html",
    ],
)
def test_is_snapshot_path_true_for_allowlisted(path):
    assert snapshot.is_snapshot_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/status",
        "/index.html",
        "/gbauto/other.json",
        "/gbauto-supabaseX/snapshot.json",  # prefix must include trailing slash
        "/notprds/manifest.json",
        "",
    ],
)
def test_is_snapshot_path_false_for_non_allowlisted(path):
    assert snapshot.is_snapshot_path(path) is False


# ── serve_snapshot: fixture serve from a tmp root ─────────────────────────────

def _seed(tmp_path, rel, data):
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def test_serve_snapshot_serves_json_from_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    payload = {"ok": True, "rows": [1, 2, 3]}
    _seed(tmp_path, "gbauto-supabase/snapshot.json", payload)

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(handler, _parsed("/gbauto-supabase/snapshot.json"))

    assert result is True
    assert handler.status == 200
    assert handler.header("Content-Type") == "application/json; charset=utf-8"
    assert handler.header("ETag")
    assert handler.header("Cache-Control") == "public, max-age=60"
    assert json.loads(handler.raw_body().decode("utf-8")) == payload


def test_serve_snapshot_fingerprinted_query_gets_immutable_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    _seed(tmp_path, "prds/prds-manifest.json", {"prds": []})

    handler = _FakeHandler()
    snapshot.serve_snapshot(handler, _parsed("/prds/prds-manifest.json?v=abc123"))

    assert handler.status == 200
    assert handler.header("Cache-Control") == "public, max-age=31536000, immutable"


def test_serve_snapshot_gzip_when_accepted_and_large(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    big = {"rows": ["x" * 50] * 100}  # comfortably > 1024 bytes of JSON
    f = _seed(tmp_path, "observability/rollup.json", big)
    assert f.stat().st_size > 1024

    handler = _FakeHandler(request_headers={"Accept-Encoding": "gzip"})
    snapshot.serve_snapshot(handler, _parsed("/observability/rollup.json"))

    assert handler.status == 200
    assert handler.header("Content-Encoding") == "gzip"
    assert handler.header("Vary") == "Accept-Encoding"
    assert json.loads(gzip.decompress(handler.raw_body()).decode("utf-8")) == big


def test_serve_snapshot_no_gzip_without_accept_encoding(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    big = {"rows": ["x" * 50] * 100}
    _seed(tmp_path, "observability/rollup.json", big)

    handler = _FakeHandler()  # no Accept-Encoding
    snapshot.serve_snapshot(handler, _parsed("/observability/rollup.json"))

    assert handler.status == 200
    assert handler.header("Content-Encoding") is None


def test_serve_snapshot_304_on_matching_if_none_match(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    _seed(tmp_path, "repos/manifest.json", {"repos": []})

    first = _FakeHandler()
    snapshot.serve_snapshot(first, _parsed("/repos/manifest.json"))
    etag = first.header("ETag")
    assert etag

    second = _FakeHandler(request_headers={"If-None-Match": etag})
    result = snapshot.serve_snapshot(second, _parsed("/repos/manifest.json"))
    assert result is True
    assert second.status == 304
    assert second.raw_body() == b""


# ── serve_snapshot: safety / 404 paths ────────────────────────────────────────

def test_serve_snapshot_404_for_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    handler = _FakeHandler()
    # 404 branches return j(...) which is None (only the 200 path returns True).
    result = snapshot.serve_snapshot(handler, _parsed("/repos/does-not-exist.json"))
    assert result is None
    assert handler.status == 404
    assert handler.json_body() == {"error": "not found"}


def test_serve_snapshot_404_on_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    # A secret living OUTSIDE the snapshot root.
    (tmp_path / "secret.json").write_text('{"leak": true}', encoding="utf-8")

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(
        handler, _parsed("/gbauto-supabase/../../secret.json")
    )
    assert result is None
    assert handler.status == 404
    assert handler.json_body() == {"error": "not found"}


def test_serve_snapshot_404_when_path_is_a_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    (tmp_path / "repos").mkdir()
    handler = _FakeHandler()
    result = snapshot.serve_snapshot(handler, _parsed("/repos/"))
    assert result is None
    assert handler.status == 404


# ── gbauto-documents: index-only gating (bodies live in GCS, B8) ──────────────

def test_serve_snapshot_documents_index_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    _seed(tmp_path, "gbauto-documents/index.json", {"docs": []})

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(handler, _parsed("/gbauto-documents/index.json"))
    assert result is True
    assert handler.status == 200
    assert handler.json_body() == {"docs": []}


def test_serve_snapshot_documents_body_is_404_gcs_message(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    # Even if a body file exists locally, only index.json is servable.
    _seed(tmp_path, "gbauto-documents/some-doc.json", {"body": "secret"})

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(
        handler, _parsed("/gbauto-documents/some-doc.json")
    )
    assert result is None
    assert handler.status == 404
    assert "Google Cloud Storage" in handler.json_body()["error"]


def test_documents_allowlist_constants():
    assert snapshot._DOCUMENTS_PREFIX == "/gbauto-documents/"
    assert snapshot._DOCUMENTS_ALLOWED == ("/gbauto-documents/index.json",)


# ── MIME resolution + octet-stream fallback ───────────────────────────────────

def test_serve_snapshot_unknown_extension_is_octet_stream(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    f = tmp_path / "profile-art" / "blob.bin"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x00\x01\x02")

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(handler, _parsed("/profile-art/blob.bin"))
    assert result is True
    assert handler.status == 200
    assert handler.header("Content-Type") == "application/octet-stream"
    assert handler.raw_body() == b"\x00\x01\x02"


def test_serve_snapshot_html_report_body(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    f = tmp_path / "profile-reports" / "r.html"
    f.parent.mkdir(parents=True)
    f.write_text("<!doctype html><h1>hi</h1>", encoding="utf-8")

    handler = _FakeHandler()
    result = snapshot.serve_snapshot(handler, _parsed("/profile-reports/r.html"))
    assert result is True
    assert handler.status == 200
    assert handler.header("Content-Type") == "text/html; charset=utf-8"
