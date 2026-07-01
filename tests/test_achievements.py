"""Hermetic coverage for the native achievements engine (plan B11).

``api/achievements.py`` is a pure-Python port of 9119's hermes-achievements
dashboard plugin. These tests never touch the real session store, network,
Supabase, or the ``test_server`` fixture. They:

* assert catalog integrity (unique ids, required fields, ~57+ entries,
  monotonic tier ladders, secret/multi-condition invariants);
* exercise the pure tier/requirement/boolean evaluation math directly with
  synthetic aggregates;
* drive ``analyze_messages`` / ``aggregate_stats`` over fabricated messages;
* run ``scan_sessions`` / ``compute_all`` / ``evaluate_all`` against a
  ``WebUIJsonSessionDB`` re-pointed (via monkeypatch) at ``tmp_path`` with a
  couple of fabricated session JSON files;
* verify the scan-status payload shape and the degrade path (missing session
  dir -> empty scan) plus secret/multi-condition badge rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import achievements as ach
from api import config as api_config
from api import models as api_models


# These tests are hermetic unit tests: they never talk to the out-of-process
# WebUI server. Override the conftest's session-scoped autouse ``test_server``
# fixture (which symlinks ~/.hermes/skills — a privileged op that fails on
# Windows) with a no-op so the file runs standalone on any host.
@pytest.fixture(scope="session", autouse=True)
def test_server():
    yield None


# ── env fixture: redirect caches + session store to tmp_path, reset globals ───

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point achievement caches + the WebUI session store at ``tmp_path``.

    Also resets the module-level snapshot cache / scan-status globals so tests
    never leak state into each other.
    """
    state_dir = tmp_path / "state"
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(api_config, "STATE_DIR", state_dir)
    monkeypatch.setattr(api_models, "SESSION_DIR", session_dir)

    ach._SNAPSHOT_CACHE = None
    ach._SNAPSHOT_CACHE_AT = 0
    ach._SCAN_STATUS.update(
        {
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "last_duration_ms": None,
            "run_count": 0,
        }
    )
    yield session_dir
    ach._SNAPSHOT_CACHE = None
    ach._SNAPSHOT_CACHE_AT = 0


def _write_session(session_dir: Path, sid: str, messages, **meta) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "title": meta.get("title", "Untitled"),
        "messages": messages,
        "message_count": len(messages),
        "created_at": meta.get("created_at", 1_700_000_000),
        "updated_at": meta.get("updated_at", 1_700_000_100),
        "last_message_at": meta.get("last_message_at", 1_700_000_100),
    }
    if "model" in meta:
        payload["model"] = meta["model"]
    (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


# ── catalog integrity ─────────────────────────────────────────────────────────

def test_catalog_size_and_unique_ids():
    assert len(ach.ACHIEVEMENTS) >= 55, "catalog shrank unexpectedly"
    ids = [a["id"] for a in ach.ACHIEVEMENTS]
    assert len(ids) == len(set(ids)), "achievement ids must be unique"


def test_every_achievement_has_required_fields():
    for a in ach.ACHIEVEMENTS:
        for field in ("id", "name", "description", "category", "kind", "icon"):
            assert a.get(field), f"{a.get('id')!r} missing {field}"
        # Every definition is either tiered (threshold_metric + tiers) or
        # multi-condition (requirements) — never both, never neither.
        tiered = "threshold_metric" in a
        multi = "requirements" in a
        assert tiered ^ multi, f"{a['id']} must be exactly one of tiered/requirements"


def test_tiered_ladders_are_monotonic_and_named():
    for a in ach.ACHIEVEMENTS:
        if "threshold_metric" not in a:
            continue
        tiers = a["tiers"]
        assert [t["name"] for t in tiers] == ach.TIER_NAMES
        thresholds = [t["threshold"] for t in tiers]
        assert thresholds == sorted(thresholds)
        assert len(set(thresholds)) == len(thresholds), f"{a['id']} dup thresholds"


def test_multi_condition_have_requirements_only():
    multi = [a for a in ach.ACHIEVEMENTS if a["kind"] == "multi_condition"]
    assert multi, "expected some multi_condition badges"
    for a in multi:
        assert "requirements" in a and a["requirements"]
        assert "threshold_metric" not in a
        for r in a["requirements"]:
            assert set(r) == {"metric", "gte"}
            assert isinstance(r["gte"], int)


def test_secret_badges_flagged():
    secret_ids = {a["id"] for a in ach.ACHIEVEMENTS if a.get("secret")}
    assert {"port_3000_taken", "permission_denied_any_percent"} <= secret_ids
    # A non-secret should not be flagged.
    let_him_cook = next(a for a in ach.ACHIEVEMENTS if a["id"] == "let_him_cook")
    assert not let_him_cook.get("secret")


def test_tiers_and_req_helpers():
    built = ach.tiers([1, 2, 3, 4, 5])
    assert built == [
        {"name": "Copper", "threshold": 1},
        {"name": "Silver", "threshold": 2},
        {"name": "Gold", "threshold": 3},
        {"name": "Diamond", "threshold": 4},
        {"name": "Olympian", "threshold": 5},
    ]
    assert ach.req("total_errors", 10) == {"metric": "total_errors", "gte": 10}


# ── tier / requirement / boolean evaluation math ──────────────────────────────

def _def(aid):
    return next(a for a in ach.ACHIEVEMENTS if a["id"] == aid)


def test_evaluate_tiered_zero_progress_not_unlocked():
    res = ach.evaluate_tiered(_def("let_him_cook"), {"max_tool_calls_in_session": 0})
    assert res["unlocked"] is False
    assert res["tier"] is None
    assert res["state"] == "discovered"
    assert res["next_tier"] == "Copper"
    assert res["next_threshold"] == 200
    assert res["progress_pct"] == 0


def test_evaluate_tiered_mid_ladder():
    res = ach.evaluate_tiered(_def("let_him_cook"), {"max_tool_calls_in_session": 600})
    assert res["unlocked"] is True
    assert res["tier"] == "Silver"
    assert res["next_tier"] == "Gold"
    assert res["next_threshold"] == 1200
    # floor((600-500)/(1200-500)*100) == 14
    assert res["progress_pct"] == 14


def test_evaluate_tiered_max_tier_caps_at_100():
    res = ach.evaluate_tiered(_def("let_him_cook"), {"max_tool_calls_in_session": 99999})
    assert res["unlocked"] is True
    assert res["tier"] == "Olympian"
    assert res["next_tier"] is None
    assert res["progress_pct"] == 100


def test_evaluate_tiered_secret_hidden_until_discovered():
    secret_def = _def("port_3000_taken")
    hidden = ach.evaluate_tiered(secret_def, {})
    assert hidden["state"] == "secret"
    assert hidden["discovered"] is False
    # Any progress reveals it (state leaves 'secret').
    seen = ach.evaluate_tiered(secret_def, {"port_conflict_events": 3})
    assert seen["state"] != "secret"
    assert seen["discovered"] is True


def test_evaluate_requirements_partial_then_complete():
    full_send = _def("full_send")
    none = ach.evaluate_requirements(full_send, {})
    assert none["unlocked"] is False
    assert none["state"] == "discovered"  # not secret -> discovered even at 0

    partial = ach.evaluate_requirements(
        full_send,
        {"max_terminal_calls_in_session": 180, "max_file_tool_calls_in_session": 0,
         "max_web_browser_calls_in_session": 0},
    )
    assert partial["unlocked"] is False
    assert 0 < partial["progress_pct"] < 100

    complete = ach.evaluate_requirements(
        full_send,
        {"max_terminal_calls_in_session": 999, "max_file_tool_calls_in_session": 999,
         "max_web_browser_calls_in_session": 999},
    )
    assert complete["unlocked"] is True
    assert complete["progress_pct"] == 100


def test_evaluate_requirements_secret_hidden():
    ocf = _def("one_character_fix")
    assert ocf.get("secret") is True
    hidden = ach.evaluate_requirements(ocf, {})
    assert hidden["state"] == "secret"
    assert hidden["discovered"] is False


def test_evaluate_definition_dispatch():
    tiered = ach.evaluate_definition(_def("let_him_cook"), {"max_tool_calls_in_session": 250})
    assert tiered["tier"] == "Copper"
    req = ach.evaluate_definition(_def("full_send"), {})
    assert req["tier"] is None and req["next_threshold"] == 100
    boolean = ach.evaluate_boolean({"metric": "foo"}, {"foo": 1})
    assert boolean["unlocked"] is True and boolean["progress_pct"] == 100
    assert ach.evaluate_boolean({"metric": "foo"}, {})["unlocked"] is False


# ── model provider / local-model helpers ──────────────────────────────────────

def test_model_provider_inference():
    # A slash-prefixed name yields the vendor prefix directly.
    assert ach.model_provider("anthropic/claude-3-opus") == "anthropic"
    assert ach.model_provider("openai/gpt-4o") == "openai"
    # A bare model name without a known provider substring falls back to the
    # leading token ("gpt-4o" contains no "openai" marker).
    assert ach.model_provider("gpt-4o") == "gpt"
    assert ach.model_provider("gemini-1.5-pro") == "google"  # gemini maps to google
    assert ach.model_provider("") is None
    assert ach.model_provider("none") is None


def test_is_local_model_name():
    assert ach.is_local_model_name("ollama/llama3") is True
    assert ach.is_local_model_name("local/mistral") is True
    assert ach.is_local_model_name("gpt-4o") is False
    assert ach.is_local_model_name("") is False


# ── criteria / display / label ────────────────────────────────────────────────

def test_metric_label_known_and_fallback():
    assert ach.metric_label("total_errors") == "error/failed/traceback messages observed"
    assert ach.metric_label("some_unknown_metric") == "some unknown metric"


def test_criteria_for_tiered_and_requirements():
    crit = ach.criteria_for(_def("let_him_cook"))
    assert "Tier ladder" in crit and "Copper 200" in crit
    crit_req = ach.criteria_for(_def("full_send"))
    assert crit_req.startswith("Requirement:") and ">=" in crit_req


def test_display_achievement_masks_secret_state():
    masked = ach.display_achievement({**_def("port_3000_taken"), "state": "secret"})
    assert masked["name"] == "???"
    assert masked["icon"] == "secret"
    assert "Secret achievement" in masked["description"]
    assert "criteria" in masked
    # Non-secret state passes name/icon through and adds criteria.
    shown = ach.display_achievement({**_def("let_him_cook"), "state": "unlocked"})
    assert shown["name"] == "Let Him Cook"
    assert "criteria" in shown


# ── analyze_messages / aggregate_stats ────────────────────────────────────────

def test_analyze_messages_counts_openai_and_anthropic_tool_calls():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "terminal_run"}},
            {"name": "read_file"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "web_search"},
            {"type": "text", "text": "thinking"},
        ]},
    ]
    st = ach.analyze_messages("s1", "Title", messages)
    assert st["tool_call_count"] == 3
    assert st["distinct_tool_count"] == 3
    assert st["terminal_calls"] == 1
    assert st["web_calls"] == 1
    assert st["file_reads_searches"] == 1
    assert st["message_count"] == 2
    assert isinstance(st["tool_names"], set)


def test_analyze_messages_tool_result_rows_not_double_counted():
    messages = [
        {"role": "assistant", "tool_calls": [{"name": "terminal_run"}]},
        # A tool result row echoes the call; it must NOT add a new tool call.
        {"role": "tool", "content": "output of terminal_run"},
    ]
    st = ach.analyze_messages("s2", "T", messages)
    assert st["tool_call_count"] == 1


def test_analyze_messages_error_and_port_signals():
    messages = [
        {"role": "user", "content": "start dev server"},
        {"role": "tool", "content": "Error: port 3000 already in use eaddrinuse; permission denied"},
    ]
    st = ach.analyze_messages("s3", "T", messages)
    assert st["error_count"] >= 1
    assert st["port_conflict_events"] == 1
    assert st["permission_denied_events"] >= 1


def test_aggregate_stats_maxes_and_sums():
    s_a = ach.analyze_messages("a", "A", [
        {"role": "assistant", "tool_calls": [{"name": "terminal_run"}, {"name": "read_file"}]},
        {"role": "tool", "content": "error boom"},
    ])
    s_b = ach.analyze_messages("b", "B", [
        {"role": "assistant", "tool_calls": [{"name": "web_search"}]},
    ])
    agg = ach.aggregate_stats([s_a, s_b])
    assert agg["session_count"] == 2
    assert agg["max_tool_calls_in_session"] == 2
    assert agg["total_tool_calls"] == 3
    assert agg["total_errors"] >= 1
    assert agg["total_terminal_calls"] == 1


def test_aggregate_stats_empty():
    agg = ach.aggregate_stats([])
    assert agg["session_count"] == 0
    assert agg["max_tool_calls_in_session"] == 0
    assert agg["distinct_model_count"] == 0


# ── scan_sessions against a fabricated tmp_path store ──────────────────────────

def test_scan_sessions_reads_fabricated_sessions(env):
    session_dir = env
    _write_session(session_dir, "sess-one", [
        {"role": "assistant", "tool_calls": [{"name": "terminal_run"}, {"name": "read_file"}]},
        {"role": "tool", "content": "error occurred"},
    ], model="anthropic/claude-3", title="One")
    _write_session(session_dir, "sess-two", [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "web_search"}]},
    ], model="openai/gpt-4o", title="Two")

    scan = ach.scan_sessions()
    assert scan.get("error") is None
    assert len(scan["sessions"]) == 2
    agg = scan["aggregate"]
    assert agg["session_count"] == 2
    assert agg["total_tool_calls"] == 3
    assert agg["distinct_provider_count"] == 2  # anthropic + openai
    meta = scan["scan_meta"]
    assert meta["sessions_total"] == 2
    assert meta["sessions_rescanned"] == 2
    assert meta["sessions_reused"] == 0
    # Checkpoint was persisted under the redirected state dir.
    assert ach.checkpoint_path().exists()


def test_scan_sessions_warm_scan_reuses_checkpoint(env):
    session_dir = env
    _write_session(session_dir, "sess-warm", [
        {"role": "assistant", "tool_calls": [{"name": "terminal_run"}]},
    ], title="Warm")
    first = ach.scan_sessions()
    assert first["scan_meta"]["sessions_rescanned"] == 1
    # Second scan with unchanged fingerprint should reuse cached stats.
    second = ach.scan_sessions()
    assert second["scan_meta"]["sessions_reused"] == 1
    assert second["scan_meta"]["sessions_rescanned"] == 0
    assert second["scan_meta"]["mode"] == "incremental"


def test_scan_sessions_missing_dir_degrades_to_empty(env):
    # env fixture points SESSION_DIR at a dir that was never created.
    scan = ach.scan_sessions()
    assert scan["sessions"] == []
    assert scan["aggregate"]["session_count"] == 0
    assert scan.get("error") is None


def test_scan_sessions_import_failure_degrades(env, monkeypatch):
    # Simulate the WebUIJsonSessionDB import failing (SDK/module absent).
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "api.webui_session_db":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    scan = ach.scan_sessions()
    assert scan["sessions"] == []
    assert scan["aggregate"] == {}
    assert "Could not import" in scan["error"]
    assert scan["scan_meta"]["mode"] == "failed"


# ── compute_all / evaluate_all end-to-end (synchronous, force) ────────────────

def test_compute_all_shape(env):
    session_dir = env
    _write_session(session_dir, "sess-c", [
        {"role": "assistant", "tool_calls": [{"name": "terminal_run"}]},
    ], title="C")
    computed = ach.compute_all()
    for key in ("achievements", "sessions", "aggregate", "scan_meta",
                "unlocked_count", "discovered_count", "secret_count",
                "total_count", "generated_at"):
        assert key in computed
    assert computed["total_count"] == len(ach.ACHIEVEMENTS)
    assert len(computed["achievements"]) == len(ach.ACHIEVEMENTS)
    # Every rendered achievement carries evaluation fields.
    for a in computed["achievements"]:
        assert "state" in a and "progress_pct" in a


def test_evaluate_all_force_unlocks_message_badge(env):
    session_dir = env
    # 300 trivial messages -> unlocks "This Was Supposed To Be Quick" (Copper 300).
    messages = [{"role": "user", "content": "hi"} for _ in range(300)]
    _write_session(session_dir, "sess-long", messages, title="Long")
    data = ach.evaluate_all(force=True)
    quick = next(a for a in data["achievements"] if a["id"] == "supposed_to_be_quick")
    assert quick["unlocked"] is True
    assert quick["tier"] == "Copper"
    assert data["unlocked_count"] >= 1
    # State file was persisted with an unlock record.
    state = ach.load_state()
    assert "supposed_to_be_quick" in state.get("unlocks", {})


# ── scan-status payload shape ─────────────────────────────────────────────────

def test_scan_status_payload_shape(env):
    payload = ach._scan_status_payload(now=1_700_000_500)
    for key in ("state", "started_at", "finished_at", "last_error",
                "last_duration_ms", "run_count", "ttl_seconds",
                "snapshot_generated_at", "snapshot_age_seconds", "snapshot_stale"):
        assert key in payload
    assert payload["state"] == "idle"
    assert payload["ttl_seconds"] == ach.SNAPSHOT_TTL_SECONDS
    # No snapshot cached -> stale.
    assert payload["snapshot_stale"] is True


def test_is_snapshot_stale_logic():
    assert ach._is_snapshot_stale(None) is True
    assert ach._is_snapshot_stale({"generated_at": 0}) is True
    fresh = {"generated_at": 1_000}
    assert ach._is_snapshot_stale(fresh, now=1_000 + ach.SNAPSHOT_TTL_SECONDS - 1) is False
    assert ach._is_snapshot_stale(fresh, now=1_000 + ach.SNAPSHOT_TTL_SECONDS + 5) is True


# ── secret / multi-condition badges via session-badge pathway ─────────────────

def test_session_badges_unknown_session_returns_empty(env):
    # No cache/sessions -> evaluate_all returns pending payload (empty sessions).
    out = ach._session_badges_payload("does-not-exist")
    assert out == {"session_id": "does-not-exist", "badges": []}


def test_session_badges_for_strong_session(env):
    session_dir = env
    messages = [{"role": "user", "content": "hi"} for _ in range(300)]
    _write_session(session_dir, "sess-badge", messages, title="Badge")
    # Prime the snapshot cache synchronously so evaluate_all() (non-force,
    # called inside _session_badges_payload) serves our scanned session.
    ach.evaluate_all(force=True)
    out = ach._session_badges_payload("sess-badge")
    assert out["session_id"] == "sess-badge"
    badge_ids = {b["id"] for b in out["badges"]}
    assert "supposed_to_be_quick" in badge_ids
    # Rendered badges are display-shaped (criteria attached).
    for b in out["badges"]:
        assert "criteria" in b


# ── state / snapshot / checkpoint persistence helpers ─────────────────────────

def test_state_roundtrip_and_defaults(env):
    assert ach.load_state() == {"unlocks": {}}
    ach.save_state({"unlocks": {"x": {"unlocked_at": 1}}})
    assert ach.load_state()["unlocks"]["x"]["unlocked_at"] == 1


def test_checkpoint_defaults_and_roundtrip(env):
    cp = ach.load_checkpoint()
    assert cp["schema_version"] == 1 and cp["sessions"] == {}
    ach.save_checkpoint({"sessions": {"s": {"fingerprint": {}, "stats": {}}}})
    reloaded = ach.load_checkpoint()
    assert "s" in reloaded["sessions"]


def test_json_safe_serializes_sets():
    out = ach._json_safe({"names": {"b", "a"}, "nested": [{"x": {"z"}}]})
    assert out["names"] == ["a", "b"]
    assert out["nested"][0]["x"] == ["z"]


def test_session_fingerprint_uses_webui_metadata():
    fp = ach.session_fingerprint({
        "last_message_at": 5, "created_at": 1, "model": "m",
        "message_count": 9, "title": "T",
    })
    assert fp == {"last_active": 5, "started_at": 1, "model": "m",
                  "message_count": 9, "title": "T"}
    # Falls back to updated_at when last_message_at absent, Untitled default.
    fp2 = ach.session_fingerprint({"updated_at": 7})
    assert fp2["last_active"] == 7 and fp2["title"] == "Untitled"
