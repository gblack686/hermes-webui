/*
 * gbauto-page-registry.js — GBauto Unified Shell page registry
 * ------------------------------------------------------------------
 * Plan: second-brain/plans/2026-07-01-hermes-webui-unified-gbauto-shell-tac-plan.html
 * Approval: docs/rfcs/gbauto-webui-merge-approval.yaml
 *
 * This registry is DESCRIPTIVE, not a nav rebuilder. The native WebUI rail /
 * sidebar-nav DOM (and the heavily test-guarded external "Hermes Dashboard"
 * link) are left exactly as they ship. The registry is the durable owner map
 * for every native (8787) page and every ported/deferred 9119 page, and it
 * drives:
 *   - /home route-strip + gallery card availability (hide/disable unported).
 *   - a single source of truth for target_route -> owner_panel switching.
 *   - a unique-target-route invariant that the Phase 2.3 test asserts.
 *
 * Schema per plan Phase 2.1:
 *   id, label, source_app, source_route, target_route, owner_panel,
 *   priority, status, data_contract, validation
 *
 * status vocabulary:
 *   keep_native   native 8787 panel, first-class, must not regress
 *   adopt_native  9119 page whose behavior native already covers (superset)
 *   ported        9119 page reimplemented natively in this shell
 *   deferred      9119 page tracked but not yet ported (hidden on /home)
 *   external_link the 9119 dashboard link itself (controlled, not a panel)
 */
(function (global) {
  'use strict';

  var REGISTRY = [
    /* -------- native 8787 panels: keep every one -------- */
    { id: 'chat', label: 'Chat', source_app: 'webui', source_route: '/chat', target_route: '/chat', owner_panel: 'chat', priority: 'p0', status: 'keep_native', data_contract: 'native chat/session APIs', validation: 'native panel opens' },
    { id: 'tasks', label: 'Tasks', source_app: 'webui', source_route: '/tasks', target_route: '/tasks', owner_panel: 'tasks', priority: 'p0', status: 'keep_native', data_contract: 'native todo/task state', validation: 'native panel opens' },
    { id: 'kanban', label: 'Kanban', source_app: 'webui', source_route: '/kanban', target_route: '/kanban', owner_panel: 'kanban', priority: 'p0', status: 'keep_native', data_contract: '/api/kanban/*', validation: 'native panel opens' },
    { id: 'skills', label: 'Skills', source_app: 'webui', source_route: '/skills', target_route: '/skills', owner_panel: 'skills', priority: 'p1', status: 'keep_native', data_contract: 'native skills catalog', validation: 'native panel opens' },
    { id: 'memory', label: 'Memory', source_app: 'webui', source_route: '/memory', target_route: '/memory', owner_panel: 'memory', priority: 'p1', status: 'keep_native', data_contract: 'native memory API', validation: 'native panel opens' },
    { id: 'workspaces', label: 'Spaces', source_app: 'webui', source_route: '/workspaces', target_route: '/workspaces', owner_panel: 'workspaces', priority: 'p1', status: 'keep_native', data_contract: '/api/workspaces', validation: 'native panel opens' },
    { id: 'profiles', label: 'Profiles', source_app: 'webui', source_route: '/profiles', target_route: '/profiles', owner_panel: 'profiles', priority: 'p1', status: 'keep_native', data_contract: 'native profiles API', validation: 'native panel opens' },
    { id: 'todos', label: 'Todos', source_app: 'webui', source_route: '/todos', target_route: '/todos', owner_panel: 'todos', priority: 'p1', status: 'keep_native', data_contract: 'native todo state', validation: 'native panel opens' },
    { id: 'insights', label: 'Insights', source_app: 'webui', source_route: '/insights', target_route: '/insights', owner_panel: 'insights', priority: 'p1', status: 'keep_native', data_contract: 'native insights', validation: 'native panel opens' },
    { id: 'logs', label: 'Logs', source_app: 'webui', source_route: '/logs', target_route: '/logs', owner_panel: 'logs', priority: 'p1', status: 'keep_native', data_contract: 'native logs', validation: 'native panel opens' },
    { id: 'settings', label: 'Settings', source_app: 'webui', source_route: '/settings', target_route: '/settings', owner_panel: 'settings', priority: 'p2', status: 'keep_native', data_contract: 'native settings/config', validation: 'native panel opens' },

    /* -------- GBauto home: ported approval example (Phase 3) -------- */
    { id: 'home', label: 'Home', source_app: 'dashboard-9119', source_route: '/home', target_route: '/home', owner_panel: 'home', priority: 'p0', status: 'ported', data_contract: 'static (composer wired to native chat)', validation: 'browser smoke /home' },

    /* -------- 9119 P0 pages -------- */
    { id: 'sessions', label: 'Sessions', source_app: 'dashboard-9119', source_route: '/sessions', target_route: '/sessions', owner_panel: 'chat', priority: 'p0', status: 'adopt_native', data_contract: '/api/sessions, /api/sessions/search (native superset)', validation: 'native session sidebar + search' },
    { id: 'artifacts', label: 'Artifacts', source_app: 'dashboard-9119', source_route: '/artifacts', target_route: '/artifacts', owner_panel: 'artifacts', priority: 'p0', status: 'ported', data_contract: '/api/gbauto/artifacts (server-backed index)', validation: 'artifacts panel lists + previews' },

    /* -------- 9119 P1/P2/P3 pages: tracked, deferred (hidden on /home) -------- */
    { id: 'overview', label: 'Overview', source_app: 'dashboard-9119', source_route: '/overview', target_route: '/overview', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server snapshot JSON', validation: 'n/a until ported' },
    { id: 'repos', label: 'Repos', source_app: 'dashboard-9119', source_route: '/repos', target_route: '/repos', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server snapshot JSON', validation: 'n/a until ported' },
    { id: 'lineage', label: 'Lineage', source_app: 'dashboard-9119', source_route: '/lineage', target_route: '/lineage', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server snapshot JSON', validation: 'n/a until ported' },
    { id: 'supabase', label: 'Supabase', source_app: 'dashboard-9119', source_route: '/supabase', target_route: '/supabase', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server-backed snapshot (no browser reads)', validation: 'n/a until ported' },
    { id: 'langfuse', label: 'Langfuse', source_app: 'dashboard-9119', source_route: '/langfuse', target_route: '/langfuse', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server-backed snapshot', validation: 'n/a until ported' },
    { id: 'kanban-data', label: 'Kanban Data', source_app: 'dashboard-9119', source_route: '/kanban-data', target_route: '/kanban-data', owner_panel: null, priority: 'p1', status: 'deferred', data_contract: 'server route reading snapshots', validation: 'n/a until ported' },
    { id: 'analytics', label: 'Analytics', source_app: 'dashboard-9119', source_route: '/analytics', target_route: '/analytics', owner_panel: null, priority: 'p2', status: 'deferred', data_contract: 'server snapshot JSON', validation: 'n/a until ported' },
    { id: 'models', label: 'Models', source_app: 'dashboard-9119', source_route: '/models', target_route: '/models', owner_panel: null, priority: 'p2', status: 'deferred', data_contract: 'merge with settings model picker', validation: 'n/a until ported' },
    { id: 'cron', label: 'Cron', source_app: 'dashboard-9119', source_route: '/cron', target_route: '/cron', owner_panel: null, priority: 'p2', status: 'deferred', data_contract: 'server route', validation: 'n/a until ported' },
    { id: 'plugins', label: 'Plugins', source_app: 'dashboard-9119', source_route: '/plugins', target_route: '/plugins', owner_panel: null, priority: 'p2', status: 'deferred', data_contract: 'native extension/plugin support', validation: 'n/a until ported' },
    { id: 'config', label: 'Config', source_app: 'dashboard-9119', source_route: '/config', target_route: '/config', owner_panel: 'settings', priority: 'p2', status: 'deferred', data_contract: 'merge with 8787 settings', validation: 'n/a until ported' },
    { id: 'env', label: 'Keys', source_app: 'dashboard-9119', source_route: '/env', target_route: '/env', owner_panel: null, priority: 'p2', status: 'deferred', data_contract: 'server route (secret-safe)', validation: 'n/a until ported' },
    { id: 'docs', label: 'Documentation', source_app: 'dashboard-9119', source_route: '/docs', target_route: '/docs', owner_panel: null, priority: 'p3', status: 'deferred', data_contract: 'static docs index', validation: 'n/a until ported' },

    /* -------- controlled external dashboard link (NOT a panel) -------- */
    { id: 'dashboard-9119-link', label: 'Hermes Dashboard', source_app: 'dashboard-9119', source_route: '/', target_route: 'external:9119', owner_panel: null, priority: 'p3', status: 'external_link', data_contract: '/api/dashboard/status', validation: 'test_dashboard_link_ui + test_dashboard_probe' }
  ];

  var AVAILABLE_STATUSES = { keep_native: true, adopt_native: true, ported: true };

  function byId(id) {
    for (var i = 0; i < REGISTRY.length; i++) { if (REGISTRY[i].id === id) return REGISTRY[i]; }
    return null;
  }
  function byTarget(route) {
    for (var i = 0; i < REGISTRY.length; i++) { if (REGISTRY[i].target_route === route) return REGISTRY[i]; }
    return null;
  }

  // A route is "available" in this shell when it maps to a real, active panel:
  // status is available AND (no owner panel required OR the owner panel's nav
  // button actually exists in the DOM). Used by /home to hide unported cards.
  function isRouteAvailable(routeOrId) {
    var entry = byTarget(routeOrId) || byId(routeOrId);
    if (!entry) return false;
    if (!AVAILABLE_STATUSES[entry.status]) return false;
    if (!entry.owner_panel) return true;
    if (typeof document === 'undefined') return true;
    return !!document.querySelector('[data-panel="' + entry.owner_panel + '"]');
  }

  // Resolve a target route to its owner panel name (for switchPanel), or null.
  function ownerPanelFor(routeOrId) {
    var entry = byTarget(routeOrId) || byId(routeOrId);
    return entry ? entry.owner_panel : null;
  }

  // Phase 2.3 invariant: every registry item has a unique, non-empty target
  // route. Returns { ok, duplicates:[...], missing:[...] } for the test + a
  // console warning at load so drift is loud in dev.
  function validate() {
    var seen = {}, duplicates = [], missing = [];
    for (var i = 0; i < REGISTRY.length; i++) {
      var t = REGISTRY[i].target_route;
      if (!t) { missing.push(REGISTRY[i].id); continue; }
      if (seen[t]) duplicates.push(t); else seen[t] = true;
    }
    return { ok: duplicates.length === 0 && missing.length === 0, duplicates: duplicates, missing: missing };
  }

  var api = {
    REGISTRY: REGISTRY,
    all: function () { return REGISTRY.slice(); },
    byId: byId,
    byTarget: byTarget,
    isRouteAvailable: isRouteAvailable,
    ownerPanelFor: ownerPanelFor,
    validate: validate
  };

  global.GBAUTO_PAGE_REGISTRY = api;

  try {
    var v = validate();
    if (!v.ok && global.console && console.warn) {
      console.warn('[gbauto-page-registry] non-unique/empty target routes', v);
    }
  } catch (_) { /* non-fatal */ }

  if (typeof module !== 'undefined' && module.exports) { module.exports = api; }
})(typeof window !== 'undefined' ? window : this);
