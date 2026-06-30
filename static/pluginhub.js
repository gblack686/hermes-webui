// Plugins management hub (B4: 9119 -> WebUI port).
//
// Vanilla-JS rebuild of 9119's PluginsPage.tsx. Talks to the native WebUI
// endpoints added in api/plugins.py + api/routes.py:
//   GET  /api/plugins/hub          -> merged agent + dashboard metadata
//   GET  /api/plugins/rescan       -> re-scan dashboard manifests
//   POST /api/plugins/install      -> {identifier, force, enable}
//   POST /api/plugins/<name>/enable | disable | update | remove
//   POST /api/plugins/<name>/visibility -> {hidden}
//   POST /api/plugins/providers    -> {memory_provider, context_engine}
//
// All routes are gated by the WebUI's own session auth (check_auth) + CSRF at
// the server layer; there is no 9119 token scheme. When the hermes_cli
// management backend is absent (e.g. standalone WebUI dev on PC), the hub
// reports backend_available:false and the panel renders read-only-ish with a
// notice instead of erroring.
//
// Security: plugin names/labels are NEVER interpolated into inline onclick
// handlers. Buttons carry data-* attributes and are dispatched via delegated
// listeners bound once on the container (mirrors the no-inline-handler policy
// enforced for the dashboard plugin open button).

let _pluginHubState = null;
let _pluginHubBusy = false;
let _pluginHubBound = false;

function _phEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function _phToast(msg, type) {
  if (typeof showToast === 'function') showToast(msg, null, type);
}

function _pluginHubBind() {
  if (_pluginHubBound) return;
  const content = document.getElementById('pluginHubContent');
  const sidebar = document.getElementById('pluginHubSidebar');
  if (content) content.addEventListener('click', _pluginHubOnClick);
  if (sidebar) sidebar.addEventListener('click', _pluginHubOnClick);
  _pluginHubBound = true;
}

function _pluginHubOnClick(ev) {
  const el = ev.target && ev.target.closest ? ev.target.closest('[data-ph-action]') : null;
  if (!el) return;
  const action = el.getAttribute('data-ph-action');
  const name = el.getAttribute('data-ph-name') || '';
  if (action === 'save-providers') { _pluginHubSaveProviders(); return; }
  if (action === 'focus') { _pluginHubFocus(name); return; }
  if (action === 'visibility') {
    _pluginHubVisibility(name, el.getAttribute('data-ph-hidden') === '1');
    return;
  }
  if (['enable', 'disable', 'update', 'remove'].includes(action)) {
    _pluginHubAction(name, action);
  }
}

async function loadPluginHub(force) {
  _pluginHubBind();
  const content = document.getElementById('pluginHubContent');
  const sidebar = document.getElementById('pluginHubSidebar');
  if (content && !_pluginHubState) {
    content.innerHTML = '<div style="color:var(--muted);font-size:13px">Loading...</div>';
  }
  try {
    if (force) {
      try { await api('/api/plugins/rescan'); } catch (e) { /* non-fatal */ }
    }
    _pluginHubState = await api('/api/plugins/hub');
  } catch (e) {
    _pluginHubState = null;
    const msg = (e && e.message) ? e.message : 'Failed to load plugins.';
    if (content) content.innerHTML = '<div class="plughub-banner warn">' + _phEsc(msg) + '</div>';
    if (sidebar) sidebar.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">' + _phEsc(msg) + '</div>';
    return;
  }
  _renderPluginHub();
}

function _phBadge(status) {
  const cls = status === 'enabled' ? 'enabled' : (status === 'disabled' ? 'disabled' : 'inactive');
  return '<span class="plughub-badge ' + cls + '">' + _phEsc(status || 'inactive') + '</span>';
}

function _phBtn(label, action, name, extra) {
  return '<button type="button" class="plughub-btn' + (extra && extra.cls ? ' ' + extra.cls : '')
    + '" data-ph-action="' + _phEsc(action) + '" data-ph-name="' + _phEsc(name) + '"'
    + (extra && extra.hidden != null ? ' data-ph-hidden="' + (extra.hidden ? '1' : '0') + '"' : '')
    + '>' + _phEsc(label) + '</button>';
}

function _phPluginCard(p) {
  const parts = [];
  parts.push('<div class="plughub-card" data-plugin="' + _phEsc(p.name) + '">');
  parts.push('<div class="plughub-card-head">');
  parts.push('<span class="plughub-card-name">' + _phEsc(p.name) + '</span>');
  if (p.version) parts.push('<span class="plughub-card-ver">v' + _phEsc(p.version) + '</span>');
  parts.push(_phBadge(p.runtime_status));
  if (p.source) parts.push('<span class="plughub-badge">' + _phEsc(p.source) + '</span>');
  if (p.has_dashboard_manifest) parts.push('<span class="plughub-badge dash">dashboard</span>');
  if (p.auth_required) parts.push('<span class="plughub-badge auth">auth needed</span>');
  parts.push('</div>');
  if (p.description) parts.push('<div class="plughub-card-desc">' + _phEsc(p.description) + '</div>');
  parts.push('<div class="plughub-actions">');
  if (p.runtime_status === 'enabled') {
    parts.push(_phBtn('Disable', 'disable', p.name));
  } else {
    parts.push(_phBtn('Enable', 'enable', p.name));
  }
  if (p.has_dashboard_manifest) {
    const visLabel = p.user_hidden ? 'Show in sidebar' : 'Hide from sidebar';
    parts.push(_phBtn(visLabel, 'visibility', p.name, { hidden: !p.user_hidden }));
  }
  if (p.can_update_git) parts.push(_phBtn('Update', 'update', p.name));
  if (p.can_remove) parts.push(_phBtn('Remove', 'remove', p.name, { cls: 'danger' }));
  if (p.auth_required && p.auth_command) {
    parts.push('<code style="font-size:11px;color:var(--muted)">' + _phEsc(p.auth_command) + '</code>');
  }
  parts.push('</div>');
  parts.push('</div>');
  return parts.join('');
}

function _phProviderSelect(id, current, options) {
  const opts = (options || []).map(o => {
    const sel = (o.name === current) ? ' selected' : '';
    return '<option value="' + _phEsc(o.name) + '"' + sel + '>' + _phEsc(o.name) + (o.description ? ' — ' + _phEsc(o.description) : '') + '</option>';
  });
  if (current && !(options || []).some(o => o.name === current)) {
    opts.unshift('<option value="' + _phEsc(current) + '" selected>' + _phEsc(current) + '</option>');
  }
  opts.unshift('<option value="">(none)</option>');
  return '<select id="' + id + '">' + opts.join('') + '</select>';
}

function _renderPluginHub() {
  const content = document.getElementById('pluginHubContent');
  const sidebar = document.getElementById('pluginHubSidebar');
  const st = _pluginHubState;
  if (!content || !st) return;

  const filterEl = document.getElementById('pluginHubFilter');
  const filter = (filterEl && filterEl.value || '').trim().toLowerCase();
  const matches = p => !filter
    || String(p.name || '').toLowerCase().includes(filter)
    || String(p.description || '').toLowerCase().includes(filter);

  const plugins = (st.plugins || []).filter(matches);
  const orphans = (st.orphan_dashboard_plugins || []).filter(o => !filter
    || String(o.name || o.label || '').toLowerCase().includes(filter));

  const html = [];

  if (st.backend_available === false) {
    html.push('<div class="plughub-banner warn">Plugin management backend (hermes_cli) is not available on this host. Install / enable / disable / update / remove and provider selection are disabled here; manage plugins on a host running the full Hermes agent. Dashboard plugins discovered by the WebUI are still listed below.</div>');
  }

  html.push('<div class="plughub-section">');
  html.push('<h3 class="plughub-section-title">Agent plugins</h3>');
  html.push('<p class="plughub-section-sub">Installed Hermes agent plugins. Enable to load at runtime; disable to exclude.</p>');
  if (plugins.length) {
    plugins.forEach(p => html.push(_phPluginCard(p)));
  } else {
    html.push('<div style="color:var(--muted);font-size:12.5px">' + (st.backend_available === false ? 'No agent plugins (backend unavailable).' : 'No agent plugins found.') + '</div>');
  }
  html.push('</div>');

  if (orphans.length) {
    html.push('<div class="plughub-section">');
    html.push('<h3 class="plughub-section-title">Dashboard-only plugins</h3>');
    html.push('<p class="plughub-section-sub">Discovered dashboard extensions without a matching agent plugin.</p>');
    orphans.forEach(o => {
      html.push('<div class="plughub-card">');
      html.push('<div class="plughub-card-head">');
      html.push('<span class="plughub-card-name">' + _phEsc(o.label || o.name) + '</span>');
      if (o.version) html.push('<span class="plughub-card-ver">v' + _phEsc(o.version) + '</span>');
      html.push('<span class="plughub-badge dash">dashboard</span>');
      html.push('</div>');
      if (o.description) html.push('<div class="plughub-card-desc">' + _phEsc(o.description) + '</div>');
      html.push('</div>');
    });
    html.push('</div>');
  }

  const prov = st.providers || {};
  const hasProviderOpts = (prov.memory_options && prov.memory_options.length) || (prov.context_options && prov.context_options.length) || prov.memory_provider || prov.context_engine;
  if (st.backend_available !== false && hasProviderOpts) {
    html.push('<div class="plughub-section">');
    html.push('<h3 class="plughub-section-title">Providers</h3>');
    html.push('<p class="plughub-section-sub">Exclusive providers selected via configuration.</p>');
    html.push('<div class="plughub-providers">');
    html.push('<div class="plughub-provider-row"><label for="phMemoryProvider">Memory provider</label>' + _phProviderSelect('phMemoryProvider', prov.memory_provider || '', prov.memory_options) + '</div>');
    html.push('<div class="plughub-provider-row"><label for="phContextEngine">Context engine</label>' + _phProviderSelect('phContextEngine', prov.context_engine || '', prov.context_options) + '</div>');
    html.push('<div class="plughub-provider-row" style="justify-content:flex-end"><button type="button" class="plughub-btn" data-ph-action="save-providers">Save providers</button></div>');
    html.push('</div>');
    html.push('</div>');
  }

  content.innerHTML = html.join('');

  if (sidebar) {
    if (plugins.length || orphans.length) {
      const items = [];
      plugins.forEach(p => {
        const dot = p.runtime_status === 'enabled' ? 'enabled' : (p.runtime_status === 'disabled' ? 'disabled' : '');
        items.push('<div class="plughub-sidebar-item" data-ph-action="focus" data-ph-name="' + _phEsc(p.name) + '"><span class="plughub-sidebar-dot ' + dot + '"></span>' + _phEsc(p.name) + '</div>');
      });
      orphans.forEach(o => {
        items.push('<div class="plughub-sidebar-item"><span class="plughub-sidebar-dot"></span>' + _phEsc(o.label || o.name) + '</div>');
      });
      sidebar.innerHTML = items.join('');
    } else {
      sidebar.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">No plugins.</div>';
    }
  }
}

function _pluginHubFocus(name) {
  const sel = (window.CSS && CSS.escape) ? CSS.escape(name) : name;
  const card = document.querySelector('#pluginHubContent .plughub-card[data-plugin="' + sel + '"]');
  if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function _pluginHubAction(name, action) {
  if (_pluginHubBusy) return;
  if (action === 'remove' && !window.confirm('Remove plugin "' + name + '"? This deletes it from disk.')) return;
  _pluginHubBusy = true;
  try {
    await api('/api/plugins/' + encodeURIComponent(name) + '/' + action, { method: 'POST', body: '{}' });
    _phToast('Plugin ' + name + ' ' + action + (action.endsWith('e') ? 'd' : 'ed'), 'success');
    await loadPluginHub();
  } catch (e) {
    _phToast((e && e.message) || (action + ' failed'), 'error');
  } finally {
    _pluginHubBusy = false;
  }
}

async function _pluginHubVisibility(name, hidden) {
  if (_pluginHubBusy) return;
  _pluginHubBusy = true;
  try {
    await api('/api/plugins/' + encodeURIComponent(name) + '/visibility', { method: 'POST', body: JSON.stringify({ hidden: !!hidden }) });
    _phToast(name + (hidden ? ' hidden from sidebar' : ' shown in sidebar'), 'success');
    await loadPluginHub();
  } catch (e) {
    _phToast((e && e.message) || 'Visibility update failed', 'error');
  } finally {
    _pluginHubBusy = false;
  }
}

async function _pluginHubInstall() {
  const identifier = window.prompt('Install plugin\n\nEnter a plugin identifier (git URL, git@host:owner/repo, or a registry name):');
  if (!identifier || !identifier.trim()) return;
  if (_pluginHubBusy) return;
  _pluginHubBusy = true;
  try {
    await api('/api/plugins/install', { method: 'POST', timeoutMs: 120000, body: JSON.stringify({ identifier: identifier.trim(), force: false, enable: true }) });
    _phToast('Installed ' + identifier.trim(), 'success');
    await loadPluginHub(true);
  } catch (e) {
    _phToast((e && e.message) || 'Install failed', 'error');
  } finally {
    _pluginHubBusy = false;
  }
}

async function _pluginHubSaveProviders() {
  if (_pluginHubBusy) return;
  const mem = document.getElementById('phMemoryProvider');
  const ctx = document.getElementById('phContextEngine');
  _pluginHubBusy = true;
  try {
    await api('/api/plugins/providers', { method: 'POST', body: JSON.stringify({
      memory_provider: mem ? mem.value : null,
      context_engine: ctx ? ctx.value : null,
    }) });
    _phToast('Providers saved', 'success');
    await loadPluginHub();
  } catch (e) {
    _phToast((e && e.message) || 'Failed to save providers', 'error');
  } finally {
    _pluginHubBusy = false;
  }
}
