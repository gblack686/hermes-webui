"""GBauto native data routes — artifacts/documents index (Phase 5).

Server-backed replacement for the 9119 DocumentsPage data source. There is NO
browser Supabase read and NO 8791 sidecar: the WebUI backend owns the index.

Resolution order (all bounded, all degrade with explicit staleness metadata):

  1. A pre-generated JSON index (env HERMES_GBAUTO_ARTIFACTS_INDEX, or the
     conventional ``gbauto-documents/index.json`` under the artifacts dir). This
     mirrors the nightly-diff bundle the 9119 dashboard consumed, but served by
     8787 instead of shipped as a static browser bundle.
  2. A filesystem scan of HERMES_GBAUTO_ARTIFACTS_DIR for report files
     (*.html, *.pdf, *.md) when no index file exists.
  3. An empty, explicitly-stale payload when nothing is configured, so the UI
     renders a clear "no artifacts source configured" state rather than erroring.

Wired into api/routes.py via the ``/api/gbauto/`` delegating block; auth is the
central check_auth gate (this path is not in PUBLIC_PATHS), so it is
login-gated automatically.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from api.helpers import j, bad

# Bounds: never return an unbounded list to the browser.
_MAX_ARTIFACTS = 500
_MAX_SCAN_FILES = 4000
_SCAN_EXTS = {".html": "report", ".pdf": "pdf", ".md": "markdown"}
# Consider an index file "stale" once it is older than this many seconds.
_STALE_AFTER_SECONDS = 26 * 60 * 60  # ~26h, so a nightly job that skipped one day flags stale


def _artifacts_dir() -> Path | None:
    raw = os.environ.get("HERMES_GBAUTO_ARTIFACTS_DIR", "").strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
    except (OSError, ValueError):
        return None
    return p if p.is_dir() else None


def _index_file() -> Path | None:
    raw = os.environ.get("HERMES_GBAUTO_ARTIFACTS_INDEX", "").strip()
    if raw:
        try:
            p = Path(raw).expanduser()
        except (OSError, ValueError):
            p = None
        if p and p.is_file():
            return p
    d = _artifacts_dir()
    if d:
        cand = d / "gbauto-documents" / "index.json"
        if cand.is_file():
            return cand
        cand = d / "index.json"
        if cand.is_file():
            return cand
    return None


def _iso(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    except (OSError, ValueError, OverflowError):
        return ""


def _sanitize_artifact(a: dict) -> dict | None:
    """Keep only known, bounded, string/number fields; drop anything else."""
    if not isinstance(a, dict):
        return None
    out: dict = {}
    for k in (
        "id", "title", "description", "docType", "extension", "group",
        "taxonomy", "publicPath", "previewPath", "sourcePath",
        "generatedAt", "modifiedAt",
    ):
        v = a.get(k)
        if isinstance(v, str):
            out[k] = v[:2000]
    sb = a.get("sizeBytes")
    if isinstance(sb, (int, float)):
        out["sizeBytes"] = int(sb)
    if not out.get("id"):
        out["id"] = out.get("sourcePath") or out.get("title") or ""
    if not out.get("id"):
        return None
    return out


def _from_index_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("index.json is not an object")
    raw_arts = data.get("artifacts")
    if not isinstance(raw_arts, list):
        raise ValueError("index.json missing 'artifacts' list")
    arts = []
    for a in raw_arts[:_MAX_ARTIFACTS]:
        s = _sanitize_artifact(a)
        if s:
            arts.append(s)
    recently = data.get("recentlyAdded")
    recently = [x for x in recently if isinstance(x, str)][:_MAX_ARTIFACTS] if isinstance(recently, list) else []
    generated = data.get("generatedAt")
    generated = generated if isinstance(generated, str) else _iso(path.stat().st_mtime)

    age = time.time() - path.stat().st_mtime
    stale_reason = None
    if age > _STALE_AFTER_SECONDS:
        hours = int(age // 3600)
        stale_reason = f"index {hours}h old"
    truncated = len(raw_arts) > _MAX_ARTIFACTS
    if truncated:
        stale_reason = (stale_reason + "; " if stale_reason else "") + f"showing first {_MAX_ARTIFACTS} of {len(raw_arts)}"
    return {
        "artifacts": arts,
        "recentlyAdded": recently,
        "generatedAt": generated,
        "source": "index",
        "stale_reason": stale_reason,
    }


def _from_scan(directory: Path) -> dict:
    arts = []
    scanned = 0
    for root, dirs, files in os.walk(directory):
        # Skip hidden / vcs / heavy dirs to keep the scan bounded.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for name in files:
            scanned += 1
            if scanned > _MAX_SCAN_FILES or len(arts) >= _MAX_ARTIFACTS:
                break
            ext = os.path.splitext(name)[1].lower()
            doctype = _SCAN_EXTS.get(ext)
            if not doctype:
                continue
            full = Path(root) / name
            try:
                st = full.stat()
            except OSError:
                continue
            rel = str(full.relative_to(directory)).replace("\\", "/")
            arts.append({
                "id": rel,
                "title": os.path.splitext(name)[0].replace("-", " ").replace("_", " "),
                "docType": doctype,
                "extension": ext.lstrip("."),
                "group": os.path.dirname(rel) or "root",
                "sourcePath": rel,
                "sizeBytes": int(st.st_size),
                "modifiedAt": _iso(st.st_mtime),
                "generatedAt": _iso(st.st_mtime),
            })
        if scanned > _MAX_SCAN_FILES or len(arts) >= _MAX_ARTIFACTS:
            break
    arts.sort(key=lambda a: a.get("modifiedAt", ""), reverse=True)
    return {
        "artifacts": arts,
        "recentlyAdded": [a["id"] for a in arts[:8]],
        "generatedAt": _iso(time.time()),
        "source": "scan",
        "stale_reason": (f"scan capped at {_MAX_ARTIFACTS}" if len(arts) >= _MAX_ARTIFACTS else None),
    }


def _artifacts_payload() -> dict:
    idx = _index_file()
    if idx:
        try:
            return _from_index_file(idx)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "artifacts": [], "recentlyAdded": [], "generatedAt": None,
                "source": "index", "stale_reason": f"index unreadable: {exc}",
            }
    d = _artifacts_dir()
    if d:
        try:
            return _from_scan(d)
        except OSError as exc:
            return {
                "artifacts": [], "recentlyAdded": [], "generatedAt": None,
                "source": "scan", "stale_reason": f"scan failed: {exc}",
            }
    return {
        "artifacts": [], "recentlyAdded": [], "generatedAt": None,
        "source": "none",
        "stale_reason": "no artifacts source configured (set HERMES_GBAUTO_ARTIFACTS_DIR or HERMES_GBAUTO_ARTIFACTS_INDEX)",
    }


def handle_gbauto_get(handler, parsed):
    """GET dispatch for /api/gbauto/*.

    Three-valued contract (matches api/kanban_bridge.py):
      True  -> matched + responded
      None  -> matched, response already sent by j()/bad()
      False -> no path matched (caller emits 404)
    """
    path = parsed.path
    try:
        if path == "/api/gbauto/health":
            return j(handler, {"ok": True, "service": "gbauto"}) or True
        if path == "/api/gbauto/artifacts":
            return j(handler, _artifacts_payload()) or True
    except ImportError as exc:
        return bad(handler, f"feature unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc), status=400)
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False
