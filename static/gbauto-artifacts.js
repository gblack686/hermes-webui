/*
 * gbauto-artifacts.js — GBauto /artifacts panel (Phase 4 P0)
 * Server-backed port of the 9119 DocumentsPage. Reads the native
 * GET /api/gbauto/artifacts index (no browser Supabase, no 9119 static bundle,
 * no 8791 sidecar) and renders a searchable/filterable card gallery.
 * Plan: 2026-07-01-hermes-webui-unified-gbauto-shell-tac-plan
 */
(function () {
  'use strict';

  var STATE = { artifacts: [], recentlyAdded: [], generatedAt: null, stale: null, loaded: false, loading: false, error: null };
  var FILTER = { q: '', docType: '', group: '', onlyNew: false };

  function api(path, opts) {
    if (typeof window.api === 'function') return window.api(path, opts);
    return fetch(path, opts).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) { for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); } }
    if (kids) { for (var i = 0; i < kids.length; i++) { var c = kids[i]; if (c == null) continue; n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); } }
    return n;
  }

  function unique(list, key) {
    var seen = {}, out = [];
    list.forEach(function (a) { var v = a[key]; if (v && !seen[v]) { seen[v] = 1; out.push(v); } });
    out.sort();
    return out;
  }

  function matches(a) {
    if (FILTER.docType && a.docType !== FILTER.docType) return false;
    if (FILTER.group && a.group !== FILTER.group) return false;
    if (FILTER.onlyNew && STATE.recentlyAdded.indexOf(a.id) === -1) return false;
    if (FILTER.q) {
      var q = FILTER.q.toLowerCase();
      var hay = ((a.title || '') + ' ' + (a.description || '') + ' ' + (a.taxonomy || '') + ' ' + (a.sourcePath || '')).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  }

  function card(a) {
    var isNew = STATE.recentlyAdded.indexOf(a.id) !== -1;
    var kids = [];
    if (a.previewPath) {
      kids.push(el('img', { 'class': 'gb-artifact-preview', 'alt': '', 'loading': 'lazy', 'decoding': 'async', 'src': a.previewPath }));
    }
    kids.push(el('span', { 'class': 'gb-artifact-meta' }, [
      [a.docType, a.group, (isNew ? 'NEW' : null)].filter(Boolean).join(' · ')
    ]));
    kids.push(el('strong', null, [a.title || a.sourcePath || a.id]));
    if (a.description) kids.push(el('p', null, [a.description]));
    var href = a.publicPath || a.previewPath || null;
    return el('a', {
      'class': 'gb-artifact-card',
      'href': href || null,
      'target': href ? '_blank' : null,
      'rel': href ? 'noopener noreferrer' : null,
      'title': a.title || ''
    }, kids);
  }

  function render() {
    var mount = document.getElementById('mainArtifacts');
    if (!mount) return;
    mount.innerHTML = '';
    var page = el('div', { 'class': 'gb-artifacts-page' });

    var stale = STATE.stale ? (' · stale: ' + STATE.stale) : '';
    var gen = STATE.generatedAt ? ('generated ' + STATE.generatedAt) : '';
    page.appendChild(el('div', { 'class': 'gb-artifacts-head' }, [
      el('h2', null, ['Artifacts']),
      el('span', { 'class': 'gb-artifacts-stale' }, [gen + stale])
    ]));

    var search = el('input', { 'type': 'search', 'placeholder': 'Search artifacts…', 'value': FILTER.q });
    search.addEventListener('input', function () { FILTER.q = search.value; renderGrid(grid); });

    var docSel = el('select');
    docSel.appendChild(el('option', { 'value': '' }, ['All types']));
    unique(STATE.artifacts, 'docType').forEach(function (v) { docSel.appendChild(el('option', { 'value': v }, [v])); });
    docSel.value = FILTER.docType;
    docSel.addEventListener('change', function () { FILTER.docType = docSel.value; renderGrid(grid); });

    var grpSel = el('select');
    grpSel.appendChild(el('option', { 'value': '' }, ['All groups']));
    unique(STATE.artifacts, 'group').forEach(function (v) { grpSel.appendChild(el('option', { 'value': v }, [v])); });
    grpSel.value = FILTER.group;
    grpSel.addEventListener('change', function () { FILTER.group = grpSel.value; renderGrid(grid); });

    var newBtn = el('button', { 'type': 'button', 'class': 'home-chat-shortcuts' }, [FILTER.onlyNew ? '✓ New only' : 'New only']);
    newBtn.style.border = '1px solid var(--border2)'; newBtn.style.borderRadius = '6px'; newBtn.style.padding = '.42rem .6rem'; newBtn.style.background = 'var(--input-bg)'; newBtn.style.cursor = 'pointer';
    newBtn.addEventListener('click', function () { FILTER.onlyNew = !FILTER.onlyNew; render(); });

    page.appendChild(el('div', { 'class': 'gb-artifacts-toolbar' }, [search, docSel, grpSel, newBtn]));

    var grid = el('div', { 'class': 'gb-artifacts-grid' });
    page.appendChild(grid);
    mount.appendChild(page);
    renderGrid(grid);
  }

  function renderGrid(grid) {
    grid.innerHTML = '';
    if (STATE.loading) { grid.appendChild(el('div', { 'class': 'gb-artifacts-empty' }, ['Loading…'])); return; }
    if (STATE.error) { grid.appendChild(el('div', { 'class': 'gb-artifacts-error' }, ['Could not load artifacts: ' + STATE.error])); return; }
    var shown = STATE.artifacts.filter(matches);
    if (!shown.length) { grid.appendChild(el('div', { 'class': 'gb-artifacts-empty' }, [STATE.artifacts.length ? 'No artifacts match the current filters.' : 'No artifacts indexed yet.'])); return; }
    shown.forEach(function (a) { grid.appendChild(card(a)); });
  }

  function load(force) {
    if (STATE.loading) return;
    if (STATE.loaded && !force) { render(); return; }
    STATE.loading = true; STATE.error = null; render();
    api('/api/gbauto/artifacts', { timeoutToast: false }).then(function (data) {
      STATE.artifacts = (data && data.artifacts) || [];
      STATE.recentlyAdded = (data && data.recentlyAdded) || [];
      STATE.generatedAt = data && data.generatedAt || null;
      STATE.stale = data && data.stale_reason || null;
      STATE.loaded = true; STATE.loading = false;
      render();
    }).catch(function (e) {
      STATE.loading = false; STATE.error = (e && e.message) || String(e);
      render();
    });
  }

  window.gbautoLoadArtifacts = load;
})();
