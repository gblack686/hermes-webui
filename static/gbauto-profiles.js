/* GBAutomation Profiles Catalog panel (plan B12): Teams / Index / TAC + drill-downs.
 *
 * Native vanilla-JS port of 9119's ProfilesPage React catalog (the GBAuto-
 * proprietary Teams/Index/TAC views + profile/team detail pages). The admin
 * view (list/create/rename/delete) is already native in webui's existing
 * `profiles` panel and is intentionally NOT duplicated here.
 *
 * Data plane
 * ----------
 *   - Catalog: committed snapshot fixture (/agent-profiles/catalog.json, served
 *     by api/snapshot.py from B0a) renders first so the panel works offline on
 *     the PC. Then the live tenant-scoped endpoint /api/gbauto/agent-profiles
 *     (api/gbauto_agent_profiles.py) is probed; when it returns available=true
 *     (Mini-only -- Supabase is DNS-blocked from the PC) it replaces the fixture.
 *   - Runtime evidence: /api/logs/supabase/timeline (B9, already merged) read
 *     for agent_runs, scoped to the active tenant + the drilled profile/team.
 *   - Profile art + reports: /profile-art/<id>.jpg and /profile-reports/<id>/...
 *     served by api/snapshot.py (B12 prefixes). Art degrades to a hued tile.
 *
 * Tenant filtering (REQUIRED, plan decision #6): the live endpoint is tenant-
 * scoped at QUERY time; the fixture is pre-scoped at generation time. The active
 * tenant is taken from the catalog payload's _meta/tenant (mirrors the gbauto-
 * data panels' _overviewActiveTenant model). Live paths are Mini-only.
 */
(function () {
  'use strict';

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
  function relTime(value) {
    if (typeof _overviewRelativeTime === 'function') return _overviewRelativeTime(value);
    return value || '';
  }
  function fetchJson(url) {
    return fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  function fmtDate(value) {
    if (!value) return 'No recent run';
    var d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    try { return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(d); }
    catch (_e) { return String(value); }
  }

  // ── module state ──
  var _catalog = null;        // last rendered catalog payload (fixture or live)
  var _view = 'teams';        // 'teams' | 'index' | 'tac'
  var _drill = null;          // {kind:'team'|'profile', id:'...'} or null
  var _loaded = false;
  var _evidenceCache = {};     // search-key -> rows (per session)

  // Embedded TAC Lead variants (small, stable subset of 9119's gbautoLibrary;
  // the heavy generated library is intentionally not ported -- catalog rosters
  // come from Supabase/the fixture instead).
  var TAC_LEAD_VARIANTS = [
    { id: 'tac-lead-north-star', title: 'North Star Advisor', modeId: 'agentic_engineering_advisor', profileId: 'tac-lead',
      summary: 'Staff-level TAC strategy, zero-touch engineering guidance, and architecture tradeoffs.',
      emphasis: 'Use when the question is still strategic and the route is not clear yet.' },
    { id: 'tac-lead-component-retrieval', title: 'Component Retrieval', modeId: 'tac_component_retrieval', profileId: 'tac-lead',
      summary: 'Find real TAC components, skills, prompts, hooks, agents, and repo patterns before design.',
      emphasis: 'Use when a recommendation needs evidence from actual reusable assets.' },
    { id: 'tac-lead-dispatch', title: 'Team Dispatch', modeId: 'tac_team_dispatch', profileId: 'tac-lead',
      summary: 'Convert approved plans into Hermes Kanban work for tac-director and the TAC build team.',
      emphasis: 'Use after the PRD/spec is clear and operator approval exists.' },
  ];

  // Profiles that have a committed individual HTML report fixture (B12).
  var PROFILE_REPORTS = {
    'tac-lead': {
      title: 'TAC Lead Profile Report',
      description: 'Structured GBauto report with KPI ranks, skill affinity, handoff map, full prompt, and current Hermes profile YAML.',
      publicPath: '/profile-reports/tac-lead/index.html',
      summaryPath: '/profile-reports/tac-lead/data-summary.json',
    },
  };

  function tenantOf(catalog) {
    var meta = (catalog && catalog._meta) || {};
    return (catalog && catalog.tenant) || activeTenant(meta);
  }
  function teams(catalog) { return (catalog && Array.isArray(catalog.teams)) ? catalog.teams : []; }
  function profiles(catalog) { return (catalog && Array.isArray(catalog.profiles)) ? catalog.profiles : []; }
  function routes(catalog) { return (catalog && Array.isArray(catalog.routes)) ? catalog.routes : []; }

  function profileById(catalog, id) {
    return profiles(catalog).find(function (p) { return p.profile_id === id; }) || null;
  }
  function teamById(catalog, id) {
    return teams(catalog).find(function (tm) { return tm.team_id === id; }) || null;
  }
  function profilesForTeam(catalog, team) {
    if (!team) return [];
    var spec = team.specialist_profiles || [];
    var existing = team.existing_specialist_profiles || [];
    return profiles(catalog).filter(function (p) {
      return p.team_id === team.team_id
        || p.profile_id === team.orchestrator_profile
        || p.profile_id === team.lead_profile
        || spec.indexOf(p.profile_id) !== -1
        || existing.indexOf(p.profile_id) !== -1;
    });
  }
  function routesForTeam(catalog, team) {
    if (!team) return [];
    return routes(catalog).filter(function (r) {
      var metaTeam = r.metadata && r.metadata.team_id;
      return r.source_path === team.source_path || metaTeam === team.team_id || r.target_team_id === team.team_id;
    });
  }
  function isCoverage(team) { return !!(team && team.metadata && team.metadata.team_kind === 'coverage'); }
  function isTelegramFleet(team) { return !!(team && team.metadata && team.metadata.deployed_telegram_fleet === true); }

  function routeTargetLabel(r) {
    var target = r.target_profile_id || r.target_team_id || r.target_profile_key || 'unassigned';
    var policy = String((r.metadata && r.metadata.route_policy) || '').toLowerCase();
    var name = String(r.route_name || '').toLowerCase();
    var toTac = target === 'carlos' && (policy.indexOf('tac') !== -1 || /coding|build|repo|deploy/.test(name));
    return toTac ? 'carlos → TAC' : target;
  }

  // ── art ──
  function hashSeed(value) {
    var hash = 0; value = String(value || '');
    for (var i = 0; i < value.length; i++) hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
    return hash;
  }
  function artBlock(id, name, compact) {
    var hue = hashSeed(id) % 360;
    var cls = 'pcat-art' + (compact ? ' is-compact' : '');
    var fallback = '<div class="pcat-art pcat-art-fallback' + (compact ? ' is-compact' : '') + '" role="img" '
      + 'aria-label="' + esc(name) + ' art" style="--pcat-hue:' + hue + 'deg">'
      + '<span>' + esc(name) + '</span></div>';
    // Try the committed/served art; swap to the hued fallback on error.
    return '<img class="' + cls + '" loading="lazy" alt="' + esc(name) + '" '
      + 'src="/profile-art/' + esc(id) + '.jpg" '
      + 'onerror="this.outerHTML=' + esc(JSON.stringify(fallback)) + '">';
  }

  // ── runtime evidence (agent_runs timeline) ──
  // Reads the supabase-logs timeline mirror when present. webui only exposes a
  // subset of the gbauto supabase-logs surface today, so a missing endpoint (or
  // a PC without the CLI) degrades to an honest "unavailable" note rather than a
  // misleading zero-count panel. Live timeline is Mini-only (mini_pending).
  function loadEvidence(tenant, searchKey, mountId) {
    var url = '/api/logs/supabase/timeline?source=agent_runs&days=14&limit=200'
      + '&client=' + encodeURIComponent(tenant)
      + (searchKey ? '&search=' + encodeURIComponent(searchKey) : '');
    function unavailable() {
      var mount = document.getElementById(mountId);
      if (!mount) return;
      mount.innerHTML = ''
        + '<div class="pcat-evidence-head"><span class="gbhub-eyebrow">Supabase Runtime Evidence</span>'
        + '<h3>Client-scoped profile activity</h3>'
        + '<p class="gbhub-muted">Scope: <strong>' + esc(tenant) + '</strong>. The <code>agent_runs</code> timeline mirror is not available here &mdash; live runtime evidence is Mini-only.</p></div>';
    }
    function render(rows) {
      var mount = document.getElementById(mountId);
      if (!mount) return;
      rows = Array.isArray(rows) ? rows : [];
      var failures = rows.filter(function (r) {
        var s = String(r.status_family || r.status || '').toLowerCase();
        return ['failed', 'error', 'blocked'].indexOf(s) !== -1;
      }).length;
      var traced = rows.filter(function (r) { return !!r.trace_id; }).length;
      var latest = '';
      rows.forEach(function (r) {
        var raw = r.started_at || r.source_updated_at || r.created_at || '';
        if (raw && (!latest || new Date(raw) > new Date(latest))) latest = raw;
      });
      var pct = rows.length ? Math.round((traced / rows.length) * 100) : 0;
      mount.innerHTML = ''
        + '<div class="pcat-evidence-head"><span class="gbhub-eyebrow">Supabase Runtime Evidence</span>'
        + '<h3>Client-scoped profile activity</h3>'
        + '<p class="gbhub-muted">Scope: <strong>' + esc(tenant) + '</strong>. Read from <code>agent_runs</code> via the timeline mirror (B9).</p></div>'
        + '<div class="pcat-evidence-grid">'
        + '<section><span class="gbhub-eyebrow">Runs (14d)</span><strong>' + rows.length + '</strong><p class="gbhub-muted">' + esc(fmtDate(latest)) + '</p></section>'
        + '<section><span class="gbhub-eyebrow">Failures</span><strong>' + failures + '</strong><p class="gbhub-muted">' + (rows.length ? (Math.max(rows.length - failures, 0) + ' non-failing') : 'No rows for this scope.') + '</p></section>'
        + '<section><span class="gbhub-eyebrow">Trace links</span><strong>' + traced + '</strong><p class="gbhub-muted">' + (rows.length ? (pct + '% have trace ids') : 'No trace candidates.') + '</p></section>'
        + '</div>';
    }
    var ck = searchKey || '*';
    if (Object.prototype.hasOwnProperty.call(_evidenceCache, ck)) {
      var cached = _evidenceCache[ck];
      if (cached === null) unavailable(); else render(cached);
      return;
    }
    fetchJson(url).then(function (resp) {
      // null => endpoint unavailable; {rows:[...]} => available (maybe empty).
      var rows = (resp && Array.isArray(resp.rows)) ? resp.rows : (resp ? [] : null);
      _evidenceCache[ck] = rows;
      if (rows === null) unavailable(); else render(rows);
    });
  }

  // ── sub-nav ──
  function subnav(catalog) {
    var t = teams(catalog).length, p = profiles(catalog).length;
    function btn(view, label) {
      var active = (_view === view && !_drill) ? ' pcat-tab--active' : '';
      return '<button type="button" class="pcat-tab' + active + '" onclick="profilesCatalogSetView(\'' + view + '\')">' + esc(label) + '</button>';
    }
    return '<nav class="pcat-subnav" aria-label="Profile catalog views">'
      + btn('teams', 'Teams (' + t + ')')
      + btn('index', 'Index (' + p + ')')
      + btn('tac', 'TAC')
      + '</nav>';
  }

  // ── views ──
  function routeMap(catalog, team, compact, max) {
    var rs = routesForTeam(catalog, team);
    var seen = {}; rs = rs.filter(function (r) {
      var k = r.route_name + ':' + r.target_type + ':' + r.target_profile_id + ':' + r.target_team_id;
      if (seen[k]) return false; seen[k] = 1; return true;
    });
    if (!rs.length) return compact ? '' : '<div class="pcat-routemap"><span class="gbhub-eyebrow">Routes</span><p class="gbhub-muted">No Supabase route rows indexed for this team yet.</p></div>';
    var shown = max ? rs.slice(0, max) : rs;
    var rows = shown.map(function (r) {
      var target = routeTargetLabel(r);
      var tacCls = target.indexOf('TAC') !== -1 ? ' pcat-route--tac' : '';
      return '<div class="pcat-route' + tacCls + '"><span>' + esc(String(r.route_name).replace(/_/g, ' ')) + '</span>'
        + '<span class="pcat-route-arrow" aria-hidden="true">→</span><span>' + esc(target) + '</span></div>';
    }).join('');
    var more = rs.length - shown.length;
    return '<div class="pcat-routemap"><div class="pcat-routemap-head"><span class="gbhub-eyebrow">Route Map</span>'
      + '<span class="gbhub-badge">' + rs.length + ' routes</span></div>'
      + '<div class="pcat-route-flow">' + rows + (more > 0 ? '<div class="pcat-route pcat-route--more"><span>' + more + ' more</span></div>' : '') + '</div></div>';
  }

  function teamCard(catalog, team) {
    var members = profilesForTeam(catalog, team);
    var roster = members.slice(0, 8).map(function (p) { return '<span>' + esc(p.display_name || p.profile_id) + '</span>'; }).join('');
    return '<button type="button" class="pcat-card" onclick="profilesCatalogDrill(\'team\',\'' + esc(team.team_id) + '\')">'
      + '<div class="pcat-card-topline"><span>' + esc(team.tenant || 'gbauto Core') + '</span>'
      + '<span>' + members.length + ' profiles / ' + (routesForTeam(catalog, team).length) + ' routes</span></div>'
      + '<h3>' + esc(team.display_name) + '</h3>'
      + '<p class="gbhub-muted">' + esc(team.purpose || '') + '</p>'
      + '<div class="pcat-roster">' + roster
      + (isCoverage(team) ? '<span class="pcat-flag">coverage</span>' : '')
      + (isTelegramFleet(team) ? '<span class="pcat-flag">Telegram fleet</span>' : '')
      + '</div></button>';
  }

  function renderTeams(catalog) {
    var ts = teams(catalog);
    var tenant = tenantOf(catalog);
    var totalProfiles = ts.reduce(function (a, t) { return a + profilesForTeam(catalog, t).length; }, 0);
    var totalRoutes = ts.reduce(function (a, t) { return a + routesForTeam(catalog, t).length; }, 0);
    var grid = ts.length
      ? '<section class="pcat-grid">' + ts.map(function (t) { return teamCard(catalog, t); }).join('') + '</section>'
      : '<section class="gbhub-empty-state">No profile teams in scope.</section>';
    return ''
      + '<section class="gbhub-hero">'
      + '<div class="gbhub-brand-row"><span class="gbhub-mark">gb</span><span>GBAutomation</span></div>'
      + '<span class="gbhub-badge">Profile teams</span>'
      + '<h2>Profile Team Catalog</h2>'
      + '<p class="gbhub-muted"><strong>' + ts.length + '</strong> teams &middot; <strong>' + totalProfiles + '</strong> profiles &middot; <strong>' + totalRoutes + '</strong> routes for <strong>' + esc(tenant) + '</strong>.</p>'
      + '</section>'
      + grid;
  }

  function profileCard(catalog, p) {
    return '<button type="button" class="pcat-card pcat-card--profile" onclick="profilesCatalogDrill(\'profile\',\'' + esc(p.profile_id) + '\')">'
      + artBlock(p.profile_id, p.display_name || p.profile_id, true)
      + '<div class="pcat-card-body"><div class="pcat-card-topline"><span>' + esc(p.team_id || p.profile_type || 'template') + '</span>'
      + '<span>' + esc(p.model || p.runtime || 'model tbd') + '</span></div>'
      + '<h3>' + esc(p.display_name || p.profile_id) + '</h3>'
      + '<p class="gbhub-muted">' + esc(p.role || 'Reusable Hermes profile.') + '</p>'
      + '<div class="pcat-roster"><span>' + esc(p.profile_type || 'profile') + '</span>'
      + (p.skill_count != null ? '<span>' + p.skill_count + ' skills</span>' : '')
      + (p.route_count != null ? '<span>' + p.route_count + ' routes</span>' : '')
      + '</div></div></button>';
  }

  function renderIndex(catalog) {
    var ps = profiles(catalog);
    var tenant = tenantOf(catalog);
    var grid = ps.length
      ? '<section class="pcat-grid">' + ps.map(function (p) { return profileCard(catalog, p); }).join('') + '</section>'
      : '<section class="gbhub-empty-state">No profiles in scope.</section>';
    return ''
      + '<section class="gbhub-hero">'
      + '<div class="gbhub-brand-row"><span class="gbhub-mark">gb</span><span>GBAutomation</span></div>'
      + '<span class="gbhub-badge">Profile index</span>'
      + '<h2>Hermes Profile Index</h2>'
      + '<p class="gbhub-muted"><strong>' + ps.length + '</strong> profiles from <strong>' + esc(tenant) + '</strong>. Source: ' + esc(catalog.source || 'fixture') + '.</p>'
      + '</section>'
      + grid;
  }

  function renderTac(catalog) {
    var tacTeam = teamById(catalog, 'tac-hermes');
    var tacProfiles = tacTeam ? profilesForTeam(catalog, tacTeam)
      : profiles(catalog).filter(function (p) { return p.team_id === 'tac-hermes'; });
    var variants = TAC_LEAD_VARIANTS.map(function (v) {
      return '<button type="button" class="pcat-variant" onclick="profilesCatalogDrill(\'profile\',\'' + esc(v.profileId) + '\')">'
        + '<div class="pcat-card-topline"><span>' + esc(v.modeId) + '</span><span>option</span></div>'
        + '<h3>' + esc(v.title) + '</h3><p class="gbhub-muted">' + esc(v.summary) + '</p>'
        + '<strong class="pcat-variant-emph">' + esc(v.emphasis) + '</strong></button>';
    }).join('');
    var roster = tacProfiles.length
      ? '<section class="pcat-grid">' + tacProfiles.map(function (p) { return profileCard(catalog, p); }).join('') + '</section>'
      : '<section class="gbhub-empty-state">No TAC profiles in scope.</section>';
    return ''
      + '<section class="gbhub-hero">'
      + '<div class="gbhub-brand-row"><span class="gbhub-mark">gb</span><span>GBAutomation</span></div>'
      + '<span class="gbhub-badge">TAC Lead variants</span>'
      + '<h2>TAC Lead Variants</h2>'
      + '<p class="gbhub-muted">Three UI treatments for the same profile: advisor, retrieval, and dispatch. Each maps to an operating mode in the profile spec.</p>'
      + '</section>'
      + '<section class="pcat-variant-grid">' + variants + '</section>'
      + '<div class="pcat-evidence" id="pcatEvidence"></div>'
      + roster;
  }

  function renderTeamDetail(catalog, team) {
    var members = profilesForTeam(catalog, team);
    return ''
      + '<button type="button" class="pcat-back" onclick="profilesCatalogBack()">← Profiles catalog</button>'
      + '<section class="gbhub-hero">'
      + '<span class="gbhub-eyebrow">Profile Team</span>'
      + '<h2>' + esc(team.display_name) + '</h2>'
      + '<p class="gbhub-muted">' + esc(team.purpose || '') + '</p>'
      + '<div class="pcat-roster"><span>' + esc(team.team_id) + '</span><span>' + esc(team.runtime || 'hermes') + '</span>'
      + '<span>' + esc(team.tenant || 'shared') + '</span><span>' + members.length + ' profiles</span>'
      + (routesForTeam(catalog, team).length ? '<span>' + routesForTeam(catalog, team).length + ' routes</span>' : '')
      + (isCoverage(team) ? '<span class="pcat-flag">coverage</span>' : '')
      + '</div></section>'
      + '<div class="pcat-evidence" id="pcatEvidence"></div>'
      + '<div class="pcat-detail-row">'
      + '<div class="pcat-routemap-wrap">' + routeMap(catalog, team, false, 0) + '</div>'
      + '<div class="pcat-source"><span class="gbhub-eyebrow">Source</span><code>' + esc(team.source_path || 'supabase:agent_profile_teams') + '</code></div>'
      + '</div>'
      + (members.length ? '<section class="pcat-grid">' + members.map(function (p) { return profileCard(catalog, p); }).join('') + '</section>' : '');
  }

  function renderProfileDetail(catalog, p) {
    var report = PROFILE_REPORTS[p.profile_id];
    var skills = (p.suggested_skills || []).map(function (s) { return '<span>' + esc(s) + '</span>'; }).join('');
    var routeKeys = (p.route_keys || []).map(function (s) { return '<span>' + esc(s) + '</span>'; }).join('');
    var reportBlock = report
      ? '<article class="pcat-report"><div class="pcat-report-head"><div><span class="gbhub-eyebrow">Embedded report</span>'
        + '<h3>' + esc(report.title) + '</h3><p class="gbhub-muted">' + esc(report.description) + '</p></div>'
        + '<div class="pcat-report-actions">'
        + '<a href="' + esc(report.summaryPath) + '" target="_blank" rel="noreferrer">Summary data</a>'
        + '<a href="' + esc(report.publicPath) + '" target="_blank" rel="noreferrer">Open raw report</a></div></div>'
        + '<iframe class="pcat-report-frame" loading="lazy" src="' + esc(report.publicPath) + '" title="' + esc(p.display_name) + ' report"></iframe></article>'
      : '<article class="pcat-source"><span class="gbhub-eyebrow">Report</span><p class="gbhub-muted">No individual HTML profile report has been generated for this profile yet.</p></article>';
    return ''
      + '<button type="button" class="pcat-back" onclick="profilesCatalogBack()">← Profiles catalog</button>'
      + '<section class="gbhub-hero pcat-profile-hero">'
      + '<div class="pcat-profile-hero-copy"><span class="gbhub-eyebrow">Individual Profile</span>'
      + '<h2>' + esc(p.display_name || p.profile_id) + '</h2>'
      + '<p class="gbhub-muted">' + esc(p.role || 'Reusable Hermes profile.') + '</p>'
      + '<div class="pcat-roster"><span>' + esc(p.status || 'status tbd') + '</span><span>' + esc(p.model || 'model tbd') + '</span>'
      + (p.provider ? '<span>' + esc(p.provider) + '</span>' : '')
      + '<span>' + (p.skill_count != null ? p.skill_count : 0) + ' skills</span>'
      + '<span>' + (p.route_count != null ? p.route_count : 0) + ' routes</span></div></div>'
      + artBlock(p.profile_id, p.display_name || p.profile_id, true)
      + '</section>'
      + '<div class="pcat-evidence" id="pcatEvidence"></div>'
      + '<div class="pcat-detail-row">'
      + '<article class="pcat-source"><span class="gbhub-eyebrow">Suggested skills</span><div class="pcat-roster">' + (skills || '<span class="gbhub-muted">none</span>') + '</div></article>'
      + '<article class="pcat-source"><span class="gbhub-eyebrow">Route keys</span><div class="pcat-roster">' + (routeKeys || '<span class="gbhub-muted">none</span>') + '</div></article>'
      + '<article class="pcat-source"><span class="gbhub-eyebrow">Source</span><code>' + esc(p.source_path || 'supabase:agent_profile_catalog') + '</code>'
      + (p.deploy_path ? '<code>' + esc(p.deploy_user ? (p.deploy_user + '@') : '') + esc(p.deploy_path) + '</code>' : '') + '</article>'
      + '</div>'
      + reportBlock;
  }

  function render() {
    var container = document.getElementById('profilescatalogContent');
    if (!container) return;
    var catalog = _catalog;
    if (!catalog) {
      container.innerHTML = '<section class="gbhub-hero"><h2>Profiles Catalog</h2><p class="gbhub-muted">Catalog unavailable.</p></section>';
      return;
    }
    var tenant = tenantOf(catalog);
    var body;
    var evidenceKey = '*';
    if (_drill && _drill.kind === 'team') {
      var team = teamById(catalog, _drill.id);
      if (!team) { _drill = null; render(); return; }
      body = renderTeamDetail(catalog, team); evidenceKey = team.team_id;
    } else if (_drill && _drill.kind === 'profile') {
      var p = profileById(catalog, _drill.id) || { profile_id: _drill.id, display_name: _drill.id };
      body = renderProfileDetail(catalog, p); evidenceKey = p.profile_id;
    } else if (_view === 'index') {
      body = subnav(catalog) + renderIndex(catalog);
    } else if (_view === 'tac') {
      body = subnav(catalog) + renderTac(catalog); evidenceKey = 'tac';
    } else {
      body = subnav(catalog) + renderTeams(catalog);
    }
    container.innerHTML = body;
    if (document.getElementById('pcatEvidence')) {
      loadEvidence(tenant, evidenceKey === '*' ? '' : evidenceKey, 'pcatEvidence');
    }
  }

  // ── public handlers (referenced by inline onclick) ──
  window.profilesCatalogSetView = function (view) { _view = view; _drill = null; render(); };
  window.profilesCatalogDrill = function (kind, id) { _drill = { kind: kind, id: id }; render(); };
  window.profilesCatalogBack = function () { _drill = null; render(); };

  window.loadProfilesCatalog = function (force) {
    var container = document.getElementById('profilescatalogContent');
    if (!container) return;
    if (_loaded && !force) return;
    // Fixture first (works offline on the PC), then upgrade to live (Mini-only).
    fetchJson('/agent-profiles/catalog.json').then(function (fixture) {
      if (fixture) { _catalog = fixture; render(); }
      _loaded = true;
      var tenant = tenantOf(_catalog || {});
      fetchJson('/api/gbauto/agent-profiles?tenant=' + encodeURIComponent(tenant)).then(function (live) {
        if (live && live.ok && live.available && (Array.isArray(live.teams) && live.teams.length || Array.isArray(live.profiles) && live.profiles.length)) {
          _catalog = live;
          _evidenceCache = {};
          render();
        }
      });
    });
  };
})();
