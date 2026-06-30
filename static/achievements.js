/* Achievements panel (plan B11): Steam-style badges for agentic Hermes work.
 *
 * Native vanilla-JS port of 9119's hermes-achievements dashboard plugin UI.
 * The pure engine lives in api/achievements.py and scans this WebUI's own
 * JSON session store; this module just renders its payload.
 *
 * Endpoints (all gated on the WebUI's own auth):
 *   GET  /api/plugins/hermes-achievements/achievements   full catalog + counts
 *   GET  /api/plugins/hermes-achievements/scan-status     background-scan state
 *   GET  /api/plugins/hermes-achievements/recent-unlocks  newest unlocks
 *   POST /api/plugins/hermes-achievements/rescan          force a synchronous scan
 *   POST /api/plugins/hermes-achievements/reset-state     clear unlock history
 *
 * The 1200x630 share-card canvas from 9119 is intentionally deferred (plan
 * keeps B11 at size L, not XL).
 */
(function () {
  'use strict';

  function esc(v) {
    if (typeof _ovEsc === 'function') return _ovEsc(v);
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fetchJson(url) {
    return fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // ── module state ──
  var _data = null;       // last /achievements payload
  var _filter = 'all';    // 'all' | 'unlocked' | 'discovered' | 'secret'
  var _loaded = false;
  var _pollTimer = null;
  var _rescanning = false;

  var TIER_ORDER = ['Copper', 'Silver', 'Gold', 'Diamond', 'Olympian'];

  function content() { return document.getElementById('achievementsContent'); }

  function summaryBar(data) {
    var meta = data.scan_meta || {};
    var status = meta.status || {};
    var stale = data.is_stale || status.snapshot_stale;
    var scanState = status.state || meta.mode || 'idle';
    var note = '';
    if (data.error) {
      note = '<span class="ach-pill ach-pill--err">' + esc(data.error) + '</span>';
    } else if (meta.mode === 'pending' || scanState === 'running') {
      note = '<span class="ach-pill ach-pill--run">Scanning sessions&hellip;</span>';
    } else if (stale) {
      note = '<span class="ach-pill ach-pill--stale">Refreshing in background&hellip;</span>';
    }
    return '<div class="ach-summary">'
      + '<div class="ach-stat"><b>' + esc(data.unlocked_count || 0) + '</b><span>Unlocked</span></div>'
      + '<div class="ach-stat"><b>' + esc(data.discovered_count || 0) + '</b><span>In progress</span></div>'
      + '<div class="ach-stat"><b>' + esc(data.secret_count || 0) + '</b><span>Secret</span></div>'
      + '<div class="ach-stat"><b>' + esc(data.total_count || 0) + '</b><span>Total</span></div>'
      + '<div class="ach-summary-note">' + note + '</div>'
      + '</div>';
  }

  function filterBar() {
    var items = [['all', 'All'], ['unlocked', 'Unlocked'], ['discovered', 'In progress'], ['secret', 'Secret']];
    return '<div class="ach-filters">' + items.map(function (it) {
      var cls = 'ach-filter' + (_filter === it[0] ? ' is-active' : '');
      return '<button type="button" class="' + cls + '" onclick="achievementsSetFilter(\'' + it[0] + '\')">' + esc(it[1]) + '</button>';
    }).join('') + '</div>';
  }

  function progressBar(a) {
    var pct = Math.max(0, Math.min(100, Number(a.progress_pct || 0)));
    var label;
    if (a.unlocked && !a.next_tier) {
      label = 'Maxed out';
    } else if (typeof a.progress === 'number' && a.next_threshold) {
      label = esc(a.progress) + ' / ' + esc(a.next_threshold) + (a.next_tier ? ' → ' + esc(a.next_tier) : '');
    } else {
      label = pct + '%';
    }
    return '<div class="ach-progress"><div class="ach-progress-track"><div class="ach-progress-fill" style="width:' + pct + '%"></div></div>'
      + '<span class="ach-progress-label">' + label + '</span></div>';
  }

  function card(a) {
    var state = a.state || (a.unlocked ? 'unlocked' : 'discovered');
    var cls = 'ach-card ach-card--' + esc(state);
    var tier = a.tier ? '<span class="ach-tier ach-tier--' + esc(String(a.tier).toLowerCase()) + '">' + esc(a.tier) + '</span>' : '';
    var lock = a.unlocked ? '★' : (state === 'secret' ? '?' : '☆');
    var crit = a.criteria ? '<p class="ach-crit">' + esc(a.criteria) + '</p>' : '';
    return '<article class="' + cls + '">'
      + '<div class="ach-card-head"><span class="ach-badge" aria-hidden="true">' + lock + '</span>'
      + '<div class="ach-card-title"><h4>' + esc(a.name) + '</h4>' + tier + '</div></div>'
      + '<p class="ach-desc">' + esc(a.description) + '</p>'
      + progressBar(a)
      + crit
      + '</article>';
  }

  function matchesFilter(a) {
    if (_filter === 'all') return true;
    if (_filter === 'unlocked') return !!a.unlocked;
    if (_filter === 'secret') return a.state === 'secret';
    if (_filter === 'discovered') return !a.unlocked && a.state !== 'secret';
    return true;
  }

  function render() {
    var el = content();
    if (!el) return;
    var data = _data;
    if (!data) {
      el.innerHTML = '<p class="gbhub-muted">Loading achievements&hellip;</p>';
      return;
    }
    var achievements = (data.achievements || []).filter(matchesFilter);
    // Group by category, preserving first-seen order.
    var groups = [];
    var byCat = {};
    achievements.forEach(function (a) {
      var cat = a.category || 'Other';
      if (!byCat[cat]) { byCat[cat] = []; groups.push(cat); }
      byCat[cat].push(a);
    });
    // Sort unlocked-first then by tier depth within each category.
    function rank(a) {
      var t = a.tier ? TIER_ORDER.indexOf(a.tier) : -1;
      return (a.unlocked ? 100 : 0) + (t + 1) + (a.progress_pct || 0) / 1000;
    }
    var body = groups.map(function (cat) {
      var cards = byCat[cat].sort(function (x, y) { return rank(y) - rank(x); }).map(card).join('');
      return '<section class="ach-group"><h3 class="ach-group-title">' + esc(cat)
        + ' <span class="ach-group-count">' + byCat[cat].length + '</span></h3>'
        + '<div class="ach-grid">' + cards + '</div></section>';
    }).join('');
    if (!groups.length) {
      body = '<p class="gbhub-muted">No achievements match this filter yet.</p>';
    }
    el.innerHTML = summaryBar(data) + filterBar() + body;
  }

  function schedulePoll(data) {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
    var meta = (data && data.scan_meta) || {};
    var status = meta.status || {};
    var busy = meta.mode === 'pending' || status.state === 'running' || data.is_stale;
    // Only poll while a scan is in flight; stop once we have a fresh snapshot.
    if (busy && _currentPanelIsAchievements()) {
      _pollTimer = setTimeout(function () { refresh(); }, 2500);
    }
  }

  function _currentPanelIsAchievements() {
    try { return typeof _currentPanel !== 'undefined' && _currentPanel === 'achievements'; }
    catch (_e) { return true; }
  }

  function refresh() {
    return fetchJson('/api/plugins/hermes-achievements/achievements').then(function (data) {
      if (data) { _data = data; render(); schedulePoll(data); }
      return data;
    });
  }

  // ── public handlers (referenced by inline onclick + panels.js) ──
  window.achievementsSetFilter = function (filter) { _filter = filter; render(); };

  window.achievementsRescan = function () {
    if (_rescanning) return;
    _rescanning = true;
    var btn = document.getElementById('achievementsRescanBtn');
    if (btn) btn.classList.add('is-busy');
    var done = function () {
      _rescanning = false;
      if (btn) btn.classList.remove('is-busy');
    };
    var poster = (typeof api === 'function')
      ? api('/api/plugins/hermes-achievements/rescan', { method: 'POST', timeoutMs: 120000 })
      : fetch('rescan'.replace(/^/, '/api/plugins/hermes-achievements/'), { method: 'POST', credentials: 'include' }).then(function (r) { return r.ok ? r.json() : null; });
    Promise.resolve(poster).then(function (data) {
      if (data && data.achievements) { _data = data; render(); schedulePoll(data); }
      else { refresh(); }
    }).catch(function () { refresh(); }).then(done, done);
  };

  window.loadAchievements = function (force) {
    var el = content();
    if (!el) return;
    if (_loaded && !force && _data) { render(); return; }
    _loaded = true;
    render();
    refresh();
  };
})();
