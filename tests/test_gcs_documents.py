"""Hermetic coverage for the GCS documents serving scaffold (plan B8).

Document bodies live in Google Cloud Storage; ``api/gcs_documents.py`` resolves
a browser-openable URL for a document id. On the PC ``google-cloud-storage`` is
never installed and there is no gcloud auth, so the module degrades to the
public ``gcs_url`` already committed in the index (``signed: false``,
``mini_pending: true``). These tests assert that degrade path plus the pure
env/lookup/parse logic WITHOUT the SDK, the network, or the server fixture.

Mirrors tests/test_sankey_explorer.py + tests/test_dashboard_probe.py.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from api import gcs_documents as gd


def _install_fake_storage(monkeypatch, *, signed_return=None, client_raises=False):
    """Inject a fake ``google.cloud.storage`` so signing is fully hermetic.

    google-cloud-storage may or may not be installed; injecting a fake makes the
    lazy ``from google.cloud import storage`` deterministic and never touches
    real gcloud credentials or the network.
    """
    fake = types.ModuleType("google.cloud.storage")

    class _FakeBlob:
        def generate_signed_url(self, **kwargs):
            return signed_return

    class _FakeBucket:
        def blob(self, object_path):
            return _FakeBlob()

    class _FakeClient:
        def __init__(self, *a, **k):
            if client_raises:
                raise RuntimeError("no gcloud auth (simulated)")

        def bucket(self, name):
            return _FakeBucket()

    fake.Client = _FakeClient
    # `from google.cloud import storage` binds the *attribute* on the parent
    # namespace package, so override both the attribute and the sys.modules
    # entry to reliably shadow a real (installed) SDK.
    import importlib
    try:
        parent = importlib.import_module("google.cloud")
    except Exception:
        parent = types.ModuleType("google.cloud")
        monkeypatch.setitem(sys.modules, "google.cloud", parent)
    monkeypatch.setattr(parent, "storage", fake, raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake)
    return fake


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_index(tmp_path: Path, monkeypatch, documents) -> Path:
    """Point HERMES_SNAPSHOT_ROOT at tmp_path and write an index there."""
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    doc_dir = tmp_path / "gbauto-documents"
    doc_dir.mkdir(parents=True, exist_ok=True)
    path = doc_dir / "index.json"
    path.write_text(json.dumps({"documents": documents}), encoding="utf-8")
    # Bust the module-level cache so a fresh read happens.
    gd._INDEX_CACHE.clear()
    return path


_SAMPLE = [
    {
        "id": "doc-1",
        "name": "Q1 report.pdf",
        "gcs_url": "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf",
    },
    {
        "id": "doc-2",
        "name": "notes.md",
        "public_url": "https://storage.googleapis.com/gbauto-docs-prod/notes.md",
    },
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the bucket env unset and the cache cleared."""
    monkeypatch.delenv("HERMES_GCS_DOCUMENTS_BUCKET", raising=False)
    monkeypatch.delenv("HERMES_SNAPSHOT_ROOT", raising=False)
    gd._INDEX_CACHE.clear()
    yield
    gd._INDEX_CACHE.clear()


# ── documents_bucket() reads the env var ──────────────────────────────────────

def test_documents_bucket_absent_is_none():
    assert gd.documents_bucket() is None


def test_documents_bucket_empty_string_is_none(monkeypatch):
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "")
    assert gd.documents_bucket() is None


def test_documents_bucket_reads_configured_value(monkeypatch):
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    assert gd.documents_bucket() == "gbauto-docs-prod"


# ── index load / lookup shape ─────────────────────────────────────────────────

def test_load_index_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    assert gd.load_index() is None


def test_load_index_reads_and_caches(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    data = gd.load_index()
    assert isinstance(data, dict)
    assert len(data["documents"]) == 2
    # A second read with an unchanged (size, mtime) signature is served from the
    # cache -- the exact same object is returned, not a re-parsed copy.
    assert gd.load_index() is data


def test_load_index_non_dict_json_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    doc_dir = tmp_path / "gbauto-documents"
    doc_dir.mkdir(parents=True)
    (doc_dir / "index.json").write_text("[1, 2, 3]", encoding="utf-8")
    gd._INDEX_CACHE.clear()
    assert gd.load_index() is None


def test_load_index_bad_json_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    doc_dir = tmp_path / "gbauto-documents"
    doc_dir.mkdir(parents=True)
    (doc_dir / "index.json").write_text("{not json", encoding="utf-8")
    gd._INDEX_CACHE.clear()
    assert gd.load_index() is None


def test_documents_tolerates_missing_or_bad_list():
    assert gd._documents(None) == []
    assert gd._documents({}) == []
    assert gd._documents({"documents": "nope"}) == []
    assert gd._documents({"documents": [{"id": "x"}]}) == [{"id": "x"}]


def test_find_document_hit_and_miss(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    assert gd.find_document("doc-1")["name"] == "Q1 report.pdf"
    assert gd.find_document("doc-2")["name"] == "notes.md"
    assert gd.find_document("nope") is None


def test_find_document_none_index(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SNAPSHOT_ROOT", str(tmp_path))
    assert gd.find_document("doc-1") is None


# ── _gcs_available() — degrade path when the SDK cannot import ─────────────────

def test_gcs_available_returns_bool():
    assert isinstance(gd._gcs_available(), bool)


def test_gcs_available_false_when_import_fails(monkeypatch):
    # Setting the submodule to None in sys.modules makes ``import`` raise,
    # which the bare except maps to the degrade (False) path.
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    assert gd._gcs_available() is False


# ── _object_path_from_gcs_url() parsing ───────────────────────────────────────

def test_object_path_extracted_from_public_url():
    url = "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf"
    assert gd._object_path_from_gcs_url(url, "gbauto-docs-prod") == "reports/q1.pdf"


def test_object_path_missing_bucket_marker_is_none():
    url = "https://storage.googleapis.com/other-bucket/reports/q1.pdf"
    assert gd._object_path_from_gcs_url(url, "gbauto-docs-prod") is None


def test_object_path_trailing_bucket_only_is_none():
    url = "https://storage.googleapis.com/gbauto-docs-prod/"
    assert gd._object_path_from_gcs_url(url, "gbauto-docs-prod") is None


# ── signed_url() is guarded (always None on the PC) ───────────────────────────

def test_signed_url_none_without_bucket():
    assert gd.signed_url("reports/q1.pdf") is None


def test_signed_url_none_without_object_path(monkeypatch):
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    assert gd.signed_url("") is None


def test_signed_url_none_when_sdk_absent(monkeypatch):
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    # Force the "no SDK" degrade regardless of what is installed on the runner.
    monkeypatch.setattr(gd, "_gcs_available", lambda: False)
    assert gd.signed_url("reports/q1.pdf") is None


def test_signed_url_none_when_client_raises(monkeypatch):
    """A gcloud-auth/client failure degrades to None (no crash)."""
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    monkeypatch.setattr(gd, "_gcs_available", lambda: True)
    _install_fake_storage(monkeypatch, client_raises=True)
    assert gd.signed_url("reports/q1.pdf") is None


def test_signed_url_mints_when_sdk_and_bucket_present(monkeypatch):
    """Mini path: SDK present + bucket configured -> a V4 signed URL string."""
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    monkeypatch.setattr(gd, "_gcs_available", lambda: True)
    signed = "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf?X-Goog-Signature=xyz"
    _install_fake_storage(monkeypatch, signed_return=signed)
    assert gd.signed_url("reports/q1.pdf") == signed


# ── resolve_document_url() degrade path (the headline behaviour) ──────────────

def test_resolve_unknown_id_is_none(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    assert gd.resolve_document_url("nope") is None


def test_resolve_degrades_to_public_url_without_sdk_no_bucket(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    res = gd.resolve_document_url("doc-1")
    assert res == {
        "id": "doc-1",
        "name": "Q1 report.pdf",
        "url": "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf",
        "signed": False,
        "bucket": None,
        "mini_pending": True,
    }


def test_resolve_degrades_with_bucket_set_but_sdk_absent(tmp_path, monkeypatch):
    """Bucket configured, but no SDK/gcloud on the PC -> still public + pending."""
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    res = gd.resolve_document_url("doc-1")
    assert res["signed"] is False
    assert res["mini_pending"] is True
    assert res["bucket"] == "gbauto-docs-prod"
    assert res["url"] == "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf"


def test_resolve_uses_public_url_fallback_field(tmp_path, monkeypatch):
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    res = gd.resolve_document_url("doc-2")
    assert res["url"] == "https://storage.googleapis.com/gbauto-docs-prod/notes.md"
    assert res["signed"] is False
    assert res["mini_pending"] is True


def test_resolve_signed_path_when_signing_succeeds(tmp_path, monkeypatch):
    """Mini path: bucket set, object resolvable, signing yields a URL."""
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    _write_index(tmp_path, monkeypatch, _SAMPLE)
    signed = "https://storage.googleapis.com/gbauto-docs-prod/reports/q1.pdf?X-Goog-Signature=abc"
    monkeypatch.setattr(gd, "signed_url", lambda obj, **kw: signed)
    res = gd.resolve_document_url("doc-1")
    assert res["signed"] is True
    assert res["mini_pending"] is False
    assert res["url"] == signed
    assert res["bucket"] == "gbauto-docs-prod"


def test_resolve_falls_back_to_gcs_object_field(tmp_path, monkeypatch):
    """When the public url lacks the bucket marker, gcs_object is used to sign."""
    monkeypatch.setenv("HERMES_GCS_DOCUMENTS_BUCKET", "gbauto-docs-prod")
    docs = [{
        "id": "doc-3",
        "name": "weird.pdf",
        "gcs_url": "https://cdn.example.test/weird.pdf",  # no bucket marker
        "gcs_object": "explicit/weird.pdf",
    }]
    _write_index(tmp_path, monkeypatch, docs)
    seen = {}

    def fake_signed(obj, **kw):
        seen["obj"] = obj
        return "https://signed/weird.pdf?sig=1"

    monkeypatch.setattr(gd, "signed_url", fake_signed)
    res = gd.resolve_document_url("doc-3")
    assert seen["obj"] == "explicit/weird.pdf"
    assert res["signed"] is True
    assert res["url"] == "https://signed/weird.pdf?sig=1"


def test_resolve_missing_url_degrades_to_empty_string(tmp_path, monkeypatch):
    docs = [{"id": "doc-4", "name": "no-url.pdf"}]
    _write_index(tmp_path, monkeypatch, docs)
    res = gd.resolve_document_url("doc-4")
    assert res["url"] == ""
    assert res["signed"] is False
    assert res["mini_pending"] is True
