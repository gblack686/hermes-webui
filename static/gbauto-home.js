/*
 * gbauto-home.js — GBauto operator /home panel (Phase 3, approval example)
 * Ported from hermes-agent web/src/pages/HomePage.tsx (9119) into the native
 * WebUI panel system. Static content; the composer is rewired to the native
 * chat send path (switchPanel('chat') -> set #msg -> send()) instead of the
 * 9119 navigate('/chat?prompt=...&send=1').
 * Plan: 2026-07-01-hermes-webui-unified-gbauto-shell-tac-plan
 */
(function () {
  'use strict';

  var QUICK_PROMPTS = [
    'Summarize the latest ecom telemetry and open risks.',
    'Review recent failed skill runs and tell me what to fix first.',
    'Find the newest report artifacts and recommend the next polish pass.'
  ];

  // label | meta | route | image | description   (verbatim from HomePage.tsx)
  var HOME_CARDS = [
    ['Hermes Chat', 'Live', '/chat', 'static/skill-art/ai.jpg', 'Resume a Hermes session or start a new operator thread.'],
    ['Observability', 'Supabase', '/logs', 'static/skill-art/mlops.jpg', 'Review agent runs, traces, failures, and Supabase telemetry.'],
    ['Skills', 'Catalog', '/skills', 'static/skill-art/design-system.jpg', 'Browse installed skills, Aura references, and Canopy-backed catalogs.'],
    ['Artifacts', 'Reports', '/artifacts', 'static/skill-art/marketing.jpg', 'Open report artifacts, client packages, and approved templates.'],
    ['Repos', 'Git', '/repos', 'static/skill-art/motion.jpg', 'Inspect repo activity, commits, and project-level source maps.']
  ];

  // label | route   (route strip; icons kept minimal/mono per footprint)
  var ROUTE_STRIP = [
    ['Overview', '/overview'],
    ['Repos', '/repos'],
    ['Skills', '/skills'],
    ['Langfuse', '/langfuse'],
    ['Artifacts', '/artifacts']
  ];

  function reg() { return (typeof window !== 'undefined' && window.GBAUTO_PAGE_REGISTRY) || null; }
  function routeAvailable(route) { var r = reg(); return r ? r.isRouteAvailable(route) : false; }
  function ownerPanel(route) { var r = reg(); return r ? r.ownerPanelFor(route) : null; }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) { for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); } }
    if (kids) { for (var i = 0; i < kids.length; i++) { var c = kids[i]; n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); } }
    return n;
  }

  // Native chat send path (replaces 9119 navigate). Sets the composer and sends.
  function submitPrompt(text) {
    text = (text || '').trim();
    if (!text) return;
    if (typeof switchPanel === 'function') switchPanel('chat');
    var msg = document.getElementById('msg');
    if (msg) {
      msg.value = text;
      try { msg.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
      if (typeof autosize === 'function') { try { autosize(msg); } catch (_) {} }
      try { msg.focus(); } catch (_) {}
    }
    if (typeof send === 'function') { try { send(); } catch (_) {} }
  }

  // Navigate to a target route's owner panel (native switchPanel). Unavailable
  // routes are rendered disabled and never reach here.
  function goRoute(route, ev) {
    if (ev && ev.preventDefault) ev.preventDefault();
    if (!routeAvailable(route)) return;
    var panel = ownerPanel(route);
    if (panel && typeof switchPanel === 'function') switchPanel(panel, { fromRailClick: true });
  }

  function buildRouteStrip() {
    var strip = el('section', { 'class': 'home-chat-route-strip', 'aria-label': 'Primary routes' });
    ROUTE_STRIP.forEach(function (item) {
      var label = item[0], route = item[1];
      var available = routeAvailable(route);
      var a = el('a', {
        'href': available ? '#' + route : null,
        'role': 'link',
        'aria-disabled': available ? null : 'true',
        'title': available ? label : (label + ' — not yet available in this shell')
      }, [el('span', null, [label])]);
      if (available) a.addEventListener('click', function (ev) { goRoute(route, ev); });
      strip.appendChild(a);
    });
    return strip;
  }

  function buildGallery() {
    var gallery = el('section', { 'class': 'home-chat-gallery', 'aria-label': 'Home gallery' });
    HOME_CARDS.forEach(function (card) {
      var label = card[0], meta = card[1], route = card[2], art = card[3], desc = card[4];
      var available = routeAvailable(route);
      var a = el('a', {
        'class': 'home-chat-card',
        'href': available ? '#' + route : null,
        'aria-disabled': available ? null : 'true',
        'style': available ? null : 'opacity:.5;pointer-events:none;cursor:not-allowed'
      }, [
        el('img', { 'alt': '', 'decoding': 'async', 'loading': 'lazy', 'src': art }),
        el('span', { 'class': 'home-chat-card-meta' }, [meta]),
        el('strong', null, [label]),
        el('p', null, [desc]),
        el('span', { 'class': 'home-chat-card-action' }, [available ? 'Open →' : 'Soon'])
      ]);
      if (available) a.addEventListener('click', function (ev) { goRoute(route, ev); });
      gallery.appendChild(a);
    });
    return gallery;
  }

  function buildComposer() {
    var textarea = el('textarea', { 'aria-label': 'Message Hermes', 'rows': '4', 'placeholder': 'Ask Hermes to investigate, build, review, scrape, or summarize...' });

    var submit = el('button', { 'class': 'home-chat-submit', 'type': 'submit', 'disabled': 'disabled' }, ['Send to Hermes']);

    var shortcuts = el('div', { 'class': 'home-chat-shortcuts' });
    QUICK_PROMPTS.forEach(function (p) {
      var b = el('button', { 'type': 'button', 'title': p }, [p]);
      b.addEventListener('click', function () { submitPrompt(p); });
      shortcuts.appendChild(b);
    });

    var footer = el('div', { 'class': 'home-chat-composer-footer' }, [shortcuts, submit]);
    var form = el('form', { 'class': 'home-chat-composer' }, [textarea, footer]);

    textarea.addEventListener('input', function () { submit.disabled = !textarea.value.trim(); });
    form.addEventListener('submit', function (ev) { ev.preventDefault(); submitPrompt(textarea.value); textarea.value = ''; submit.disabled = true; });
    return form;
  }

  function buildHome() {
    var hero = el('section', { 'class': 'home-chat-hero', 'aria-labelledby': 'home-title' }, [
      el('div', { 'class': 'home-chat-kicker' }, ['Home chat gallery']),
      el('h1', { 'id': 'home-title' }, ['Create operational momentum with Hermes.']),
      el('p', { 'class': 'home-chat-intro' }, ['A GBauto home surface adapted from the approved Aura home template: chat first, routes nearby, and the dashboard context one click away.']),
      buildComposer()
    ]);
    var page = el('div', { 'class': 'home-chat-page' }, [hero, buildRouteStrip(), buildGallery()]);
    return page;
  }

  var _rendered = false;
  function render() {
    var mount = document.getElementById('mainHome');
    if (!mount) return;
    if (_rendered && mount.firstChild) return; // static; render once
    mount.innerHTML = '';
    mount.appendChild(buildHome());
    _rendered = true;
  }

  // Expose for panels.js lazy-load hook and re-render on availability changes.
  window.gbautoRenderHome = render;
  window.gbautoHomeSubmitPrompt = submitPrompt;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render, { once: true });
  } else {
    render();
  }
})();
