"""Regression tests for #5132 — cap sidebar state.db source/title overrides to top-N.

On power users with thousands of sessions, GET /api/sessions blocked 5-18s in the
all_sessions.state_db_overrides stage: _apply_sidebar_state_db_overrides probed
state.db for EVERY row's source/title (2400+ ids) on every concurrent poll, piling
up sqlite reads against the gateway-watcher + agent writes and flapping the UI to
"Connection lost". The sidebar paints pinned-first then newest-first, and the caller
passes an already-sorted list, so overriding only the top-N (default 300) most-recent
rows covers the visible window while bounding wall-clock — exactly the precedent set
for lineage enrichment in #4638. The cap is env-configurable
(HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N) and fails open.

These tests pin: (1) only the top-N ids are probed when the list exceeds the cap,
(2) the env override is honored, (3) a non-positive / unparseable cap disables the
cap (override all), (4) lists at/under the cap probe everything, and (5) a DB error
never breaks /api/sessions.
"""
from __future__ import annotations

import api.models as models


def _capture_probed_ids(monkeypatch):
    """Patch _read_state_db_sidebar_overrides to record the id set it is asked for."""
    seen = {}

    def _fake_read(db_path, id_set):
        seen["ids"] = set(id_set)
        return {}

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    return seen


def _sessions(n):
    # Caller passes an already pinned-first/newest-first sorted list; index order
    # therefore IS paint priority. id "s0" is the most-recent/visible-most.
    return [{"session_id": f"s{i}"} for i in range(n)]


def test_caps_overrides_to_top_n_default_300(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(300)}, (
        "Default cap must probe exactly the top-300 (paint-priority) sessions"
    )


def test_env_override_changes_cap(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "50")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(50)}, (
        "HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N must bound the probed set"
    )


def test_non_positive_cap_disables_capping(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    models._apply_sidebar_state_db_overrides(_sessions(500))
    assert len(seen["ids"]) == 500, "cap<=0 must override all sessions (cap disabled)"


def test_unparseable_cap_falls_back_to_default(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "not-a-number")
    models._apply_sidebar_state_db_overrides(_sessions(1000))
    assert seen["ids"] == {f"s{i}" for i in range(300)}, (
        "An unparseable cap must fall back to the default 300, not crash"
    )


def test_list_under_cap_probes_everything(monkeypatch):
    seen = _capture_probed_ids(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", raising=False)
    models._apply_sidebar_state_db_overrides(_sessions(120))
    assert seen["ids"] == {f"s{i}" for i in range(120)}, (
        "A list at/under the cap must override every session"
    )


def test_override_failure_is_swallowed(monkeypatch):
    """Override lookup must fail open — a DB error never breaks /api/sessions."""
    def _boom(db_path, id_set):
        raise RuntimeError("db down")

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _boom)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    # Must not raise.
    models._apply_sidebar_state_db_overrides(_sessions(10))


def test_capped_rows_still_receive_overrides(monkeypatch):
    """The top-N rows that ARE probed must still get their metadata applied."""
    def _fake_read(db_path, id_set):
        # Return a webui source override for s0 only.
        return {
            "s0": {
                "_state_db_source": "webui",
                "_state_db_source_tag": "webui",
                "_state_db_raw_source": "webui",
                "_state_db_session_source": "webui",
                "_state_db_source_label": "WebUI",
            }
        }

    monkeypatch.setattr(models, "_read_state_db_sidebar_overrides", _fake_read)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: ":memory:")
    sessions: list[dict] = _sessions(400)
    sessions[0]["is_cli_session"] = True
    models._apply_sidebar_state_db_overrides(sessions)
    assert sessions[0]["is_cli_session"] is False, (
        "A probed top-N row with a webui state.db source must be corrected"
    )
