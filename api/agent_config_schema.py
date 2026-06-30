"""
Agent config schema editor backend (B2 — 9119 → webui migration).

Ports the schema-derivation + normalize/denormalize logic from the 9119 FastAPI
dashboard (``hermes_cli/web_server.py`` lines 250-443, 839-876, 1137-1196,
3338-3355) so the WebUI can offer a CORE "Agent Config" tab over the Hermes
**agent** ``config.yaml`` — the runtime config the agent itself reads. This is
deliberately distinct from WebUI's own ``/api/settings`` + ``api/config.py``
runtime env: those manage the WebUI server, whereas this edits the agent's
``model`` / ``terminal`` / ``agent.*`` config. All endpoints are namespaced
under ``/api/agent-config*`` to avoid colliding with ``/api/settings``.

Degradation contract (PC vs Mini):
- ``DEFAULT_CONFIG`` (the schema source) is lazy-imported from
  ``hermes_cli.config``. On the PC where ``hermes_cli`` cannot import, it falls
  back to a committed fixture (``api/fixtures/agent_config_defaults.json``) so
  the schema form + defaults endpoints still render for offline smoke.
- The config file path is resolved through WebUI's OWN
  ``api/config._get_config_path()`` (which already honours ``HERMES_CONFIG_PATH``
  and the active profile). If that resolved file does not exist AND no override
  is set, the raw/structured GET endpoints fall back to the committed sample
  fixture so the editor is never blank during smoke. Writes always target the
  resolved (real) path — never the in-repo fixtures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DEFAULTS_FIXTURE = _FIXTURES_DIR / "agent_config_defaults.json"
_SAMPLE_FIXTURE = _FIXTURES_DIR / "agent_config_sample.yaml"


# ---------------------------------------------------------------------------
# Schema overrides — ported verbatim from 9119 web_server.py:254-347.
# Manual overrides for fields that need select options or custom types.
# ---------------------------------------------------------------------------
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "vercel_sandbox", "singularity"],
    },
    "terminal.vercel_runtime": {
        "type": "select",
        "description": "Vercel Sandbox runtime",
        "options": ["node24", "node22", "python3.13"],
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        "options": ["local", "openai"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    "telegram": "discord",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value (9119 web_server.py:377)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict.

    Ported verbatim from 9119 web_server.py:392-429.
    """
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version"}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG resolution (lazy hermes_cli → committed fixture).
# ---------------------------------------------------------------------------
def _default_config() -> Dict[str, Any]:
    """Return DEFAULT_CONFIG from hermes_cli, or the committed fixture on PC."""
    try:
        from hermes_cli.config import DEFAULT_CONFIG  # type: ignore

        if isinstance(DEFAULT_CONFIG, dict):
            return DEFAULT_CONFIG
    except Exception:
        pass
    try:
        return json.loads(_DEFAULTS_FIXTURE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_defaults() -> Dict[str, Any]:
    """GET /api/agent-config/defaults payload."""
    return _default_config()


def build_config_schema() -> Dict[str, Any]:
    """GET /api/agent-config/schema payload: {fields, category_order}.

    Built lazily (not at import) so a missing hermes_cli on PC degrades to the
    fixture without erroring at module load. Injects the virtual
    ``model_context_length`` field right after ``model`` (9119:434-443).
    """
    schema = _build_schema_from_config(_default_config())
    mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
    ordered: Dict[str, Dict[str, Any]] = {}
    for k, v in schema.items():
        ordered[k] = v
        if k == "model":
            ordered["model_context_length"] = mcl_entry
    if "model_context_length" not in ordered:
        ordered["model_context_length"] = mcl_entry
    return {"fields": ordered, "category_order": _CATEGORY_ORDER}


# ---------------------------------------------------------------------------
# Config file path + read/write (via WebUI's own config.py path resolution).
# ---------------------------------------------------------------------------
def _resolved_config_path() -> Path:
    """Resolve the agent config.yaml path via WebUI's own resolver.

    Honours HERMES_CONFIG_PATH + the active WebUI profile. Falls back to
    ~/.hermes/config.yaml if api.config cannot be imported.
    """
    try:
        from api.config import _get_config_path

        return _get_config_path()
    except Exception:
        return Path(os.path.expanduser("~/.hermes/config.yaml"))


def _read_path() -> Path:
    """Path to READ from: the real config if it exists, else the sample fixture.

    The fixture fallback only applies when no explicit HERMES_CONFIG_PATH is set,
    so a deliberately-empty override still reads empty (live behaviour) while a
    fresh PC checkout still renders the editor with sample content.
    """
    resolved = _resolved_config_path()
    if resolved.exists():
        return resolved
    if os.getenv("HERMES_CONFIG_PATH"):
        return resolved  # honour explicit override even when missing
    return _SAMPLE_FIXTURE


def load_agent_config() -> Dict[str, Any]:
    """Parse the agent config.yaml into a dict (empty dict if missing/bad)."""
    import yaml

    path = _read_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_agent_config(config: Dict[str, Any]) -> None:
    """Write a config dict to the resolved (real) config.yaml path.

    Always targets ``_resolved_config_path()`` — never the in-repo fixtures —
    so a smoke write against the sample fixture is impossible.
    """
    import yaml

    path = _resolved_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Normalize / denormalize — ported from 9119 web_server.py:839-859, 1137-1186.
# ---------------------------------------------------------------------------
def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten ``model`` dict → string + surface ``model_context_length``."""
    config = dict(config)
    model_val = config.get("model")
    if isinstance(model_val, dict):
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving (9119:1137-1186)."""
    config = dict(config)
    config.pop("_model_meta", None)

    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        try:
            disk_config = load_agent_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                disk_model["default"] = model_val
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass
    return config


# ---------------------------------------------------------------------------
# Endpoint payload builders (called from api/routes.py dispatch).
# ---------------------------------------------------------------------------
def get_config() -> Dict[str, Any]:
    """GET /api/agent-config: normalized config, internal keys stripped."""
    config = _normalize_config_for_web(load_agent_config())
    return {k: v for k, v in config.items() if not str(k).startswith("_")}


def update_config(config_body: Dict[str, Any]) -> None:
    """PUT /api/agent-config: denormalize the web payload and persist it."""
    if not isinstance(config_body, dict):
        raise ValueError("config must be a mapping")
    save_agent_config(_denormalize_config_from_web(config_body))


def get_config_raw() -> Dict[str, Any]:
    """GET /api/agent-config/raw: {yaml, path, is_fixture}."""
    read_path = _read_path()
    resolved = _resolved_config_path()
    try:
        text = read_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {
        "yaml": text,
        "path": str(resolved),
        "is_fixture": read_path != resolved,
    }


def update_config_raw(yaml_text: str) -> None:
    """PUT /api/agent-config/raw: validate YAML mapping and persist verbatim."""
    import yaml

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError("YAML must be a mapping")
    save_agent_config(parsed)
