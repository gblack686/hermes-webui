/* Keys / Env tab (B3 — 9119 -> webui migration).
 *
 * Vanilla-JS reimplementation of the 9119 React EnvPage.tsx (~931 LOC):
 * provider-grouped API-key rows + reveal/set/remove, plus tool/messaging/
 * setting/skill categories. Talks to GET /api/env + POST /api/env/{set,
 * remove,reveal}. Reveal is gated server-side on WebUI's OWN auth + a
 * rate limiter (NOT 9119's bearer-token scheme). Writes flow through the
 * global api() helper, which injects the WebUI CSRF token transparently.
 *
 * The OAuth section adopts WebUI-native provider auth (Settings > Providers)
 * rather than re-implementing 9119's OAuthProvidersCard flows here.
 */

const _envState = {
  vars: null,          // { KEY: {is_set, redacted_value, description, url, category, is_password, tools, advanced} }
  edits: {},           // KEY -> in-progress edit value
  revealed: {},        // KEY -> revealed plaintext
  expanded: {},        // provider group name -> bool
  showAdvanced: true,
  loaded: false,
};

/* Map env-var key prefixes to a human-friendly provider name + ordering.
 * Ported from 9119 EnvPage PROVIDER_GROUPS. */
const _ENV_PROVIDER_GROUPS = [
  { prefix: 'NOUS_', name: 'Nous Portal', priority: 0 },
  { prefix: 'ANTHROPIC_', name: 'Anthropic', priority: 1 },
  { prefix: 'DASHSCOPE_', name: 'DashScope (Qwen)', priority: 2 },
  { prefix: 'HERMES_QWEN_', name: 'DashScope (Qwen)', priority: 2 },
  { prefix: 'DEEPSEEK_', name: 'DeepSeek', priority: 3 },
  { prefix: 'GOOGLE_', name: 'Gemini', priority: 4 },
  { prefix: 'GEMINI_', name: 'Gemini', priority: 4 },
  { prefix: 'GLM_', name: 'GLM / Z.AI', priority: 5 },
  { prefix: 'ZAI_', name: 'GLM / Z.AI', priority: 5 },
  { prefix: 'Z_AI_', name: 'GLM / Z.AI', priority: 5 },
  { prefix: 'HF_', name: 'Hugging Face', priority: 6 },
  { prefix: 'KIMI_', name: 'Kimi / Moonshot', priority: 7 },
  { prefix: 'MINIMAX_CN_', name: 'MiniMax (China)', priority: 9 },
  { prefix: 'MINIMAX_', name: 'MiniMax', priority: 8 },
  { prefix: 'OPENCODE_GO_', name: 'OpenCode Go', priority: 10 },
  { prefix: 'OPENCODE_ZEN_', name: 'OpenCode Zen', priority: 11 },
  { prefix: 'OPENROUTER_', name: 'OpenRouter', priority: 12 },
  { prefix: 'XIAOMI_', name: 'Xiaomi MiMo', priority: 13 },
];

function _envProviderGroup(key) {
  for (const g of _ENV_PROVIDER_GROUPS) {
    if (key.startsWith(g.prefix)) return g.name;
  }
  return 'Other';
}
function _envProviderPriority(name) {
  const e = _ENV_PROVIDER_GROUPS.find(g => g.name === name);
  return e ? e.priority : 99;
}

const _ENV_CATEGORY_LABELS = {
  tool: 'Tools',
  messaging: 'Messaging',
  setting: 'Settings',
  skill: 'Skills',
};
const _ENV_CATEGORY_ORDER = ['tool', 'messaging', 'setting', 'skill'];

async function loadEnv(force) {
  const root = document.getElementById('envRoot');
  if (!root) return;
  if (_envState.loaded && !force) { _envRender(); return; }
  root.innerHTML = '<div style="color:var(--muted);font-size:13px">Loading...</div>';
  try {
    const data = await api('/api/env');
    _envState.vars = (data && typeof data === 'object') ? data : {};
    _envState.loaded = true;
    _envRender();
  } catch (e) {
    root.innerHTML = '<div style="color:var(--danger,#e05);font-size:13px">Failed to load env vars: ' + esc(e && e.message) + '</div>';
  }
}

function toggleEnvAdvanced() {
  _envState.showAdvanced = !_envState.showAdvanced;
  const btn = document.getElementById('envAdvancedToggle');
  if (btn) btn.textContent = _envState.showAdvanced ? 'Hide advanced' : 'Show advanced';
  _envRender();
}

function _envToggleGroup(name) {
  _envState.expanded[name] = !_envState.expanded[name];
  _envRender();
}

function _envBeginEdit(key) {
  _envState.edits[key] = '';
  _envRender();
  const inp = document.getElementById('env-edit-' + _envCssId(key));
  if (inp) inp.focus();
}

function _envCancelEdit(key) {
  delete _envState.edits[key];
  _envRender();
}

async function envReveal(key) {
  if (_envState.revealed[key] !== undefined) {
    delete _envState.revealed[key];
    _envRender();
    return;
  }
  try {
    const resp = await api('/api/env/reveal', { method: 'POST', body: JSON.stringify({ key }) });
    _envState.revealed[key] = (resp && resp.value != null) ? resp.value : '';
    _envRender();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to reveal ' + key + ': ' + (e && e.message), 5000, 'error');
  }
}

async function envSave(key) {
  const inp = document.getElementById('env-edit-' + _envCssId(key));
  const value = inp ? inp.value : _envState.edits[key];
  if (!value) return;
  try {
    await api('/api/env/set', { method: 'POST', body: JSON.stringify({ key, value }) });
    const info = _envState.vars[key] || {};
    info.is_set = true;
    info.redacted_value = value.length > 8 ? (value.slice(0, 4) + '...' + value.slice(-4)) : '***';
    _envState.vars[key] = info;
    delete _envState.edits[key];
    delete _envState.revealed[key];
    _envRender();
    if (typeof showToast === 'function') showToast(key + ' saved', 3000, 'success');
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to save ' + key + ': ' + (e && e.message), 5000, 'error');
  }
}

async function envClear(key) {
  const info = _envState.vars[key] || {};
  const label = key + (info.description ? ' — ' + info.description : '');
  if (!window.confirm('Remove ' + label + ' from the .env file? This cannot be undone.')) return;
  try {
    await api('/api/env/remove', { method: 'POST', body: JSON.stringify({ key }) });
    info.is_set = false;
    info.redacted_value = null;
    _envState.vars[key] = info;
    delete _envState.edits[key];
    delete _envState.revealed[key];
    _envRender();
    if (typeof showToast === 'function') showToast(key + ' removed', 3000, 'success');
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to remove ' + key + ': ' + (e && e.message), 5000, 'error');
  }
}

function _envCssId(key) {
  return String(key).replace(/[^A-Za-z0-9_-]/g, '_');
}

function _envOnEditInput(key, el) {
  _envState.edits[key] = el.value;
}

/* ── render a single key row ── */
function _envRowHtml(key, info, compact) {
  const isEditing = _envState.edits[key] !== undefined;
  const isRevealed = _envState.revealed[key] !== undefined;
  const setBadge = info.is_set
    ? '<span class="env-badge env-badge-set">set</span>'
    : '<span class="env-badge env-badge-unset">not set</span>';
  const getLink = info.url
    ? '<a class="env-getkey" href="' + esc(info.url) + '" target="_blank" rel="noreferrer">Get key &#8599;</a>'
    : '';

  // Compact unset, non-editing row (inside provider groups)
  if (compact && !info.is_set && !isEditing) {
    return '<div class="env-row env-row-compact">'
      + '<div class="env-row-meta"><span class="env-key">' + esc(key) + '</span>'
      + '<span class="env-desc">' + esc(info.description || '') + '</span></div>'
      + '<div class="env-row-actions">' + getLink
      + '<button type="button" class="compact" onclick="_envBeginEdit(\'' + esc(key) + '\')">Set</button></div>'
      + '</div>';
  }

  if (!info.is_set && !isEditing) {
    return '<div class="env-row">'
      + '<div class="env-row-meta"><span class="env-key">' + esc(key) + '</span>'
      + '<span class="env-desc">' + esc(info.description || '') + '</span></div>'
      + '<div class="env-row-actions">' + getLink
      + '<button type="button" class="compact" onclick="_envBeginEdit(\'' + esc(key) + '\')">Set</button></div>'
      + '</div>';
  }

  // Full row for set keys or keys being edited
  let body = '<div class="env-row env-row-full">';
  body += '<div class="env-row-head"><span class="env-key">' + esc(key) + '</span>' + setBadge + getLink + '</div>';
  if (info.description) body += '<div class="env-desc">' + esc(info.description) + '</div>';
  if (Array.isArray(info.tools) && info.tools.length) {
    body += '<div class="env-tools">' + info.tools.map(t => '<span class="env-tool">' + esc(t) + '</span>').join('') + '</div>';
  }

  if (!isEditing) {
    const display = isRevealed ? esc(_envState.revealed[key]) : esc(info.is_set ? (info.redacted_value || '***') : '---');
    body += '<div class="env-row-value">';
    body += '<code class="env-value' + (isRevealed ? ' env-value-revealed' : '') + '">' + display + '</code>';
    if (info.is_set) {
      body += '<button type="button" class="compact" onclick="envReveal(\'' + esc(key) + '\')">' + (isRevealed ? 'Hide' : 'Reveal') + '</button>';
    }
    body += '<button type="button" class="compact" onclick="_envBeginEdit(\'' + esc(key) + '\')">' + (info.is_set ? 'Replace' : 'Set') + '</button>';
    if (info.is_set) {
      body += '<button type="button" class="compact danger" onclick="envClear(\'' + esc(key) + '\')">Clear</button>';
    }
    body += '</div>';
  } else {
    const placeholder = info.is_set ? 'Replace current value (' + esc(info.redacted_value || '---') + ')' : 'Enter value';
    body += '<div class="env-row-value">';
    body += '<input id="env-edit-' + _envCssId(key) + '" class="env-input" type="text" autocomplete="off" spellcheck="false" placeholder="' + placeholder + '" value="' + esc(_envState.edits[key] || '') + '" oninput="_envOnEditInput(\'' + esc(key) + '\', this)">';
    body += '<button type="button" class="compact primary" onclick="envSave(\'' + esc(key) + '\')">Save</button>';
    body += '<button type="button" class="compact" onclick="_envCancelEdit(\'' + esc(key) + '\')">Cancel</button>';
    body += '</div>';
  }
  body += '</div>';
  return body;
}

function _envRender() {
  const root = document.getElementById('envRoot');
  if (!root) return;
  const vars = _envState.vars || {};
  const entries = Object.entries(vars);
  const showAdv = _envState.showAdvanced;

  // ── Provider groups ──
  const providerEntries = entries.filter(([, info]) =>
    info.category === 'provider' && (showAdv || !info.advanced));
  const groupMap = new Map();
  for (const ent of providerEntries) {
    const g = _envProviderGroup(ent[0]);
    if (!groupMap.has(g)) groupMap.set(g, []);
    groupMap.get(g).push(ent);
  }
  const groups = Array.from(groupMap.entries())
    .map(([name, ents]) => ({ name, priority: _envProviderPriority(name), entries: ents }))
    .sort((a, b) => a.priority - b.priority);

  const configuredProviders = groups.filter(g => g.entries.some(([, i]) => i.is_set)).length;

  let html = '';
  html += '<div class="env-intro">Stored in the agent\'s <code>.env</code>. Changes apply to new sessions; restart the agent for running ones.</div>';

  // OAuth — adopt WebUI-native provider auth
  html += '<div class="env-card">';
  html += '<div class="env-card-head"><span class="env-card-title">OAuth providers</span></div>';
  html += '<div class="env-card-body"><p class="env-desc">OAuth-based providers (Anthropic, Nous, Codex) authenticate through WebUI\'s native provider flow.</p>';
  html += '<button type="button" class="compact" onclick="switchPanel(\'settings\')">Open Providers settings</button></div>';
  html += '</div>';

  // Providers card
  html += '<div class="env-card">';
  html += '<div class="env-card-head"><span class="env-card-title">LLM Providers</span>'
    + '<span class="env-card-meta">' + configuredProviders + ' of ' + groups.length + ' configured</span></div>';
  html += '<div class="env-card-body env-groups">';
  if (!groups.length) {
    html += '<div class="env-desc">No provider keys in catalog.</div>';
  }
  for (const group of groups) {
    const expanded = !!_envState.expanded[group.name];
    const setCount = group.entries.filter(([, i]) => i.is_set).length;
    html += '<div class="env-group">';
    html += '<button type="button" class="env-group-head" aria-expanded="' + expanded + '" onclick="_envToggleGroup(\'' + esc(group.name) + '\')">';
    html += '<span class="env-group-caret">' + (expanded ? '&#9662;' : '&#9656;') + '</span>';
    html += '<span class="env-group-name">' + esc(group.name) + '</span>';
    if (setCount) html += '<span class="env-badge env-badge-set">' + setCount + ' set</span>';
    html += '<span class="env-card-meta">' + group.entries.length + ' key' + (group.entries.length !== 1 ? 's' : '') + '</span>';
    html += '</button>';
    if (expanded) {
      html += '<div class="env-group-body">';
      // API keys / tokens first, then base URLs, then other
      const apiKeys = group.entries.filter(([k]) => k.endsWith('_API_KEY') || k.endsWith('_TOKEN'));
      const baseUrls = group.entries.filter(([k]) => k.endsWith('_BASE_URL'));
      const other = group.entries.filter(([k]) => !k.endsWith('_API_KEY') && !k.endsWith('_TOKEN') && !k.endsWith('_BASE_URL'));
      for (const [k, info] of apiKeys) html += _envRowHtml(k, info, true);
      for (const [k, info] of baseUrls) html += _envRowHtml(k, info, true);
      for (const [k, info] of other) html += _envRowHtml(k, info, true);
      html += '</div>';
    }
    html += '</div>';
  }
  html += '</div></div>';

  // Non-provider categories
  for (const cat of _ENV_CATEGORY_ORDER) {
    const catEntries = entries.filter(([, info]) =>
      info.category === cat && (showAdv || !info.advanced));
    if (!catEntries.length) continue;
    const setEntries = catEntries.filter(([, i]) => i.is_set);
    const unsetEntries = catEntries.filter(([, i]) => !i.is_set);
    html += '<div class="env-card">';
    html += '<div class="env-card-head"><span class="env-card-title">' + esc(_ENV_CATEGORY_LABELS[cat] || cat) + '</span>'
      + '<span class="env-card-meta">' + setEntries.length + ' of ' + catEntries.length + ' configured</span></div>';
    html += '<div class="env-card-body">';
    for (const [k, info] of setEntries) html += _envRowHtml(k, info, false);
    if (unsetEntries.length) {
      const expandKey = '__cat_' + cat;
      const expanded = !!_envState.expanded[expandKey];
      html += '<button type="button" class="env-collapse-toggle" onclick="_envToggleGroup(\'' + expandKey + '\')">'
        + (expanded ? '&#9662;' : '&#9656;') + ' ' + unsetEntries.length + ' not configured</button>';
      if (expanded) {
        for (const [k, info] of unsetEntries) html += _envRowHtml(k, info, false);
      }
    }
    html += '</div></div>';
  }

  root.innerHTML = html;
}
