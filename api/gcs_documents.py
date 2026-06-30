"""
GBAutomation documents — Google Cloud Storage serving scaffold (plan B8).

Document BODIES (6.2 GB / 10.6k files) live in Google Cloud Storage, never in
the repo or on a shared volume. webui serves only a small ``index.json``
natively (via the B0a snapshot route, ``/gbauto-documents/index.json``); each
index entry carries a ``gcs_url`` pointing at the object in the bucket.

This module is the *serving* scaffold: given a document id it resolves a URL the
browser can open. When ``google-cloud-storage`` is installed AND a bucket is
configured (``HERMES_GCS_DOCUMENTS_BUCKET``) it mints a short-lived V4 signed
URL; otherwise it degrades to the public ``gcs_url`` already committed in the
index. The signed-URL path needs gcloud credentials and is MINI-ONLY (the PC
has no gcloud auth — the documented reauth gotcha), so on the PC the route
returns the public URL with ``signed: false`` and ``mini_pending: true``.

PII safety: the index this reads is aggregate-only / pre-scoped at generation
time. This module touches only that index + the bucket env var; never client
PII. ``google-cloud-storage`` is imported lazily so importing this module (and
running PC smoke) never requires the SDK or gcloud to be installed.

mini_pending (live path, runs on the Mini only):
    - ``build_gbauto_documents_index.py`` uploads bodies to the bucket and emits
      this index into ``HERMES_SNAPSHOT_ROOT/gbauto-documents/index.json``.
    - signed-URL minting needs ``gcloud auth`` on the Mini (reauth gotcha).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# Default signed-URL lifetime (seconds). Short-lived so a copied URL expires.
_SIGNED_URL_TTL_SECONDS = 900

# (size, mtime_ns) -> parsed index, so repeated resolves don't re-read the file.
_INDEX_CACHE: dict = {}
_INDEX_LOCK = threading.Lock()


def documents_bucket() -> str | None:
    """The configured GCS bucket holding document bodies, or None on the PC."""
    return os.environ.get("HERMES_GCS_DOCUMENTS_BUCKET") or None


def _index_path() -> Path:
    """Resolve the documents index under the B0a snapshot root."""
    from api.snapshot import snapshot_root

    return snapshot_root() / "gbauto-documents" / "index.json"


def load_index() -> dict | None:
    """Load + cache the committed/Generated documents index. None if absent."""
    path = _index_path()
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    key = str(path)
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached and cached[0] == sig:
            return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = (sig, data)
    return data


def _documents(data: dict | None) -> list:
    if not isinstance(data, dict):
        return []
    docs = data.get("documents")
    return docs if isinstance(docs, list) else []


def find_document(doc_id: str) -> dict | None:
    """Return the index entry for ``doc_id`` (or None)."""
    for doc in _documents(load_index()):
        if isinstance(doc, dict) and str(doc.get("id")) == str(doc_id):
            return doc
    return None


def _gcs_available() -> bool:
    """True only when the GCS SDK imports (never installed on the PC)."""
    try:
        import google.cloud.storage  # noqa: F401

        return True
    except Exception:
        return False


def _object_path_from_gcs_url(gcs_url: str, bucket: str) -> str | None:
    """Extract the object path from ``https://storage.googleapis.com/<bucket>/<object>``."""
    marker = f"/{bucket}/"
    idx = gcs_url.find(marker)
    if idx == -1:
        return None
    return gcs_url[idx + len(marker):] or None


def signed_url(object_path: str, *, ttl_seconds: int = _SIGNED_URL_TTL_SECONDS) -> str | None:
    """Mint a V4 signed GET URL for ``object_path``.

    Returns None when no bucket is configured or the GCS SDK / gcloud auth is
    unavailable (i.e. always None on the PC). Live signing is Mini-only.
    """
    bucket = documents_bucket()
    if not bucket or not object_path or not _gcs_available():
        return None
    try:
        from datetime import timedelta

        from google.cloud import storage  # lazy: never required for import/smoke

        client = storage.Client()
        blob = client.bucket(bucket).blob(object_path)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds),
            method="GET",
        )
    except Exception:
        return None


def resolve_document_url(doc_id: str) -> dict | None:
    """Resolve a browser-openable URL for a document id.

    Returns ``{id, name, url, signed, bucket, mini_pending}`` or None when the
    id is unknown. ``signed`` is True only when a GCS signed URL was actually
    minted (Mini, gcloud authed); otherwise it degrades to the public
    ``gcs_url`` from the committed index and ``mini_pending`` is True.
    """
    doc = find_document(doc_id)
    if not doc:
        return None
    public_url = doc.get("gcs_url") or doc.get("public_url") or ""
    bucket = documents_bucket()
    signed = None
    if bucket and public_url:
        obj = _object_path_from_gcs_url(public_url, bucket) or doc.get("gcs_object")
        if obj:
            signed = signed_url(obj)
    url = signed or public_url
    return {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "url": url,
        "signed": bool(signed),
        "bucket": bucket,
        "mini_pending": not bool(signed),
    }
