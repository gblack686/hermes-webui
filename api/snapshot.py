"""
GBAutomation static-snapshot serving (migration plan B0a).

webui serves none of the gbauto JSON snapshot prefixes that the 9119 dashboard
exposed through its bundled ``web_dist`` (it served them via the SPA catch-all
out of ``HERMES_WEB_DIST``). This module clones ``routes._serve_static`` into a
sandboxed :func:`serve_snapshot` rooted at a configurable ``HERMES_SNAPSHOT_ROOT``
(default ``<repo>/static/gbauto/``), mirroring 9119's ``HERMES_WEB_DIST``
override. ``handle_get`` dispatches to it via a prefix allowlist *before* the
final 404, so the gbauto tabs (B5-B9) can fetch their manifests.

Served prefixes (see :data:`SNAPSHOT_PREFIXES`):
    /gbauto-supabase/    Supabase index snapshot + contracts (B9)
    /gbauto-lineage/     PRD lineage snapshot (B7)
    /gbauto-documents/   INDEX ONLY -- index.json. Document bodies (6.2 GB)
                         stream from Google Cloud Storage in B8, never here.
    /repos/              TAC repo catalog manifest (B6)
    /prds/               PRD manifest (B5)
    /observability/      observability rollup snapshot

Data contract / PII safety
--------------------------
Every snapshot served here is AGGREGATE-ONLY and PII-safe, produced with 9119's
``TENANT_CLIENT_ALIASES`` / ``client_slug`` tenant scoping applied AT GENERATION
TIME (the consuming gbauto tabs preserve that scoping). This serving layer does
no per-request tenant filtering -- it only hands back pre-scoped, pre-redacted
JSON that was already narrowed to the active tenant when written.

Generator repointing (Mini)
---------------------------
Snapshot freshness is NOT essential per the plan: ship committed/periodic
snapshots, no new freshness crons. To get live/fresh data the gbauto snapshot
generators must be repointed to WRITE into ``HERMES_SNAPSHOT_ROOT`` on the Mini:

    - ``nightly_artifact_diff.py``          -> /observability/, /gbauto-documents/index.json
    - prd-lineage producer (``export_prd_lineage.mjs``) -> /gbauto-lineage/, /prds/
    - the gbauto-supabase snapshot writer   -> /gbauto-supabase/
    - the gbauto-github-index generator     -> /repos/

Supabase is DNS-blocked from the PC, so live regeneration is Mini-only. The
sample fixtures committed under ``static/gbauto/`` let PC smoke prove serving
offline (curl returns the JSON).
"""

from __future__ import annotations

import gzip
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs

# Only these GET path prefixes are served from the snapshot root. Everything
# else falls through to the normal 404 / SPA handling.
SNAPSHOT_PREFIXES = (
    "/gbauto-supabase/",
    "/gbauto-lineage/",
    "/gbauto-documents/",
    "/repos/",
    "/prds/",
    "/observability/",
    # Profiles Catalog (B12): agent-profile catalog fixture + profile art /
    # report bodies, migrated from 9119's web_dist static serving.
    "/agent-profiles/",
    "/profile-art/",
    "/profile-reports/",
)

# /gbauto-documents/ is index-only: document bodies live in GCS (B8), not here.
# Only this exact path under the prefix is allowed to be served.
_DOCUMENTS_PREFIX = "/gbauto-documents/"
_DOCUMENTS_ALLOWED = ("/gbauto-documents/index.json",)

# Snapshots are JSON; keep a small local MIME map so the module is self-contained.
_SNAPSHOT_MIME = {
    "json": "application/json",
    "csv": "text/csv",
    "txt": "text/plain",
    # Profile art / report bodies (B12).
    "html": "text/html",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "svg": "image/svg+xml",
    "webp": "image/webp",
}
_TEXT_MIME = {"application/json", "text/csv", "text/plain", "text/html", "image/svg+xml"}
_COMPRESSIBLE_MIME = {"application/json", "text/csv", "text/plain", "text/html", "image/svg+xml"}

# Per-file cache (raw, gzip, etag) keyed by absolute path, invalidated by
# (size, nanosecond mtime) -- same shape as routes._serve_static.
_SNAPSHOT_CACHE: dict = {}
_SNAPSHOT_CACHE_LOCK = threading.Lock()


def snapshot_root() -> Path:
    """Resolve the snapshot root, honouring ``HERMES_SNAPSHOT_ROOT``."""
    env = os.environ.get("HERMES_SNAPSHOT_ROOT")
    if env:
        return Path(env).resolve()
    return (Path(__file__).parent.parent / "static" / "gbauto").resolve()


def is_snapshot_path(path: str) -> bool:
    """True if ``path`` is one of the gbauto snapshot prefixes."""
    return any(path.startswith(p) for p in SNAPSHOT_PREFIXES)


def serve_snapshot(handler, parsed):
    """Serve a committed gbauto JSON snapshot from :func:`snapshot_root`.

    Sandboxed clone of ``routes._serve_static``: resolves the path under the
    snapshot root, rejects traversal, and serves with ETag/gzip/Cache-Control.
    """
    from api.routes import j  # local import avoids a circular import at load

    # gbauto-documents is index-only; document bodies come from GCS (B8).
    if parsed.path.startswith(_DOCUMENTS_PREFIX) and parsed.path not in _DOCUMENTS_ALLOWED:
        return j(
            handler,
            {"error": "document bodies are served from Google Cloud Storage (B8); only index.json is local"},
            status=404,
        )

    root = snapshot_root()
    rel = parsed.path.lstrip("/")
    snap_file = (root / rel).resolve()
    try:
        snap_file.relative_to(root)
    except ValueError:
        return j(handler, {"error": "not found"}, status=404)
    if not snap_file.exists() or not snap_file.is_file():
        return j(handler, {"error": "not found"}, status=404)

    ext = snap_file.suffix.lower().lstrip(".")
    ct = _SNAPSHOT_MIME.get(ext, "application/octet-stream")
    ct_header = f"{ct}; charset=utf-8" if ct in _TEXT_MIME else ct

    st = snap_file.stat()
    sig = (st.st_size, st.st_mtime_ns)
    cache_key = str(snap_file)
    raw = gz = etag = None
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == sig:
            _, raw, gz, etag = cached
    if raw is None:
        raw = snap_file.read_bytes()
        etag = f'W/"{sig[0]:x}-{sig[1]:x}"'
        gz = (
            gzip.compress(raw, compresslevel=6)
            if ct in _COMPRESSIBLE_MIME and len(raw) > 1024
            else None
        )
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_CACHE[cache_key] = (sig, raw, gz, etag)

    # Snapshots are not fingerprinted; keep them briefly fresh so a repointed
    # generator's new write is picked up without a hard reload.
    version_values = parse_qs(parsed.query, keep_blank_values=True).get("v", [""])
    has_fingerprint = bool(version_values[0])
    cache_control = (
        "public, max-age=31536000, immutable" if has_fingerprint
        else "public, max-age=60"
    )

    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache_control)
        if gz is not None:
            handler.send_header("Vary", "Accept-Encoding")
        handler.end_headers()
        return True

    accept_enc = (handler.headers.get("Accept-Encoding") or "").lower()
    use_gzip = gz is not None and "gzip" in accept_enc
    body = gz if use_gzip else raw

    handler.send_response(200)
    handler.send_header("Content-Type", ct_header)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache_control)
    if gz is not None:
        handler.send_header("Vary", "Accept-Encoding")
    if use_gzip:
        handler.send_header("Content-Encoding", "gzip")
    handler.end_headers()
    handler.wfile.write(body)
    return True
