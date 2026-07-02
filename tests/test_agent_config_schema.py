"""Hermetic coverage for the agent config schema editor backend (B2 migration).

``api/agent_config_schema.py`` ports the 9119 dashboard's schema-derivation +
normalize/denormalize logic so WebUI can edit the Hermes AGENT config.yaml. It
is degrade-safe: ``DEFAULT_CONFIG`` is lazy-imported from ``hermes_cli.config``
and falls back to a committed fixture on the PC, and the read path falls back to
a sample fixture when no real config.yaml exists. These tests exercise that
degrade path plus the pure schema/normalizer/validator logic without touching
the network, real Supabase/GCS, or the session ``test_server`` fixture.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from api import agent_config_schema as acs

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_FIXTURE = ROOT / "api" / "fixtures" / "agent_config_defaults.json"
SAMPLE_FIXTURE = ROOT / "api" / "fixtures" / "agent_config_sample.yaml"


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Override the conftest autouse ``test_server`` fixture.

    These are hermetic unit tests: they import ``api.agent_config_schema``
    directly and never hit the HTTP server, real Supabase/GCS, or hermes_cli.
    The conftest fixture boots a full server subprocess (and symlinks
    ~/.hermes/skills, which needs privilege on Windows), so we shadow it with a
    no-op to keep the file server-free and unit-only.
    """
    yield None


# ── _infer_type (pure) ────────────────────────────────────────────────────────

def test_infer_type_maps_python_values():
    assert acs._infer_type(True) == "boolean"
    # bool must be checked before int (bool is an int subclass).
    assert acs._infer_type(False) == "boolean"
    assert acs._infer_type(5) == "number"
    assert acs._infer_type(1.5) == "number"
    assert acs._infer_type([1, 2]) == "list"
    assert acs._infer_type({"a": 1}) == "object"
    assert acs._infer_type("hi") == "string"
    assert acs._infer_type(None) == "string"


# ── _build_schema_from_config (schema derivation) ─────────────────────────────

def test_build_schema_flattens_nested_dot_paths():
    cfg = {"model": "x", "terminal": {"backend": "local", "timeout": 180}}
    schema = acs._build_schema_from_config(cfg)
    assert "model" in schema
    assert "terminal.backend" in schema
    assert "terminal.timeout" in schema
    # Nested dict itself is not emitted as a leaf field.
    assert "terminal" not in schema
    assert schema["terminal.timeout"]["type"] == "number"


def test_build_schema_top_level_scalar_is_general():
    schema = acs._build_schema_from_config({"model": "x", "timezone": "UTC"})
    assert schema["model"]["category"] == "general"
    assert schema["timezone"]["category"] == "general"


def test_build_schema_skips_config_version_key():
    schema = acs._build_schema_from_config({"_config_version": 1, "model": "x"})
    assert "_config_version" not in schema
    assert "model" in schema


def test_build_schema_applies_overrides_and_category_merge():
    cfg = {"terminal": {"backend": "local"}, "dashboard": {"theme": "default"}}
    schema = acs._build_schema_from_config(cfg)
    # terminal.backend override -> select with options.
    assert schema["terminal.backend"]["type"] == "select"
    assert "local" in schema["terminal.backend"]["options"]
    # dashboard category merges into "display".
    assert schema["dashboard.theme"]["category"] == "display"


def test_build_schema_derives_humanized_description():
    schema = acs._build_schema_from_config({"agent": {"max_turns": 5}})
    assert schema["agent.max_turns"]["description"] == "Agent → Max Turns"


# ── _default_config degrade path (hermes_cli absent -> fixture) ────────────────

def test_default_config_falls_back_to_committed_fixture():
    # hermes_cli is not importable on the PC, so this exercises the fixture path.
    cfg = acs._default_config()
    assert isinstance(cfg, dict) and cfg
    expected = json.loads(DEFAULTS_FIXTURE.read_text(encoding="utf-8"))
    assert cfg == expected


def test_default_config_uses_hermes_cli_when_importable(monkeypatch):
    fake = types.ModuleType("hermes_cli")
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.DEFAULT_CONFIG = {"model": "sentinel", "agent": {"max_turns": 1}}
    fake.config = fake_cfg
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    assert acs._default_config() == {"model": "sentinel", "agent": {"max_turns": 1}}


def test_default_config_ignores_non_dict_default_config(monkeypatch):
    fake = types.ModuleType("hermes_cli")
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.DEFAULT_CONFIG = ["not", "a", "dict"]
    fake.config = fake_cfg
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    # Non-dict DEFAULT_CONFIG -> ignored, fixture fallback used.
    assert acs._default_config() == json.loads(DEFAULTS_FIXTURE.read_text(encoding="utf-8"))


def test_get_defaults_returns_default_config():
    assert acs.get_defaults() == acs._default_config()


# ── build_config_schema (payload shape + virtual field injection) ─────────────

def test_build_config_schema_shape_and_category_order():
    payload = acs.build_config_schema()
    assert set(payload.keys()) == {"fields", "category_order"}
    assert payload["category_order"] == acs._CATEGORY_ORDER
    assert isinstance(payload["fields"], dict) and payload["fields"]


def test_build_config_schema_injects_model_context_length_after_model(monkeypatch):
    monkeypatch.setattr(acs, "_default_config", lambda: {"model": "x", "timezone": "UTC"})
    fields = acs.build_config_schema()["fields"]
    keys = list(fields.keys())
    assert keys.index("model_context_length") == keys.index("model") + 1
    assert fields["model_context_length"] is acs._SCHEMA_OVERRIDES["model_context_length"]


def test_build_config_schema_appends_mcl_when_model_absent(monkeypatch):
    monkeypatch.setattr(acs, "_default_config", lambda: {"timezone": "UTC"})
    fields = acs.build_config_schema()["fields"]
    assert "model_context_length" in fields
    assert "model" not in fields


# ── path resolution + read fallback ───────────────────────────────────────────

def test_resolved_config_path_honours_env_override(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(target))
    assert acs._resolved_config_path() == target


def test_read_path_falls_back_to_sample_fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    missing = tmp_path / "nope" / "config.yaml"
    monkeypatch.setattr(acs, "_resolved_config_path", lambda: missing)
    # Compare resolved paths: the module's _SAMPLE_FIXTURE can carry an
    # unresolved "tests/.." segment depending on import context, so a raw ==
    # against the clean fixture path fails in the full suite though it's the
    # same file.
    assert acs._read_path().resolve() == SAMPLE_FIXTURE.resolve()


def test_read_path_honours_missing_explicit_override(tmp_path, monkeypatch):
    missing = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(missing))
    # Explicit override wins even when the file is absent (no fixture fallback).
    assert acs._read_path() == missing


def test_read_path_returns_real_config_when_it_exists(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_text("model: x\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(target))
    assert acs._read_path() == target


# ── load_agent_config ─────────────────────────────────────────────────────────

def test_load_agent_config_reads_sample_fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(acs, "_resolved_config_path", lambda: tmp_path / "missing.yaml")
    cfg = acs.load_agent_config()
    assert isinstance(cfg, dict)
    assert cfg["model"]["default"] == "anthropic/claude-sonnet-4.6"


def test_load_agent_config_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "gone.yaml"))
    assert acs.load_agent_config() == {}


def test_load_agent_config_bad_yaml_returns_empty(tmp_path, monkeypatch):
    bad = tmp_path / "config.yaml"
    bad.write_text("model: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(bad))
    assert acs.load_agent_config() == {}


def test_load_agent_config_non_mapping_returns_empty(tmp_path, monkeypatch):
    scalar = tmp_path / "config.yaml"
    scalar.write_text("just a string\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(scalar))
    assert acs.load_agent_config() == {}


# ── save_agent_config (writes resolved real path, never fixtures) ─────────────

def test_save_agent_config_writes_resolved_path(tmp_path, monkeypatch):
    target = tmp_path / "sub" / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(target))
    acs.save_agent_config({"model": "anthropic/claude", "agent": {"max_turns": 7}})
    assert target.exists()
    import yaml

    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert written == {"model": "anthropic/claude", "agent": {"max_turns": 7}}


def test_save_agent_config_never_touches_sample_fixture(tmp_path, monkeypatch):
    before = SAMPLE_FIXTURE.read_text(encoding="utf-8")
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    target = tmp_path / "config.yaml"
    monkeypatch.setattr(acs, "_resolved_config_path", lambda: target)
    acs.save_agent_config({"model": "x"})
    # Even though the READ path would be the fixture, WRITE targets resolved.
    assert target.exists()
    assert SAMPLE_FIXTURE.read_text(encoding="utf-8") == before


# ── normalize / denormalize ───────────────────────────────────────────────────

def test_normalize_flattens_model_dict():
    out = acs._normalize_config_for_web(
        {"model": {"default": "anthropic/x", "context_length": 4096}}
    )
    assert out["model"] == "anthropic/x"
    assert out["model_context_length"] == 4096


def test_normalize_model_dict_uses_name_fallback():
    out = acs._normalize_config_for_web({"model": {"name": "prov/y"}})
    assert out["model"] == "prov/y"
    assert out["model_context_length"] == 0


def test_normalize_string_model_sets_zero_context():
    out = acs._normalize_config_for_web({"model": "anthropic/x"})
    assert out["model"] == "anthropic/x"
    assert out["model_context_length"] == 0


def test_normalize_non_int_context_length_coerced_to_zero():
    out = acs._normalize_config_for_web(
        {"model": {"default": "x", "context_length": "bogus"}}
    )
    assert out["model_context_length"] == 0


def test_denormalize_drops_model_meta_and_context_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    out = acs._denormalize_config_from_web(
        {"model": "", "_model_meta": {"a": 1}, "model_context_length": 0}
    )
    assert "_model_meta" not in out
    assert "model_context_length" not in out


def test_denormalize_merges_into_disk_model_dict(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    import yaml

    disk.write_text(
        yaml.safe_dump({"model": {"default": "old", "provider": "anthropic"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": 8192}
    )
    assert out["model"]["default"] == "new/model"
    assert out["model"]["provider"] == "anthropic"
    assert out["model"]["context_length"] == 8192


def test_denormalize_removes_context_length_when_zero(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    import yaml

    disk.write_text(
        yaml.safe_dump({"model": {"default": "old", "context_length": 999}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": 0}
    )
    assert "context_length" not in out["model"]


def test_denormalize_string_disk_model_builds_dict_when_ctx_positive(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    disk.write_text("model: plainstring\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": 2048}
    )
    assert out["model"] == {"default": "new/model", "context_length": 2048}


def test_denormalize_string_disk_model_stays_string_when_ctx_zero(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    disk.write_text("model: plainstring\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": 0}
    )
    assert out["model"] == "new/model"


def test_denormalize_coerces_non_int_context_length(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    disk.write_text("model: plainstring\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    # "4096" -> int 4096 -> positive -> dict built.
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": "4096"}
    )
    assert out["model"] == {"default": "new/model", "context_length": 4096}


def test_denormalize_bad_context_length_string_becomes_zero(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    disk.write_text("model: plainstring\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs._denormalize_config_from_web(
        {"model": "new/model", "model_context_length": "not-a-number"}
    )
    # Bad ctx -> 0 -> string model preserved.
    assert out["model"] == "new/model"


# ── endpoint payload builders ─────────────────────────────────────────────────

def test_get_config_strips_internal_keys(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    import yaml

    disk.write_text(
        yaml.safe_dump(
            {"model": {"default": "x", "context_length": 100}, "_secret": "hide", "timezone": "UTC"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs.get_config()
    assert "_secret" not in out
    assert out["model"] == "x"
    assert out["model_context_length"] == 100
    assert out["timezone"] == "UTC"


def test_update_config_rejects_non_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    with pytest.raises(ValueError):
        acs.update_config(["not", "a", "dict"])


def test_update_config_persists_denormalized(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    acs.update_config({"model": "new/model", "model_context_length": 4096, "timezone": "UTC"})
    import yaml

    written = yaml.safe_load(disk.read_text(encoding="utf-8"))
    assert written["model"] == {"default": "new/model", "context_length": 4096}
    assert written["timezone"] == "UTC"
    assert "model_context_length" not in written


def test_get_config_raw_reports_fixture_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    resolved = tmp_path / "config.yaml"
    monkeypatch.setattr(acs, "_resolved_config_path", lambda: resolved)
    out = acs.get_config_raw()
    assert out["is_fixture"] is True
    assert out["path"] == str(resolved)
    assert "model" in out["yaml"]


def test_get_config_raw_real_file_not_fixture(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    disk.write_text("model: real\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    out = acs.get_config_raw()
    assert out["is_fixture"] is False
    assert out["path"] == str(disk)
    assert out["yaml"] == "model: real\n"


def test_update_config_raw_rejects_invalid_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    with pytest.raises(ValueError):
        acs.update_config_raw("model: [unclosed\n")


def test_update_config_raw_rejects_non_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    with pytest.raises(ValueError):
        acs.update_config_raw("- just\n- a\n- list\n")


def test_update_config_raw_empty_yaml_persists_empty_mapping(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    acs.update_config_raw("")
    import yaml

    assert yaml.safe_load(disk.read_text(encoding="utf-8")) in (None, {})


def test_update_config_raw_persists_verbatim_mapping(tmp_path, monkeypatch):
    disk = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(disk))
    acs.update_config_raw("model: keep\nagent:\n  max_turns: 3\n")
    import yaml

    written = yaml.safe_load(disk.read_text(encoding="utf-8"))
    assert written == {"model": "keep", "agent": {"max_turns": 3}}
