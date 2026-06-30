"""
Plugin discovery and static serving for Hermes Web UI.

Scans ~/.hermes/plugins/<name>/dashboard/ for manifest.json files,
matching the official Hermes dashboard plugin format.

Each plugin may have:
  dashboard/
    manifest.json   -- tab definition and entry point
    dist/
      index.js      -- plugin JS bundle (IIFE)
      style.css     -- optional plugin stylesheet
    plugin_api.py   -- optional backend API (not used in WebUI MVP)
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid dashboard-plugin name: a safe slug (it becomes a URL path component and
# a settings key). Lowercase alnum + - / _, 1-64 chars, must start with a letter.
_VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Valid tab.path: a clean same-origin absolute path. Must start with a single
# '/' (NOT '//' — a leading '//' is a protocol-relative URL that would resolve
# to a remote origin when assigned to iframe.src), then only safe path chars —
# no quotes, whitespace, control chars, query ('?') or fragment ('#').
_VALID_PLUGIN_TAB_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]{0,255}$")

# plugin_name -> manifest dict (as loaded from manifest.json)
PLUGIN_MANIFESTS: dict[str, dict] = {}

# plugin_name -> resolved static root dir
_PLUGIN_STATIC_ROOTS: dict[str, Path] = {}


def _get_plugin_base() -> Path:
    return Path(os.environ.get("HERMES_WEBUI_PLUGINS_DIR", str(Path.home() / ".hermes" / "plugins")))


def load_plugins() -> None:
    """Scan plugin directories and load manifest.json for each dashboard plugin."""
    plugin_base = _get_plugin_base()
    if not plugin_base.is_dir():
        logger.debug("No plugins directory at %s", plugin_base)
        return

    for entry in sorted(plugin_base.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "dashboard" / "manifest.json"
        if not manifest_path.is_file():
            continue

        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            logger.exception("Failed to parse manifest for plugin %s", entry.name)
            continue

        name = manifest.get("name") or entry.name

        # Validate the plugin name: it becomes a URL path component
        # (/dashboard-plugins/<name>/...) and a settings key. Restrict to a safe
        # slug so a manifest like name:"../foo" can't make the URL-space ambiguous.
        if not _VALID_PLUGIN_NAME.match(str(name)):
            logger.warning("Skipping plugin with invalid name %r (must match %s)", name, _VALID_PLUGIN_NAME.pattern)
            continue

        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")

        # Validate tab.path: it's a same-origin route the plugin page is served
        # at AND a value passed into client-side navigation. Require a clean
        # absolute path — no quotes/control chars/query/fragment — so a hostile
        # manifest can't shadow odd routes or inject via the path.
        if not _VALID_PLUGIN_TAB_PATH.match(str(tab_path)):
            logger.warning("Skipping plugin %s with invalid tab.path %r (must match %s)", name, tab_path, _VALID_PLUGIN_TAB_PATH.pattern)
            continue

        if name in PLUGIN_MANIFESTS:
            logger.warning("Duplicate plugin name skipped: %s (already loaded)", name)
            continue
        if tab_path in (m.get("tab", {}).get("path") for m in PLUGIN_MANIFESTS.values()):
            logger.warning("Plugin %s tab.path %r conflicts with another plugin; skipped", name, tab_path)
            continue

        PLUGIN_MANIFESTS[name] = manifest
        logger.info("Loaded dashboard plugin: %s (label=%s)", name, manifest.get("label", ""))

        # Pre-compute static root for fast serving (points to dashboard/)
        dashboard_dir = entry / "dashboard"
        if dashboard_dir.is_dir():
            _PLUGIN_STATIC_ROOTS[name] = dashboard_dir.resolve()


def serve_plugin_static(plugin_name: str, rel_path: str) -> tuple[bytes, str] | None:
    """
    Serve a built static asset from a plugin's dashboard/dist/ (or static/) dir.

    Returns (file_bytes, content_type) on success, None on not found.

    Security: _PLUGIN_STATIC_ROOTS points at the plugin's whole dashboard/ dir
    (the page route needs that), but the asset route must NOT expose plugin
    source/config — e.g. dashboard/plugin_api.py, manifest.json, .env. So we
    constrain served files to the built-asset subtrees (dist/ or static/), reject
    dotfiles, and require a known static extension.
    """
    root = _PLUGIN_STATIC_ROOTS.get(plugin_name)
    if not root:
        return None

    safe = (root / rel_path.lstrip("/")).resolve()
    try:
        safe.relative_to(root)
    except ValueError:
        return None  # path traversal attempt

    # Only built-asset subtrees are servable (not the dashboard root itself,
    # which holds plugin_api.py / manifest.json / config).
    rel = safe.relative_to(root)
    if not rel.parts or rel.parts[0] not in ("dist", "static"):
        return None
    # No dotfiles (.env, .git, etc.) anywhere in the path.
    if any(part.startswith(".") for part in rel.parts):
        return None

    if not safe.is_file():
        return None

    # Allowlist of static asset extensions — refuse source/config (.py, .json,
    # .toml, .env, .sh, ...) even if somehow placed under dist/.
    ext = os.path.splitext(rel_path.lower())[1]
    _STATIC_EXTS = {
        ".js", ".css", ".html", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".map", ".txt",
    }
    if ext not in _STATIC_EXTS:
        return None

    data = safe.read_bytes()
    content_type = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")

    return data, content_type


def get_plugin_metadata() -> list[dict]:
    """
    Return a list of plugin metadata suitable for the Settings → Plugins tab.
    Each entry includes name, key, version, description, and tab info for linking.

    Per-plugin enabled state is stored in settings.json under `dashboard_plugins`.
    A plugin is enabled only if the user has explicitly toggled it on (default off).
    """
    from api.config import load_settings

    plugin_settings = load_settings().get("dashboard_plugins", {})
    plugins = []
    for name, manifest in sorted(PLUGIN_MANIFESTS.items()):
        tab = manifest.get("tab", {})
        path = tab.get("path", f"/{name}")
        plugins.append({
            "name": manifest.get("label") or manifest.get("name") or name,
            "key": name,
            "version": manifest.get("version", "0.0.0"),
            "description": manifest.get("description", ""),
            "tab": {
                "path": path,
                "label": tab.get("label") or manifest.get("label") or name,
            },
            "enabled": bool(plugin_settings.get(name, False)),
            "hooks": [],
        })
    return plugins


# ── Plugin management hub (B4: 9119 -> WebUI port) ───────────────────────────
# The management surface (install / rescan / enable / disable / update / remove /
# visibility / provider) rides on ``hermes_cli.plugins_cmd``. That module is only
# importable on hosts where the full Hermes agent is installed (e.g. the Mac
# Mini). On a standalone WebUI host it is absent, so every wrapper imports it
# lazily and degrades gracefully -- the hub panel still renders and reports the
# backend as unavailable instead of 500-ing. This mirrors the existing read-only
# plugin-visibility path in api/routes.py, which already lazy-imports hermes_cli
# with a try/except fallback.
#
# Path/PII safety: unlike the 9119 dashboard endpoint, the hub payload here omits
# raw on-disk plugin paths (only name/version/description/source/flags are
# surfaced) so local filesystem layout is not leaked to the client.


class PluginManagementUnavailable(RuntimeError):
    """Raised when hermes_cli plugin management is not importable on this host."""


def _plugins_cmd():
    """Return the hermes_cli.plugins_cmd module or raise PluginManagementUnavailable."""
    try:
        from hermes_cli import plugins_cmd  # type: ignore
    except Exception as exc:  # ImportError / partial install
        raise PluginManagementUnavailable(
            "Plugin management backend (hermes_cli) is not available on this host."
        ) from exc
    return plugins_cmd


def management_available() -> bool:
    """True when the hermes_cli plugin-management backend is importable."""
    try:
        _plugins_cmd()
        return True
    except PluginManagementUnavailable:
        return False


def _name_guard(name) -> str:
    """Reject path-traversal in a plugin-name URL/JSON parameter."""
    name = str(name or "")
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid plugin name.")
    return name


def _refresh_webui_manifests() -> None:
    """Reload WebUI dashboard-plugin manifests after an on-disk change."""
    PLUGIN_MANIFESTS.clear()
    _PLUGIN_STATIC_ROOTS.clear()
    try:
        load_plugins()
    except Exception:
        logger.exception("Failed to reload dashboard plugin manifests")


def _dashboard_manifest_for(name: str) -> dict | None:
    """Return the WebUI-loaded dashboard manifest for ``name`` (copy) or None."""
    manifest = PLUGIN_MANIFESTS.get(name)
    if not manifest:
        return None
    return dict(manifest)


def _empty_providers() -> dict:
    return {
        "memory_provider": "",
        "memory_options": [],
        "context_engine": "",
        "context_options": [],
    }


def _orphan_dashboard_rows(skip: set | None = None) -> list[dict]:
    skip = skip or set()
    rows = []
    for name, manifest in sorted(PLUGIN_MANIFESTS.items()):
        if name in skip:
            continue
        row = dict(manifest)
        row.setdefault("name", name)
        rows.append(row)
    return rows


def build_hub() -> dict:
    """Merged agent-plugin + dashboard-manifest + provider metadata for the hub.

    Mirrors hermes_cli web_server ``_merged_plugins_hub`` but is path-safe and
    degrades to a backend-unavailable payload (still listing WebUI dashboard
    plugins) when hermes_cli is absent.
    """
    try:
        pc = _plugins_cmd()
    except PluginManagementUnavailable as exc:
        return {
            "plugins": [],
            "orphan_dashboard_plugins": _orphan_dashboard_rows(),
            "providers": _empty_providers(),
            "backend_available": False,
            "error": str(exc),
        }

    from hermes_cli.config import get_hermes_home, load_config

    try:
        from hermes_cli.config import cfg_get
    except Exception:
        cfg_get = None

    disabled_set = pc._get_disabled_set()
    enabled_set = pc._get_enabled_set()

    config = load_config()
    if cfg_get is not None:
        hidden_plugins = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []
    else:
        dash_cfg = config.get("dashboard") if isinstance(config, dict) else None
        hidden_plugins = (dash_cfg or {}).get("hidden_plugins") or []

    plugins_root_resolved = (get_hermes_home() / "plugins").resolve()
    rows: list[dict] = []

    for name, version, description, source, dir_str in pc._discover_all_plugins():
        if name in disabled_set:
            runtime_status = "disabled"
        elif name in enabled_set:
            runtime_status = "enabled"
        else:
            runtime_status = "inactive"

        dir_path = Path(dir_str)
        has_dash_manifest = (
            name in PLUGIN_MANIFESTS or (dir_path / "dashboard" / "manifest.json").exists()
        )

        under_user_tree = False
        try:
            dir_path.resolve().relative_to(plugins_root_resolved)
            under_user_tree = True
        except ValueError:
            pass

        can_remove_update = (
            source in {"user", "git"} and under_user_tree and dir_path.is_dir()
        )

        auth_required = False
        auth_command = ""
        try:
            manifest_data = pc._read_manifest(dir_path)
            provides_tools = manifest_data.get("provides_tools") or []
            if provides_tools:
                from tools.registry import registry  # type: ignore

                for tname in provides_tools:
                    entry = registry.get_entry(tname)
                    if entry and entry.check_fn and not entry.check_fn():
                        auth_required = True
                        auth_command = f"hermes auth {name}"
                        break
        except Exception:
            pass

        rows.append({
            "name": name,
            "version": version or "",
            "description": description or "",
            "source": source,
            "runtime_status": runtime_status,
            "has_dashboard_manifest": has_dash_manifest,
            "dashboard_manifest": _dashboard_manifest_for(name),
            "can_remove": can_remove_update,
            "can_update_git": can_remove_update and (dir_path / ".git").exists(),
            "auth_required": auth_required,
            "auth_command": auth_command,
            "user_hidden": name in hidden_plugins,
        })

    agent_names = {r["name"] for r in rows}

    providers = _empty_providers()
    try:
        providers["memory_options"] = [
            {"name": n, "description": desc} for n, desc in pc._discover_memory_providers()
        ]
    except Exception:
        pass
    try:
        providers["context_options"] = [
            {"name": n, "description": desc} for n, desc in pc._discover_context_engines()
        ]
    except Exception:
        pass
    try:
        providers["memory_provider"] = pc._get_current_memory_provider() or ""
    except Exception:
        pass
    try:
        providers["context_engine"] = pc._get_current_context_engine() or ""
    except Exception:
        pass

    return {
        "plugins": rows,
        "orphan_dashboard_plugins": _orphan_dashboard_rows(skip=agent_names),
        "providers": providers,
        "backend_available": True,
    }


def install_plugin(identifier, *, force: bool = False, enable: bool = True) -> dict:
    pc = _plugins_cmd()
    result = pc.dashboard_install_plugin(
        str(identifier or "").strip(), force=bool(force), enable=bool(enable)
    )
    # Never leak the local install path back to the client.
    if isinstance(result, dict):
        result.pop("after_install_path", None)
    if isinstance(result, dict) and result.get("ok"):
        _refresh_webui_manifests()
    return result


def set_agent_plugin_enabled(name, *, enabled: bool) -> dict:
    name = _name_guard(name)
    pc = _plugins_cmd()
    return pc.dashboard_set_agent_plugin_enabled(name, enabled=bool(enabled))


def update_user_plugin(name) -> dict:
    name = _name_guard(name)
    pc = _plugins_cmd()
    result = pc.dashboard_update_user_plugin(name)
    if isinstance(result, dict) and result.get("ok"):
        _refresh_webui_manifests()
    return result


def remove_user_plugin(name) -> dict:
    name = _name_guard(name)
    pc = _plugins_cmd()
    result = pc.dashboard_remove_user_plugin(name)
    if isinstance(result, dict) and result.get("ok"):
        _refresh_webui_manifests()
    return result


def set_providers(*, memory_provider=None, context_engine=None) -> dict:
    pc = _plugins_cmd()
    if memory_provider is not None:
        pc._save_memory_provider(str(memory_provider))
    if context_engine is not None:
        pc._save_context_engine(str(context_engine))
    return {"ok": True}


def set_visibility(name, *, hidden: bool) -> dict:
    """Toggle a plugin's sidebar visibility (hermes config dashboard.hidden_plugins)."""
    name = _name_guard(name)
    # Confirm the management backend is present (so hermes config is the right
    # store); this also keeps behaviour consistent with the other wrappers.
    _plugins_cmd()
    from hermes_cli.config import load_config, save_config

    config = load_config()
    if not isinstance(config.get("dashboard"), dict):
        config["dashboard"] = {}
    hidden_list = config["dashboard"].get("hidden_plugins") or []
    if not isinstance(hidden_list, list):
        hidden_list = []
    if hidden and name not in hidden_list:
        hidden_list.append(name)
    elif not hidden and name in hidden_list:
        hidden_list.remove(name)
    config["dashboard"]["hidden_plugins"] = hidden_list
    save_config(config)
    return {"ok": True, "name": name, "hidden": bool(hidden)}


def rescan() -> dict:
    """Re-scan WebUI dashboard-plugin manifests and report the count."""
    _refresh_webui_manifests()
    result = {"ok": True, "count": len(PLUGIN_MANIFESTS), "backend_available": management_available()}
    return result
