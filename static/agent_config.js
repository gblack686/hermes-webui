/* Agent Config schema editor panel (B2 — 9119 -> webui migration).
 *
 * Vanilla-JS reimplementation of the 9119 React ConfigPage.tsx + AutoField.tsx
 * (~870 LOC) for the Hermes *agent* config.yaml. Talks to the namespaced
 * /api/agent-config* routes (NOT WebUI's own /api/settings). MVP-first per the
 * migration plan: the raw-YAML round-trip editor is the primary, always-working
 * path; the schema form layers on top of it. Writes flow through the global
 * api() helper, which injects the WebUI CSRF token transparently.
 */

const _acState = {
  config: null,        // normalized nested config (model flattened to string)
  schema: null,        // { dotKey: {type, description, category, options?} }
  categoryOrder: [],
  defaults: null,
  yamlMode: false,
  yamlText: '',
  yamlPath: '',
  yamlIsFixture: false,
  activeCategory: '',
  search: '',
  loaded: false,
};

/* ── nested get/set by dot path (mirrors 9119 lib/nested) ── */
function _acGet(obj, key) {
  if (!obj) return undefined;
  const parts = String(key).split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}
function _acSet(obj, key, value) {
  const parts = String(key).split('.');
  const root = (obj && typeof obj === 'object') ? { ...obj } : {};
  let cur = root;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    cur[p] = (cur[p] && typeof cur[p] === 'object' && !Array.isArray(cur[p])) ? { ...cur[p] } : {};
    cur = cur[p];
  }
  cur[parts[parts.length - 1]] = value;
  return root;
}

function _acEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _acPrettyCat(cat) {
  return String(cat || '').charAt(0).toUpperCase() + String(cat || '').slice(1);
}

function _acLabel(key) {
  const raw = String(key).split('.').pop() || key;
  return raw.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

/* ── data loading ── */
async function loadAgentConfig() {
  const root = document.getElementById('agentConfigRoot');
  if (!root) return;
  if (!_acState.loaded) {
    root.innerHTML = '<div class="agentcfg-loading" data-i18n="loading">Loading...</div>';
    try {
      const [cfg, schemaResp, defaults] = await Promise.all([
        api('/api/agent-config', { timeoutToast: false }).catch(() => ({})),
        api('/api/agent-config/schema', { timeoutToast: false }).catch(() => ({ fields: {}, category_order: [] })),
        api('/api/agent-config/defaults', { timeoutToast: false }).catch(() => ({})),
      ]);
      _acState.config = (cfg && typeof cfg === 'object') ? cfg : {};
      _acState.schema = (schemaResp && schemaResp.fields) || {};
      _acState.categoryOrder = (schemaResp && schemaResp.category_order) || [];
      _acState.defaults = (defaults && typeof defaults === 'object') ? defaults : {};
      _acState.loaded = true;
    } catch (e) {
      root.innerHTML = '<div class="agentcfg-error">Failed to load agent config: ' + _acEsc(e && e.message) + '</div>';
      return;
    }
  }
  _acRender();
}

async function _acLoadYaml() {
  try {
    const resp = await api('/api/agent-config/raw', { timeoutToast: false });
    _acState.yamlText = (resp && resp.yaml) || '';
    _acState.yamlPath = (resp && resp.path) || '';
    _acState.yamlIsFixture = !!(resp && resp.is_fixture);
  } catch (e) {
    _acState.yamlText = '';
    if (typeof showToast === 'function') showToast('Failed to load raw YAML', 4000, 'error');
  }
}

/* ── categories ── */
function _acCategories() {
  const schema = _acState.schema || {};
  const all = [...new Set(Object.values(schema).map(s => String(s.category || 'general')))];
  const ordered = (_acState.categoryOrder || []).filter(c => all.includes(c));
  const extra = all.filter(c => !(_acState.categoryOrder || []).includes(c)).sort();
  return [...ordered, ...extra];
}

function _acCategoryCounts() {
  const counts = {};
  for (const s of Object.values(_acState.schema || {})) {
    const cat = String(s.category || 'general');
    counts[cat] = (counts[cat] || 0) + 1;
  }
  return counts;
}

function _acMatchedFields() {
  const q = _acState.search.trim().toLowerCase();
  const schema = _acState.schema || {};
  if (!q) {
    return Object.entries(schema).filter(([, s]) => String(s.category || 'general') === _acState.activeCategory);
  }
  return Object.entries(schema).filter(([key, s]) => {
    const label = (key.split('.').pop() || key).replace(/_/g, ' ');
    return key.toLowerCase().includes(q) ||
      label.toLowerCase().includes(q) ||
      String(s.category || '').toLowerCase().includes(q) ||
      String(s.description || '').toLowerCase().includes(q);
  });
}

/* ── render ── */
function _acRender() {
  const root = document.getElementById('agentConfigRoot');
  if (!root) return;

  const toolbar = `
    <div class="agentcfg-toolbar">
      <code class="agentcfg-path" title="${_acEsc(_acState.yamlPath)}">${_acEsc(_acState.yamlPath || 'config.yaml')}</code>
      <div class="agentcfg-toolbar-actions">
        <button type="button" class="agentcfg-btn" id="agentcfgModeBtn">${_acState.yamlMode ? 'Form' : 'YAML'}</button>
        <button type="button" class="agentcfg-btn agentcfg-btn-primary" id="agentcfgSaveBtn">Save</button>
      </div>
    </div>`;

  if (_acState.yamlMode) {
    const fixtureNote = _acState.yamlIsFixture
      ? '<div class="agentcfg-note">Showing a sample fixture (no live config.yaml found / hermes_cli unavailable). Saving writes to the resolved config path.</div>'
      : '';
    root.innerHTML = toolbar + fixtureNote +
      '<textarea class="agentcfg-yaml" id="agentcfgYaml" spellcheck="false">' + _acEsc(_acState.yamlText) + '</textarea>';
    const ta = document.getElementById('agentcfgYaml');
    if (ta) ta.addEventListener('input', () => { _acState.yamlText = ta.value; });
  } else {
    const cats = _acCategories();
    if (!_acState.activeCategory && cats.length) _acState.activeCategory = cats[0];
    const counts = _acCategoryCounts();
    const searching = _acState.search.trim().length > 0;
    const sidebar = cats.map(cat => `
      <button type="button" class="agentcfg-cat ${(!searching && _acState.activeCategory === cat) ? 'active' : ''}" data-cat="${_acEsc(cat)}">
        <span class="agentcfg-cat-name">${_acEsc(_acPrettyCat(cat))}</span>
        <span class="agentcfg-cat-count">${counts[cat] || 0}</span>
      </button>`).join('');

    const fields = _acMatchedFields();
    const fieldsHtml = fields.length
      ? fields.map(([key, s]) => _acRenderField(key, s)).join('')
      : '<div class="agentcfg-empty">No fields ' + (searching ? 'match "' + _acEsc(_acState.search) + '"' : 'in this section') + '.</div>';

    root.innerHTML = toolbar + `
      <div class="agentcfg-search-row">
        <input type="text" class="agentcfg-search" id="agentcfgSearch" placeholder="Search fields..." value="${_acEsc(_acState.search)}">
      </div>
      <div class="agentcfg-body">
        <aside class="agentcfg-sidebar">${sidebar}</aside>
        <div class="agentcfg-fields">${fieldsHtml}</div>
      </div>`;

    const search = document.getElementById('agentcfgSearch');
    if (search) {
      search.addEventListener('input', () => { _acState.search = search.value; _acRender();
        const s2 = document.getElementById('agentcfgSearch'); if (s2) { s2.focus(); s2.setSelectionRange(s2.value.length, s2.value.length); } });
    }
    root.querySelectorAll('.agentcfg-cat').forEach(btn => {
      btn.addEventListener('click', () => { _acState.search = ''; _acState.activeCategory = btn.dataset.cat; _acRender(); });
    });
    _acWireFields();
  }

  const modeBtn = document.getElementById('agentcfgModeBtn');
  if (modeBtn) modeBtn.addEventListener('click', async () => {
    _acState.yamlMode = !_acState.yamlMode;
    if (_acState.yamlMode) await _acLoadYaml();
    _acRender();
  });
  const saveBtn = document.getElementById('agentcfgSaveBtn');
  if (saveBtn) saveBtn.addEventListener('click', _acState.yamlMode ? _acSaveYaml : _acSaveForm);
}

function _acRenderField(key, s) {
  const val = (key === 'model_context_length' || key === 'model')
    ? _acState.config[key]
    : _acGet(_acState.config, key);
  const label = _acLabel(key);
  const hintKey = key.includes('.') ? `<span class="agentcfg-keypath">${_acEsc(key)}</span>` : '';
  const hintDesc = s.description ? `<span class="agentcfg-desc">${_acEsc(s.description)}</span>` : '';
  const hint = (hintKey || hintDesc) ? `<div class="agentcfg-hint">${hintKey}${hintDesc}</div>` : '';
  const type = s.type;
  const dataAttr = `data-key="${_acEsc(key)}"`;

  // Nested objects / lists-of-objects -> JSON textarea (MVP fallback).
  const isObj = val && typeof val === 'object' && !Array.isArray(val);
  const isObjList = Array.isArray(val) && val.some(i => i && typeof i === 'object');
  if (isObj || isObjList) {
    return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
      <textarea class="agentcfg-json" data-json="1" ${dataAttr} spellcheck="false">${_acEsc(JSON.stringify(val, null, 2))}</textarea></div>`;
  }

  if (type === 'boolean') {
    return `<div class="agentcfg-field agentcfg-field-bool">
      <div class="agentcfg-flabel-wrap"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}</div>
      <input type="checkbox" class="agentcfg-check" ${dataAttr} ${val ? 'checked' : ''}></div>`;
  }
  if (type === 'select') {
    const opts = (s.options || []).map(o =>
      `<option value="${_acEsc(o)}" ${String(val == null ? '' : val) === o ? 'selected' : ''}>${_acEsc(o || '(none)')}</option>`).join('');
    return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
      <select class="agentcfg-input" ${dataAttr}>${opts}</select></div>`;
  }
  if (type === 'number') {
    return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
      <input type="number" class="agentcfg-input" data-num="1" ${dataAttr} value="${val == null ? '' : _acEsc(val)}"></div>`;
  }
  if (type === 'list') {
    const joined = Array.isArray(val) ? val.join(', ') : (val == null ? '' : val);
    return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
      <input type="text" class="agentcfg-input" data-list="1" ${dataAttr} value="${_acEsc(joined)}" placeholder="comma-separated values"></div>`;
  }
  if (type === 'text') {
    return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
      <textarea class="agentcfg-textarea" ${dataAttr}>${_acEsc(val == null ? '' : val)}</textarea></div>`;
  }
  return `<div class="agentcfg-field"><label class="agentcfg-flabel">${_acEsc(label)}</label>${hint}
    <input type="text" class="agentcfg-input" ${dataAttr} value="${_acEsc(val == null ? '' : val)}"></div>`;
}

function _acApply(key, value) {
  if (key === 'model_context_length' || key === 'model') {
    _acState.config = { ..._acState.config, [key]: value };
  } else {
    _acState.config = _acSet(_acState.config, key, value);
  }
}

function _acWireFields() {
  const root = document.getElementById('agentConfigRoot');
  if (!root) return;
  root.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.type === 'checkbox') {
      el.addEventListener('change', () => _acApply(key, el.checked));
    } else if (el.dataset.json) {
      el.addEventListener('change', () => {
        try { _acApply(key, JSON.parse(el.value)); el.classList.remove('agentcfg-invalid'); }
        catch (e) { el.classList.add('agentcfg-invalid'); }
      });
    } else if (el.dataset.num) {
      el.addEventListener('input', () => {
        const raw = el.value;
        if (raw === '') { _acApply(key, 0); return; }
        const n = Number(raw);
        if (!Number.isNaN(n)) _acApply(key, n);
      });
    } else if (el.dataset.list) {
      el.addEventListener('input', () => {
        _acApply(key, el.value.split(',').map(x => x.trim()).filter(Boolean));
      });
    } else {
      el.addEventListener('input', () => _acApply(key, el.value));
    }
  });
}

/* ── save ── */
async function _acSaveForm() {
  if (!_acState.config) return;
  const btn = document.getElementById('agentcfgSaveBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    await api('/api/agent-config', { method: 'PUT', body: JSON.stringify({ config: _acState.config }) });
    if (typeof showToast === 'function') showToast('Agent config saved', 3000, 'success');
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to save: ' + (e && e.message), 5000, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  }
}

async function _acSaveYaml() {
  const btn = document.getElementById('agentcfgSaveBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    await api('/api/agent-config/raw', { method: 'PUT', body: JSON.stringify({ yaml: _acState.yamlText }) });
    if (typeof showToast === 'function') showToast('Agent config (YAML) saved', 3000, 'success');
    // Refresh the structured config so a later Form switch reflects the new YAML.
    _acState.loaded = false;
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to save YAML: ' + (e && e.message), 5000, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  }
}
