/* GBAutomation data-plane panels (plan B9): Supabase / Langfuse / Kanban-Data.
 *
 * One workstream, three vanilla-JS panels sharing ONE renderer (the generic
 * TableCard / DataTable below) and ONE committed snapshot fixture
 * (/gbauto-supabase/snapshot.json + contracts.json, served by api/snapshot.py
 * from B0a). Ported from 9119's SupabaseIndexesPage (React) without the 9119
 * token scheme.
 *
 *   - Supabase (loadSupabaseData):  health pane (live /api/gbauto/supabase-health,
 *                                   degrades empty on PC) + all-tables grid from
 *                                   the snapshot.
 *   - Langfuse (loadLangfuseData):  4 stat cards + trace table. Live stats from
 *                                   /api/gbauto/langfuse (degrades on PC) with a
 *                                   fallback to the snapshot's langfuse_traces.
 *   - Kanban-Data (loadKanbanData): read-only table browser over agent_runs /
 *                                   prd_kanban_dispatch_links / kanban_tasks /
 *                                   agent_profiles / agent_profile_teams.
 *
 * Tenant filtering (REQUIRED, plan decision #6): snapshots are pre-scoped at
 * generation time; this is the client-side guard that keeps the operator hub
 * from surfacing rows outside the active tenant's client aliases. It reuses the
 * Overview panel's ported TENANT_CLIENT_ALIASES / client_slug model
 * (_overviewActiveTenant / _overviewTenantAllows, defined in panels.js).
 * Live paths are Mini-only (Supabase DNS-blocked from the PC).
 */
(function () {
  'use strict';

  // ── shared helpers (reuse panels.js globals; provide safe fallbacks) ──
  function esc(v) {
    if (typeof _ovEsc === 'function') return _ovEsc(v);
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function activeTenant(meta) {
    if (typeof _overviewActiveTenant === 'function') return _overviewActiveTenant(meta);
    return (meta && (meta.client_slug || meta.tenant)) || 'gbautomation';
  }
  function tenantAllows(tenant, client) {
    if (typeof _overviewTenantAllows === 'function') return _overviewTenantAllows(tenant, client);
    return true;
  }
  function relTime(value) {
    if (typeof _overviewRelativeTime === 'function') return _overviewRelativeTime(value);
    return value || '';
  }
  function fetchJson(url) {
    return fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function fmtNum(value) {
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    try { return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(n); }
    catch (_e) { return String(n); }
  }
  function fmtLabel(value) {
    return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, function (l) { return l.toUpperCase(); });
  }
  function fmtDate(value) {
    if (!value) return '-';
    var d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    try { return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(d); }
    catch (_e) { return String(value); }
  }
  function fmtValue(value, key) {
    key = key || '';
    if (value === null || value === undefined || value === '') return '-';
    if (typeof value === 'number') return fmtNum(value);
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (typeof value === 'object') return JSON.stringify(value);
    var text = String(value);
    if (key.indexOf('_at') !== -1 || key.indexOf('timestamp') !== -1) return fmtDate(text);
    return text.length > 120 ? (text.slice(0, 117) + '...') : text;
  }
  function statusTone(value) {
    var s = String(value == null ? '' : value).toLowerCase();
    if (['ok', 'pass', 'passed', 'done', 'completed', 'active', 'true', 'success'].indexOf(s) !== -1) return 'good';
    if (['fail', 'failed', 'error', 'blocked', 'timeout', 'false', 'cancelled', 'attention'].indexOf(s) !== -1) return 'bad';
    if (['running', 'pending', 'proposed', 'partial', 'skipped', 'draft', 'empty'].indexOf(s) !== -1) return 'warn';
    return '';
  }

  // Preferred columns per known table (ported from 9119 PRIMARY_COLUMNS).
  var PRIMARY_COLUMNS = {
    agent_runs: ['status', 'title', 'board_slug', 'profile', 'assignee', 'client_slug', 'repo_slug', 'source_updated_at'],
    agent_log_artifacts: ['agent', 'category', 'client_slug', 'repo_slug', 'modified_at', 'content_mode', 'basename'],
    kanban_tasks: ['title', 'status', 'kind', 'client_slug', 'created_at'],
    langfuse_traces: ['trace_name', 'agent', 'profile', 'runtime', 'trace_timestamp', 'latency_sec', 'total_cost', 'total_tokens'],
    prd_kanban_dispatch_links: ['prd_id', 'status', 'relationship', 'team_id', 'profile', 'task_id', 'run_id', 'created_at'],
    host_job_runs: ['job', 'status', 'host', 'started_at', 'duration_s'],
    tac_test_runs: ['suite', 'status', 'last_run_at', 'tests_total', 'tests_passed', 'tests_failed'],
    ecom_telemetry_backfills: ['client_slug', 'status', 'updated_at', 'rows_backfilled'],
    agent_profiles: ['name', 'status', 'role', 'tenant'],
    agent_profile_teams: ['team_id', 'display_name', 'orchestrator_profile', 'tenant'],
  };

  // ── tenant scoping over the snapshot ──
  function rowHasTenantMarker(row) {
    return row && (Object.prototype.hasOwnProperty.call(row, 'client_slug')
      || Object.prototype.hasOwnProperty.call(row, 'tenant')
      || Object.prototype.hasOwnProperty.call(row, 'invoked_by'));
  }
  function rowMatchesTenant(row, tenant) {
    if (!rowHasTenantMarker(row)) return true;
    var marker = row.client_slug != null ? row.client_slug
      : (row.tenant != null ? row.tenant
        : (row.invoked_by != null ? String(row.invoked_by).replace(/^tenant:/, '') : null));
    if (marker == null || marker === '') return true;
    return tenantAllows(tenant, String(marker).toLowerCase());
  }
  function countBy(rows, col) {
    var out = {};
    rows.forEach(function (r) { var k = String(r[col] == null ? 'unknown' : r[col]); out[k] = (out[k] || 0) + 1; });
    return out;
  }
  function recomputeSummary(table, rows) {
    var summary = { row_count: rows.length };
    if (table.summary && table.summary.latest !== undefined) summary.latest = table.summary.latest;
    if (table.time_col) {
      var vals = rows.map(function (r) { return r[table.time_col]; }).filter(Boolean).map(String).sort();
      summary.latest = vals.length ? vals[vals.length - 1] : null;
    }
    if (table.status_col) summary.status_counts = countBy(rows, table.status_col);
    if (table.group_col) summary.top_groups = countBy(rows, table.group_col);
    return summary;
  }
  function scopeTable(table, tenant) {
    var rows = Array.isArray(table.rows) ? table.rows : [];
    if (!rows.length || !rowHasTenantMarker(rows[0])) return table;
    var scoped = rows.filter(function (r) { return rowMatchesTenant(r, tenant); });
    var copy = {};
    for (var k in table) { if (Object.prototype.hasOwnProperty.call(table, k)) copy[k] = table[k]; }
    copy.rows = scoped;
    copy.summary = recomputeSummary(table, scoped);
    return copy;
  }
  function scopeSnapshot(snapshot, tenant) {
    if (!snapshot || !Array.isArray(snapshot.tables)) return snapshot;
    var copy = {};
    for (var k in snapshot) { if (Object.prototype.hasOwnProperty.call(snapshot, k)) copy[k] = snapshot[k]; }
    copy.tables = snapshot.tables.map(function (t) { return scopeTable(t, tenant); });
    return copy;
  }
  function getTable(snapshot, name) {
    if (!snapshot || !Array.isArray(snapshot.tables)) return null;
    for (var i = 0; i < snapshot.tables.length; i++) { if (snapshot.tables[i].name === name) return snapshot.tables[i]; }
    return null;
  }

  // ── generic renderer fragments ──
  function statCard(opts) {
    return '<article class="supabase-stat-card">'
      + '<strong>' + esc(opts.value) + '</strong>'
      + '<span>' + esc(opts.label) + '</span>'
      + (opts.sub ? '<small>' + esc(opts.sub) + '</small>' : '')
      + '</article>';
  }
  function valuePills(values) {
    var entries = Object.keys(values || {}).slice(0, 6);
    if (!entries.length) return '<span class="supabase-muted">No grouped values</span>';
    return '<div class="supabase-pill-row">' + entries.map(function (k) {
      return '<span class="supabase-chip ' + statusTone(k) + '">' + esc(fmtLabel(k)) + ' <b>' + esc(values[k]) + '</b></span>';
    }).join('') + '</div>';
  }
  function columnsFor(table, rows) {
    var row = rows[0];
    if (!row) return [];
    var keys = Object.keys(row);
    var pref = (PRIMARY_COLUMNS[table.name] || []).filter(function (k) { return keys.indexOf(k) !== -1; });
    var rest = keys.filter(function (k) { return pref.indexOf(k) === -1; });
    return pref.concat(rest).slice(0, 8);
  }
  function rowsFor(table, query) {
    var rows = Array.isArray(table.rows) ? table.rows : [];
    if (query) {
      var needle = query.toLowerCase();
      rows = rows.filter(function (r) { return JSON.stringify(r).toLowerCase().indexOf(needle) !== -1; });
    }
    return rows.slice(0, 10);
  }
  function dataTable(table, query) {
    var rows = rowsFor(table, query);
    var cols = columnsFor(table, rows);
    if (!rows.length) {
      return '<div class="supabase-empty">' + (table.error ? ('Unavailable: ' + esc(table.error)) : 'No matching rows in this snapshot.') + '</div>';
    }
    var head = '<tr>' + cols.map(function (c) { return '<th>' + esc(fmtLabel(c)) + '</th>'; }).join('') + '</tr>';
    var body = rows.map(function (r, ri) {
      return '<tr>' + cols.map(function (c) {
        var v = r[c];
        var isStatus = c.indexOf('status') !== -1 || c === 'active' || c === 'content_mode';
        return '<td>' + (isStatus
          ? '<span class="supabase-chip ' + statusTone(v) + '">' + esc(fmtValue(v, c)) + '</span>'
          : esc(fmtValue(v, c))) + '</td>';
      }).join('') + '</tr>';
    }).join('');
    return '<div class="supabase-table-wrap"><table><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
  }
  function contractCard(contract) {
    if (!contract) {
      return '<div class="supabase-contract-card is-empty"><span>Contract</span>'
        + '<strong>Not cataloged yet</strong>'
        + '<p>Add this object to the contracts index to expose owner, access, read path, and retention.</p></div>';
    }
    return '<div class="supabase-contract-card">'
      + '<div class="supabase-contract-topline"><span>' + esc(contract.object_type || 'object') + '</span>'
      + '<span>' + esc(contract.lifecycle || 'lifecycle tbd') + '</span></div>'
      + '<div class="supabase-contract-grid">'
      + '<span><b>Domain</b>' + esc(contract.domain || '-') + '</span>'
      + '<span><b>Owner</b>' + esc(contract.owner_agent || contract.owner || '-') + '</span>'
      + '<span><b>Access</b>' + esc(fmtLabel(contract.access_model || '-')) + '</span>'
      + '<span><b>Scoped</b>' + (contract.tenant_scoped ? 'yes' : 'no') + '</span>'
      + '</div>'
      + '<dl class="supabase-contract-paths">'
      + '<div><dt>Write path</dt><dd>' + esc(contract.write_path || '-') + '</dd></div>'
      + '<div><dt>Read path</dt><dd>' + esc(contract.read_path || '-') + '</dd></div>'
      + '<div><dt>Retention</dt><dd>' + esc(contract.retention_policy || '-') + '</dd></div>'
      + '</dl>'
      + (contract.notes ? '<p class="supabase-contract-note">' + esc(contract.notes) + '</p>' : '')
      + '</div>';
  }
  function tableCard(table, contract, query) {
    return '<article class="supabase-table-card">'
      + '<header><div><p class="gbhub-eyebrow">' + esc(table.dashboard || '') + '</p>'
      + '<h3>' + esc(table.label || table.name) + '</h3>'
      + '<p>' + esc(table.description || '') + '</p></div>'
      + '<span class="supabase-count' + (table.error ? ' is-bad' : '') + '">' + esc((table.rows || []).length) + '</span></header>'
      + '<div class="supabase-card-metrics"><span><b>' + esc((table.summary && table.summary.row_count) || 0) + '</b> rows</span>'
      + '<span><b>' + esc(fmtDate(table.summary && table.summary.latest)) + '</b> latest</span></div>'
      + contractCard(contract)
      + valuePills((table.summary && (table.summary.status_counts || table.summary.top_groups)) || {})
      + dataTable(table, query)
      + '</article>';
  }
  function heroBadge(snapshot, tenant, title, eyebrow, desc) {
    var meta = (snapshot && snapshot._meta) || {};
    var stamp = snapshot && (snapshot.generated_at || meta.generated_at);
    var live = snapshot && snapshot.live;
    return '<section class="gbhub-hero">'
      + '<div class="gbhub-brand-row"><span class="gbhub-mark">gb</span><span>' + esc(eyebrow) + '</span></div>'
      + '<span class="gbhub-badge">' + (live ? 'live snapshot' : 'sample snapshot') + '</span>'
      + '<span class="gbhub-badge">scope: ' + esc(tenant) + '</span>'
      + '<h2>' + esc(title) + '</h2>'
      + '<p>' + esc(desc) + '</p>'
      + '<small class="gbhub-tenant">Tenant scope: <strong>' + esc(tenant) + '</strong>'
      + (stamp ? ' &middot; snapshot ' + esc(relTime(stamp) || stamp) : '')
      + '</small>'
      + '</section>';
  }

  // ── snapshot + contracts loader (shared, cached) ──
  var _snapshot = null;
  var _contracts = null;
  var _loadedOnce = false;
  function contractMap() {
    var map = {};
    var list = (_contracts && Array.isArray(_contracts.contracts)) ? _contracts.contracts : [];
    list.forEach(function (c) { if (c && c.table) map[c.table] = c; });
    return map;
  }
  function ensureSnapshot() {
    if (_loadedOnce) return Promise.resolve();
    return Promise.all([
      fetchJson('/gbauto-supabase/snapshot.json'),
      fetchJson('/gbauto-supabase/contracts.json'),
    ]).then(function (res) {
      _snapshot = res[0];
      _contracts = res[1];
      _loadedOnce = true;
    });
  }

  // ── Supabase health pane ──
  function healthPane(health, tenant) {
    var summary = (health && health.summary) || {};
    var relations = (health && Array.isArray(health.relations)) ? health.relations : [];
    var note = '';
    if (!health || health.available === false) {
      note = '<p class="supabase-muted">Live health is Mini-only (gbauto-supabase CLI not on PATH here). Showing the committed snapshot below.</p>';
    } else if (health.ok === false && health.error) {
      note = '<p class="supabase-muted">Health query failed: ' + esc(health.error) + '</p>';
    }
    var rowsHtml = relations.map(function (r) {
      return '<tr><td>' + esc(r.relation) + '</td><td>' + esc(fmtLabel(r.family)) + '</td>'
        + '<td>' + esc(fmtLabel(r.kind)) + '</td>'
        + '<td><span class="supabase-chip ' + statusTone(r.status) + '">' + esc(r.status) + '</span></td>'
        + '<td>' + esc(fmtNum(r.rows)) + '</td><td>' + esc(fmtNum(r.bad_rows)) + '</td>'
        + '<td>' + esc(fmtDate(r.latest)) + '</td></tr>';
    }).join('');
    var table = relations.length
      ? '<div class="supabase-table-wrap"><table><thead><tr><th>Relation</th><th>Family</th><th>Kind</th><th>Status</th><th>Rows</th><th>Bad</th><th>Latest</th></tr></thead><tbody>' + rowsHtml + '</tbody></table></div>'
      : '';
    return '<section class="supabase-health-pane">'
      + '<div class="supabase-health-head"><div><p class="gbhub-eyebrow">Tenant Data Health</p>'
      + '<h3>' + esc(tenant) + ' Supabase surface</h3></div>'
      + '<div class="supabase-health-summary">'
      + '<span><b>' + esc(fmtNum(summary.total_rows || 0)) + '</b> scoped rows</span>'
      + '<span><b>' + esc(summary.relations || 0) + '</b> relations</span>'
      + '<span><b>' + esc(summary.empty_relations || 0) + '</b> empty</span>'
      + '<span><b>' + esc(summary.attention_relations || 0) + '</b> attention</span>'
      + '<span><b>' + esc(summary.bad_rows || 0) + '</b> bad rows</span>'
      + '</div></div>'
      + note + table
      + '</section>';
  }

  function renderSupabase(snapshot, tenant, health) {
    var container = document.getElementById('supabaseContent');
    if (!container) return;
    if (!snapshot) {
      container.innerHTML = heroBadge(null, tenant, 'Supabase Indexes', 'GBAutomation Data', 'Snapshot unavailable.')
        + '<section class="gbhub-empty-state">Supabase snapshot unavailable.</section>';
      return;
    }
    var cmap = contractMap();
    var tables = snapshot.tables || [];
    var rowsLoaded = tables.reduce(function (n, t) { return n + ((t.rows || []).length); }, 0);
    var unavailable = tables.filter(function (t) { return t.error; }).length;
    var stats = [
      statCard({ label: 'Tables', sub: Object.keys(snapshot.dashboards || {}).length + ' dashboard groups', value: tables.length }),
      statCard({ label: 'Rows Loaded', sub: 'cap ' + (snapshot.row_limit || '-') + ' per table', value: rowsLoaded }),
      statCard({ label: 'Unavailable', sub: 'schema / PostgREST gap', value: unavailable }),
      statCard({ label: 'Relations', sub: 'tenant health probe', value: (health && health.summary && health.summary.relations) || '-' }),
    ].join('');
    var cards = tables.map(function (t) { return tableCard(t, cmap[t.name], ''); }).join('');
    container.innerHTML = heroBadge(snapshot, tenant, 'Supabase Indexes', 'GBAutomation Data',
        'A snapshot of the GBAutomation Supabase indexes: operations, Kanban, observability, TAC quality, and ecom intelligence.')
      + healthPane(health, tenant)
      + '<section class="supabase-stat-grid">' + stats + '</section>'
      + '<section class="supabase-table-grid">' + (cards || '<section class="gbhub-empty-state">No tables in scope.</section>') + '</section>';
  }

  function renderLangfuse(snapshot, tenant, live) {
    var container = document.getElementById('langfuseContent');
    if (!container) return;
    var cmap = contractMap();
    var traces = getTable(snapshot, 'langfuse_traces');
    var artifacts = getTable(snapshot, 'agent_log_artifacts');
    var snapRows = (traces && traces.rows) || [];
    // Prefer the live bridge summary when available; else compute from snapshot.
    var useLive = live && live.available !== false && live.ok !== false && Array.isArray(live.rows) && live.rows.length;
    var summary;
    if (useLive) {
      summary = live.summary || {};
    } else {
      var cost = snapRows.reduce(function (s, r) { return s + (Number(r.total_cost) || 0); }, 0);
      var tokens = snapRows.reduce(function (s, r) { return s + (Number(r.total_tokens) || 0); }, 0);
      var agents = {};
      snapRows.forEach(function (r) { if (r.agent) agents[r.agent] = 1; });
      summary = { trace_count: snapRows.length, agent_count: Object.keys(agents).length, total_tokens: tokens, total_cost: cost };
    }
    var srcNote = useLive
      ? '<p class="supabase-muted">Live traces from /api/gbauto/langfuse (last ' + esc(live.days) + ' days).</p>'
      : '<p class="supabase-muted">Live trace bridge is Mini-only here; stats + table from the committed snapshot.</p>';
    var stats = [
      statCard({ label: 'Mirrored Traces', sub: (snapshot && snapshot.window_days != null ? snapshot.window_days + ' day window' : 'window'), value: summary.trace_count || 0 }),
      statCard({ label: 'Agents', sub: 'distinct trace agents', value: summary.agent_count || 0 }),
      statCard({ label: 'Tokens', sub: 'input + output', value: fmtNum(summary.total_tokens || 0) }),
      statCard({ label: 'Cost', sub: 'mirrored trace total', value: '$' + (Number(summary.total_cost || 0)).toFixed(4) }),
    ].join('');
    var cards = '';
    if (traces) cards += tableCard(traces, cmap[traces.name], '');
    if (artifacts) cards += tableCard(artifacts, cmap[artifacts.name], '');
    container.innerHTML = heroBadge(snapshot, tenant, 'Langfuse Trace Index', 'GBAutomation Observability',
        'Sanitized Langfuse trace metadata mirrored into Supabase, with cost, token, and agent rollups.')
      + srcNote
      + '<section class="supabase-stat-grid">' + stats + '</section>'
      + '<section class="supabase-table-grid focused">' + (cards || '<section class="gbhub-empty-state">No traces in scope.</section>') + '</section>';
  }

  function renderKanban(snapshot, tenant) {
    var container = document.getElementById('kanbandataContent');
    if (!container) return;
    var cmap = contractMap();
    var names = ['agent_runs', 'prd_kanban_dispatch_links', 'kanban_tasks', 'agent_profiles', 'agent_profile_teams'];
    var picked = names.map(function (n) { return getTable(snapshot, n); }).filter(Boolean);
    var agentRuns = getTable(snapshot, 'agent_runs');
    var dispatch = getTable(snapshot, 'prd_kanban_dispatch_links');
    var teams = getTable(snapshot, 'agent_profile_teams');
    var runRows = (agentRuns && agentRuns.rows) || [];
    var boards = {};
    runRows.forEach(function (r) { if (r.board_slug) boards[r.board_slug] = 1; });
    var stats = [
      statCard({ label: 'Agent Runs', sub: 'Hermes board mirror', value: runRows.length }),
      statCard({ label: 'Boards', sub: 'distinct board slugs', value: Object.keys(boards).length }),
      statCard({ label: 'Dispatch Links', sub: 'PRD to task/run', value: (dispatch && dispatch.rows.length) || 0 }),
      statCard({ label: 'Profile Teams', sub: 'agent_profile_teams', value: (teams && teams.rows.length) || 0 }),
    ].join('');
    var cards = picked.map(function (t) { return tableCard(t, cmap[t.name], ''); }).join('');
    container.innerHTML = heroBadge(snapshot, tenant, 'Kanban Data Plane', 'GBAutomation Control Plane',
        'Hermes Kanban runs, PRD dispatch links, tasks, and profile-team routing data from the Supabase index snapshot. Read-only; the board itself lives in the Kanban panel.')
      + '<section class="supabase-stat-grid">' + stats + '</section>'
      + '<section class="supabase-table-grid">' + (cards || '<section class="gbhub-empty-state">No Kanban data in scope.</section>') + '</section>';
  }

  // ── public load entrypoints (called from panels.js switchPanel) ──
  function tenantNow() {
    var meta = (_snapshot && _snapshot._meta) || {};
    return activeTenant(meta);
  }

  window.loadSupabaseData = function (force) {
    var container = document.getElementById('supabaseContent');
    if (!container) return;
    if (window._supabaseDataLoaded && !force) return;
    ensureSnapshot().then(function () {
      var tenant = tenantNow();
      var scoped = scopeSnapshot(_snapshot, tenant);
      renderSupabase(scoped, tenant, null);
      window._supabaseDataLoaded = true;
      // Live health probe (degrades to empty on the PC).
      fetchJson('/api/gbauto/supabase-health?tenant=' + encodeURIComponent(tenant)).then(function (health) {
        renderSupabase(scoped, tenant, health);
      });
    });
  };

  window.loadLangfuseData = function (force) {
    var container = document.getElementById('langfuseContent');
    if (!container) return;
    if (window._langfuseDataLoaded && !force) return;
    ensureSnapshot().then(function () {
      var tenant = tenantNow();
      var scoped = scopeSnapshot(_snapshot, tenant);
      renderLangfuse(scoped, tenant, null);
      window._langfuseDataLoaded = true;
      fetchJson('/api/gbauto/langfuse?tenant=' + encodeURIComponent(tenant)).then(function (live) {
        renderLangfuse(scoped, tenant, live);
      });
    });
  };

  window.loadKanbanData = function (force) {
    var container = document.getElementById('kanbandataContent');
    if (!container) return;
    if (window._kanbanDataLoaded && !force) return;
    ensureSnapshot().then(function () {
      var tenant = tenantNow();
      var scoped = scopeSnapshot(_snapshot, tenant);
      renderKanban(scoped, tenant);
      window._kanbanDataLoaded = true;
    });
  };
})();
