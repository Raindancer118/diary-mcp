'use strict';
/* diary-web „Observatorium" — vanilla JS, no dependencies. Design: Design.md */

// ── utilities ────────────────────────────────────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c]);
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
const TYPES = ['feedback', 'user', 'project', 'reference', 'note'];  // fixed order (validated palette)
const TYPE_LABEL = { feedback: 'Feedback', user: 'Nutzer', project: 'Projekt', reference: 'Referenz', note: 'Notiz', category: 'Kategorie' };
const TYPE_HEX = { feedback: '#cc7d1b', user: '#3e8cc9', project: '#819f47', reference: '#7555a8', note: '#ca5551', category: '#5c5446' };
const REL_LABEL = { related: 'verwandt', supports: 'stützt', contradicts: 'widerspricht', requires: 'benötigt', derived_from: 'abgeleitet von' };
const CHARS_PER_TOKEN = 3.7;
const nf = new Intl.NumberFormat('de-DE');
const fmt = n => nf.format(Math.round(n ?? 0));
const compact = n => {
  n = Math.round(n ?? 0);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace('.', ',') + ' Mio.';
  if (n >= 1e4) return Math.round(n / 1e3) + 'k';
  return nf.format(n);
};
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0) + ' %';
const parseTs = s => (s ? new Date(String(s).replace(' ', 'T')) : null);
const dayDiff = d => Math.floor((startOfDay(new Date()) - startOfDay(d)) / 864e5);
const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
function relTime(s) {
  const d = parseTs(s);
  if (!d || isNaN(d)) return '—';
  const n = dayDiff(d);
  if (n <= 0) return 'heute';
  if (n === 1) return 'gestern';
  if (n < 30) return `vor ${n} Tagen`;
  if (n < 365) return `vor ${Math.round(n / 30)} Mon.`;
  return `vor ${Math.round(n / 365)} J.`;
}
const dateLong = d => d.toLocaleDateString('de-DE', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
const dateShort = d => d.toLocaleDateString('de-DE', { day: 'numeric', month: 'short' });
const fmtBytes = n => n >= 1 << 30 ? (n / (1 << 30)).toFixed(1).replace('.', ',') + ' GB'
  : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1).replace('.', ',') + ' MB'
  : n >= 1024 ? Math.round(n / 1024) + ' KB' : (n ?? 0) + ' B';
const easeOut = t => 1 - Math.pow(1 - t, 4);
const easeInOut = t => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

const postJSON = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch { /* not json */ }
    throw new Error(`${r.status} ${detail || url}`);
  }
  return r.json();
}

function countUp(el) {
  const to = Number(el.dataset.to);
  const f = el.dataset.fmt === 'compact' ? compact : el.dataset.fmt === 'pct' ? v => Math.round(v) + ' %' : fmt;
  if (REDUCED.matches || document.hidden || !isFinite(to) || to === 0) { el.textContent = f(to || 0); return; }
  const dur = 1300, t0 = performance.now();
  const step = t => {
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = f(to * easeOut(p));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function withTransition(fn) {
  if (document.startViewTransition && !REDUCED.matches && !document.hidden) document.startViewTransition(fn);
  else fn();
}

let toastTimer;
function toast(msg, bad = false, ms = 7000) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  el.style.animation = 'none'; void el.offsetWidth; el.style.animation = '';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}
$('#toast').addEventListener('click', e => { e.currentTarget.hidden = true; });

// ── tooltip (textContent only — never HTML) ──────────────────────────────
const tip = $('#tip');
function showTip(x, y, main, sub) {
  tip.replaceChildren();
  const b = document.createElement('b'); b.textContent = main; tip.append(b);
  if (sub) { const s = document.createElement('span'); s.className = 't-sub'; s.textContent = sub; tip.append(s); }
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + 'px';
  tip.style.top = (y + 16 + r.height > innerHeight ? y - r.height - 10 : y + 16) + 'px';
}
const hideTip = () => { tip.hidden = true; };
document.addEventListener('pointermove', e => {
  const el = e.target.closest?.('[data-tip]');
  if (el) showTip(e.clientX, e.clientY, el.dataset.tip, el.dataset.tipSub);
  else if (!tip.dataset.owner) hideTip();
});

// ── sky: two seeded star tiles, drifting at different speeds (CSS) ───────
function starTile(seed, count, rMin, rMax, glow) {
  let s = seed;
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
  const c = document.createElement('canvas');
  c.width = c.height = 512;
  const g = c.getContext('2d');
  const tints = ['255,244,226', '255,226,190', '214,226,255', '255,255,255'];
  for (let i = 0; i < count; i++) {
    const x = rnd() * 512, y = rnd() * 512, r = rMin + rnd() * (rMax - rMin), a = .25 + rnd() * .7;
    const tint = tints[Math.floor(rnd() * tints.length)];
    if (glow && rnd() > .6) {
      const grd = g.createRadialGradient(x, y, 0, x, y, r * 5);
      grd.addColorStop(0, `rgba(${tint},${a * .35})`);
      grd.addColorStop(1, `rgba(${tint},0)`);
      g.fillStyle = grd; g.fillRect(x - r * 5, y - r * 5, r * 10, r * 10);
    }
    g.fillStyle = `rgba(${tint},${a})`;
    g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
  }
  return c.toDataURL('image/png');
}
$('.sky-layer.far').style.backgroundImage = `url(${starTile(7, 170, .3, .8, false)})`;
$('.sky-layer.near').style.backgroundImage = `url(${starTile(42, 38, .6, 1.4, true)})`;

// ── state ────────────────────────────────────────────────────────────────
const state = { nodes: [], byPath: new Map(), bySlug: new Map(), renderBase: '', current: null, view: 'archive', loadSeq: 0 };

// ── router: #/ · #/m/<path> · #/karte · #/messwerte ─────────────────────
const memHash = path => '#/m' + encodeURI(path);
function parseHash() {
  let h;
  try { h = decodeURI(location.hash.slice(1)); } catch { h = '/'; }
  if (h.startsWith('/m/')) return { view: 'archive', path: h.slice(2) };
  if (h === '/karte') return { view: 'atlas' };
  if (h === '/messwerte') return { view: 'stats' };
  if (h === '/vorschlaege') return { view: 'review' };
  return { view: 'archive', path: null };
}
function go(path) { location.hash = memHash(path); }

function route() {
  const r = parseHash();
  const changed = r.view !== state.view;
  const apply = () => {
    document.body.dataset.view = r.view;
    state.view = r.view;
    atlas.setActive(r.view === 'atlas');
    if (r.view === 'archive') {
      if (r.path) openMemory(r.path);
      else showWelcome();
    }
    if (r.view === 'stats') stats.enter();
    if (r.view === 'review') review.enter();
  };
  if (changed) withTransition(apply); else apply();
}
addEventListener('hashchange', route);

// ── tree ─────────────────────────────────────────────────────────────────
const tree = { roots: [], els: new Map() };

function buildTree(nodes) {
  const map = new Map(nodes.map(n => [n.path, { ...n, kids: [], total: 0 }]));
  const roots = [];
  for (const n of map.values()) {
    let parent = null, p = n.path;
    while (!parent && p.lastIndexOf('/') > 0) { p = p.slice(0, p.lastIndexOf('/')); parent = map.get(p); }
    (parent ? parent.kids : roots).push(n);
  }
  const count = n => (n.total = n.kids.reduce((s, k) => s + 1 + count(k), 0));
  roots.forEach(count);
  const sort = list => { list.sort((a, b) => (b.kids.length > 0) - (a.kids.length > 0) || a.path.localeCompare(b.path)); list.forEach(k => sort(k.kids)); };
  sort(roots);
  return roots;
}

function renderTree() {
  tree.roots = buildTree(state.nodes);
  tree.els.clear();
  const host = $('#tree');
  host.replaceChildren(...tree.roots.map(n => treeNode(n, 1)));
  $('#tree-count').textContent = fmt(state.nodes.length);
  const first = host.querySelector('.t-row');
  if (first) first.tabIndex = 0;
}

function treeNode(n, level) {
  const wrap = document.createElement('div');
  wrap.className = 't-node';
  wrap.setAttribute('role', 'none');
  const row = document.createElement('div');
  row.className = 't-row';
  row.tabIndex = -1;
  row.setAttribute('role', 'treeitem');
  row.setAttribute('aria-level', level);
  if (n.kids.length) row.setAttribute('aria-expanded', 'false');
  row.title = n.title || n.path;
  row.innerHTML = `<span class="t-caret${n.kids.length ? '' : ' leaf'}">▶</span><span class="t-dot t-${esc(n.type)}"></span>`
    + `<span class="t-slug">${esc(n.slug || n.path.split('/').pop())}</span>`
    + (n.kids.length ? `<span class="t-n">${n.total}</span>` : '');
  wrap.append(row);
  if (n.kids.length) {
    const kids = document.createElement('div');
    kids.className = 't-kids';
    kids.setAttribute('role', 'group');
    const inner = document.createElement('div');
    inner.append(...n.kids.map(k => treeNode(k, level + 1)));
    kids.append(inner);
    wrap.append(kids);
  }
  row.addEventListener('click', () => {
    if (n.kids.length) toggleNode(n.path);
    go(n.path);
  });
  tree.els.set(n.path, { wrap, row, node: n });
  return wrap;
}

function toggleNode(path, open) {
  const e = tree.els.get(path);
  if (!e || !e.node.kids.length) return;
  const now = e.wrap.classList.toggle('open', open);
  e.row.setAttribute('aria-expanded', String(now));
}

function revealInTree(path) {
  $$('.t-row.active').forEach(r => r.classList.remove('active'));
  let p = path, e = tree.els.get(p);
  while (!e && p.lastIndexOf('/') > 0) { p = p.slice(0, p.lastIndexOf('/')); e = tree.els.get(p); }
  if (!e) return;
  let anc = e.wrap.parentElement?.closest('.t-node');
  while (anc) { anc.classList.add('open'); anc.firstElementChild.setAttribute('aria-expanded', 'true'); anc = anc.parentElement?.closest('.t-node'); }
  e.row.classList.add('active');
  focusRow(e.row, false);
  e.row.scrollIntoView({ block: 'nearest', behavior: REDUCED.matches ? 'auto' : 'smooth' });
}

function focusRow(row, focus = true) {
  $$('.t-row[tabindex="0"]').forEach(r => { r.tabIndex = -1; });
  row.tabIndex = 0;
  if (focus) row.focus();
}

function visibleRows() {
  return $$('.t-row', $('#tree')).filter(r => {
    if (r.closest('.t-node').hidden) return false;
    let anc = r.closest('.t-node').parentElement?.closest('.t-node');
    while (anc) { if (!anc.classList.contains('open')) return false; anc = anc.parentElement?.closest('.t-node'); }
    return true;
  });
}

$('#tree').addEventListener('keydown', e => {
  const row = e.target.closest('.t-row');
  if (!row) return;
  const path = [...tree.els.values()].find(x => x.row === row)?.node.path;
  const rows = visibleRows(), i = rows.indexOf(row);
  const move = j => { if (rows[j]) { e.preventDefault(); focusRow(rows[j]); } };
  switch (e.key) {
    case 'ArrowDown': move(i + 1); break;
    case 'ArrowUp': move(i - 1); break;
    case 'Home': move(0); break;
    case 'End': move(rows.length - 1); break;
    case 'ArrowRight': e.preventDefault(); toggleNode(path, true); break;
    case 'ArrowLeft': {
      e.preventDefault();
      const w = row.closest('.t-node');
      if (w.classList.contains('open')) toggleNode(path, false);
      else { const up = w.parentElement?.closest('.t-node'); if (up) focusRow(up.firstElementChild); }
      break;
    }
    case 'Enter': case ' ': e.preventDefault(); row.click(); break;
  }
});

let filterTimer;
$('#tree-filter').addEventListener('input', e => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => filterTree(e.target.value.trim().toLowerCase()), 120);
});
function filterTree(q) {
  let shown = 0;
  const walk = n => {
    const e = tree.els.get(n.path);
    const kidHit = n.kids.map(walk).some(Boolean);
    const self = !q || n.path.toLowerCase().includes(q) || (n.title || '').toLowerCase().includes(q);
    const vis = self || kidHit;
    e.wrap.hidden = !vis;
    if (vis) shown++;
    if (q) e.wrap.classList.toggle('open', kidHit);
    const slug = e.row.querySelector('.t-slug');
    const text = n.slug || n.path.split('/').pop();
    const at = q ? text.toLowerCase().indexOf(q) : -1;
    slug.innerHTML = at < 0 ? esc(text) : esc(text.slice(0, at)) + '<mark>' + esc(text.slice(at, at + q.length)) + '</mark>' + esc(text.slice(at + q.length));
    return vis;
  };
  tree.roots.forEach(walk);
  if (!q) { $$('.t-node.open').forEach(w => w.classList.remove('open')); if (state.current) revealInTree(state.current); }
  $('#tree-count').textContent = q ? `${fmt(shown)} / ${fmt(state.nodes.length)}` : fmt(state.nodes.length);
}

// ── welcome ──────────────────────────────────────────────────────────────
function showWelcome() {
  state.current = null;
  $('#memory').hidden = true;
  $('#welcome').hidden = false;
  $('#margin-node').hidden = true;
  $('#margin-empty').hidden = false;
  $$('.t-row.active').forEach(r => r.classList.remove('active'));
  $('.views a[data-view="archive"]').setAttribute('href', '#/');
}

function renderWelcome() {
  const content = state.nodes.filter(n => n.type !== 'category');
  const el = $('[data-count="welcome-total"]');
  el.dataset.to = content.length;
  countUp(el);
  const branches = new Set(state.nodes.map(n => n.path.split('/')[1])).size;
  const latest = content.reduce((m, n) => (n.updated_at > (m?.updated_at || '') ? n : m), null);
  $('#welcome-sub').innerHTML = `${fmt(state.nodes.length)} Pfade in ${branches} Zweigen, zuletzt bewegt ${esc(relTime(latest?.updated_at))}. `
    + `<kbd>Ctrl K</kbd> sucht, <kbd>2</kbd> öffnet die Sternkarte, <kbd>3</kbd> die Messwerte.`;
  const recent = [...content].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '')).slice(0, 9);
  $('#recent').innerHTML = recent.map(n => `<li><a href="${esc(memHash(n.path))}">
      <span class="sw dot-${esc(n.type)}"></span>
      <span><span class="r-title">${esc(n.title || n.slug)}</span><span class="r-path">${esc(n.path)}</span></span>
      <time datetime="${esc(n.updated_at)}">${esc(relTime(n.updated_at))}</time></a></li>`).join('');

  const counts = countTypes(state.nodes);
  const max = Math.max(1, ...Object.values(counts));
  $('#spectrum-mini').className = 'spectrum-mini';
  $('#spectrum-mini').innerHTML = TYPES.filter(t => counts[t]).map(t => `
    <div class="spec-row"><span class="sw dot-${t}"></span><span>${TYPE_LABEL[t]}</span><b>${fmt(counts[t])}</b>
    <span class="spec-track"><i class="t-${t}" style="--v:${counts[t] / max}"></i></span></div>`).join('');
}
const countTypes = nodes => nodes.reduce((m, n) => ((m[n.type] = (m[n.type] || 0) + 1), m), {});

// ── memory reader ────────────────────────────────────────────────────────
async function openMemory(path) {
  const seq = ++state.loadSeq;
  let node;
  try { node = await api('/api/node?path=' + encodeURIComponent(path)); }
  catch (err) {
    if (seq !== state.loadSeq) return;
    toast(`Diese Erinnerung gibt es nicht (mehr): ${path}`, true);
    showWelcome();
    return;
  }
  if (seq !== state.loadSeq) return;
  state.current = path;
  $('.views a[data-view="archive"]').setAttribute('href', memHash(path));
  $('#welcome').hidden = true;
  const mem = $('#memory');
  mem.hidden = false;

  const parts = path.split('/').filter(Boolean);
  $('#crumbs').innerHTML = parts.map((p, i) => {
    const sub = '/' + parts.slice(0, i + 1).join('/');
    return (i ? '<span class="sep">/</span>' : '<span class="sep">/</span>')
      + (state.byPath.has(sub) && sub !== path ? `<button type="button" data-go="${esc(sub)}">${esc(p)}</button>` : `<button type="button" disabled>${esc(p)}</button>`);
  }).join('');
  $('#memory-title').textContent = node.title || parts.at(-1);

  const expired = node.valid_until && parseTs(node.valid_until) < new Date();
  const imp = node.importance ?? 0;
  const stars = Math.round(imp * 5);
  $('#chips').innerHTML = [
    `<span class="chip"><span class="sw dot-${esc(node.type)}"></span>${esc(TYPE_LABEL[node.type] || node.type)}</span>`,
    node.origin === 'extracted' ? '<span class="chip muted">auto-extrahiert</span>' : '',
    node.pin_triggers?.length ? `<span class="chip">gepinnt · ${esc(node.pin_triggers.join(', '))}</span>` : '',
    expired ? '<span class="chip warn">▲ abgelaufen</span>' : '',
    ...(node.tags || []).map(t => `<span class="chip muted">#${esc(t)}</span>`),
    `<span class="magnitude" title="Wichtigkeit ${Math.round(imp * 100)} %">${[1, 2, 3, 4, 5].map(i => `<i class="${i <= stars ? 'on' : ''}" style="--i:${i}">✦</i>`).join('')}<span>${imp.toFixed(2)}</span></span>`,
  ].join('');

  state.renderBase = path;
  $('#prose').innerHTML = node.body ? markdown(node.body) : '<p class="empty">Diese Erinnerung hat keinen Text, nur Kinder im Baum.</p>';
  mem.classList.remove('enter'); void mem.offsetWidth;
  if (!REDUCED.matches) mem.classList.add('enter');
  $('#reader').scrollTop = 0;

  renderMargin(node);
  revealInTree(path);
}

$('#crumbs').addEventListener('click', e => { const b = e.target.closest('[data-go]'); if (b) go(b.dataset.go); });

function renderMargin(node) {
  $('#margin-empty').hidden = true;
  $('#margin-node').hidden = false;
  const chars = (node.body || '').length + (node.title || '').length;
  const facts = [
    ['Geändert', node.updated_at?.slice(0, 10) || '—'],
    ['Erstellt', node.created_at?.slice(0, 10) || '—'],
    ['Gültig bis', node.valid_until?.slice(0, 10) || 'unbegrenzt'],
    ['Gelesen', `${fmt(node.access_count ?? 0)} ×`],
    ['Wichtigkeit', `${Math.round((node.importance ?? 0) * 100)} %`],
    ['Umfang', `${fmt(chars)} Z. · ≈ ${fmt(chars / CHARS_PER_TOKEN)} Tok.`],
    ['Herkunft', node.origin === 'extracted' ? 'extrahiert' : 'kuratiert'],
  ];
  $('#facts').innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');

  const linkItem = (rel, path, title, note) => `<li><button type="button" data-go="${esc(path)}">
    <span class="rel ${esc(rel)}">${rel === 'contradicts' ? '▲ ' : ''}${esc(REL_LABEL[rel] || rel)}</span>
    <span class="l-title">${esc(title || path)}</span><span class="l-path">${esc(path)}</span>
    ${note ? `<span class="l-note">${esc(note)}</span>` : ''}</button></li>`;
  const out = node.links_out || [], inn = node.links_in || [];
  $('#links-out').innerHTML = out.map(l => linkItem(l.rel_type, l.target_path, l.target_title, l.note)).join('');
  $('#links-in').innerHTML = inn.map(l => linkItem(l.rel_type, l.source_path, l.source_title)).join('');
  $('#links-out-wrap').hidden = !out.length;
  $('#links-in-wrap').hidden = !inn.length;
  renderConstellation(node, out, inn);
}
$('#margin').addEventListener('click', e => { const b = e.target.closest('[data-go]'); if (b) go(b.dataset.go); });

function renderConstellation(node, out, inn) {
  const seen = new Map();
  for (const l of out) seen.set(l.target_path, { path: l.target_path, title: l.target_title, rel: l.rel_type });
  for (const l of inn) if (!seen.has(l.source_path)) seen.set(l.source_path, { path: l.source_path, title: l.source_title, rel: l.rel_type });
  const nbrs = [...seen.values()].slice(0, 12);
  const fig = $('#constellation');
  fig.hidden = !nbrs.length;
  if (!nbrs.length) return;
  const R = 56;
  const parts = [];
  const stars = [];
  nbrs.forEach((n, i) => {
    const a = -Math.PI / 2 + (i / nbrs.length) * Math.PI * 2;
    const x = Math.cos(a) * R, y = Math.sin(a) * R;
    const meta = state.byPath.get(n.path) || {};
    const col = TYPE_HEX[meta.type] || TYPE_HEX.category;
    const r = 3 + (meta.importance ?? .5) * 3;
    const slug = n.path.split('/').pop();
    const label = slug.length > 15 ? slug.slice(0, 14) + '…' : slug;
    const anchor = Math.abs(Math.cos(a)) < .3 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end';
    const lx = x + Math.cos(a) * 11, ly = y + Math.sin(a) * 11 + (Math.sin(a) > .3 ? 6 : Math.sin(a) < -.3 ? -2 : 3);
    parts.push(`<line x1="0" y1="0" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" style="--i:${i}"${n.rel === 'contradicts' ? ' stroke="#e06c5a"' : ''}/>`);
    stars.push(`<g class="star" data-go="${esc(n.path)}" data-tip="${esc(n.title || n.path)}" data-tip-sub="${esc(REL_LABEL[n.rel] || n.rel)} · ${esc(n.path)}" style="--i:${i}" tabindex="0" role="link" aria-label="${esc(n.title || n.path)}">
      <circle r="${(r * 2.6).toFixed(1)}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" fill="${col}" opacity=".18"/>
      <circle class="core" r="${r.toFixed(1)}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" fill="${col}"/>
      <text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}">${esc(label)}</text></g>`);
  });
  const col = TYPE_HEX[node.type] || '#e8a84c';
  $('#constellation-svg').innerHTML = parts.join('') + stars.join('')
    + `<circle r="20" fill="${col}" opacity=".12"/><circle r="11" fill="${col}" opacity=".25"/><circle r="6.5" fill="#fff4e0"/>`;
}
$('#constellation-svg').addEventListener('keydown', e => {
  const g = e.target.closest('[data-go]');
  if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go(g.dataset.go); }
});

// ── markdown (escape first, then a small, safe subset) ───────────────────
const PATH_RE = /(^|[\s(>„"])(\/(?:projects|user|feedback|references|notes|links)(?:\/[\w.\-]+)*)/g;
function inline(raw) {
  const codes = [];
  let s = esc(raw).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+[^\s<.,;:)])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
    .replace(PATH_RE, (m, pre, p) => (state.byPath.has(p) ? `${pre}<a class="mem" href="${memHash(p)}">${p}</a>` : m))
    .replace(/\[\[([\w.\-\/]+)\]\]/g, (m, slug) => {
      const p = resolveWiki(slug);
      return p ? `<a class="mem" href="${memHash(p)}">${slug}</a>` : m;
    });
  return s.replace(/\u0000(\d+)\u0000/g, (_, k) => {
    const c = codes[+k];
    return state.byPath.has(c) ? `<a class="mem" href="${memHash(c)}"><code>${c}</code></a>` : `<code>${c}</code>`;
  });
}
// [[slug]] → the memory with that slug, preferring the current memory's folder
function resolveWiki(slug) {
  if (slug.startsWith('/')) return state.byPath.has(slug) ? slug : null;
  const hits = state.bySlug.get(slug.split('/').pop()) || [];
  if (!hits.length) return null;
  const dir = (state.renderBase || '').slice(0, (state.renderBase || '').lastIndexOf('/'));
  return hits.find(p => p.startsWith(dir + '/')) || hits[0];
}
function markdown(src) {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;
  const isTableSep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  const special = l => /^(```|#{1,6}\s|>\s?|\s*[-*+]\s+|\s*\d+[.)]\s+|(-{3,}|\*{3,})\s*$)/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if (l.startsWith('```')) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].startsWith('```')) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    let m;
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) {
      const tag = m[1].length <= 2 ? 'h3' : m[1].length === 3 ? 'h4' : 'h5';
      out.push(`<${tag}>${inline(m[2])}</${tag}>`); i++; continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
    if (/^>\s?/.test(l)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      out.push(`<blockquote>${buf.map(inline).join('<br>')}</blockquote>`); continue;
    }
    if (/^\s*[-*+]\s+/.test(l) || /^\s*\d+[.)]\s+/.test(l)) {
      const ordered = /^\s*\d+[.)]\s+/.test(l);
      const re = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      const items = [];
      while (i < lines.length && lines[i].trim()) {
        if (re.test(lines[i])) items.push(lines[i].replace(re, ''));
        else if (/^\s+\S/.test(lines[i]) && items.length) items[items.length - 1] += ' ' + lines[i].trim();
        else if (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) && items.length) items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''));
        else break;
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map(t => `<li>${inline(t.replace(/^\[( |x)\]\s*/i, (_, x) => (x.trim() ? '☑ ' : '☐ ')))}</li>`).join('')}</${tag}>`);
      continue;
    }
    if (l.trim().startsWith('|') && isTableSep(lines[i + 1] || '')) {
      const head = cells(l);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map(h => `<th>${inline(h)}</th>`).join('')}</tr></thead>`
        + `<tbody>${rows.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !special(lines[i]) && !(lines[i].trim().startsWith('|') && isTableSep(lines[i + 1] || ''))) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out.push(`<p>${buf.map(inline).join(' ')}</p>`);
  }
  return out.map((b, k) => b.replace(/^<(\w+)/, `<$1 style="--i:${Math.min(k, 14)}"`)).join('');
}

// ── search palette ───────────────────────────────────────────────────────
const palette = {
  el: $('#palette'), input: $('#palette-input'), list: $('#palette-results'),
  items: [], sel: 0, seq: 0, timer: 0,
  open() {
    if (this.el.open) return;
    this.el.showModal();
    this.input.value = '';
    this.showRecent();
    this.input.focus();
  },
  close() { if (this.el.open) this.el.close(); },
  showRecent() {
    const recent = state.nodes.filter(n => n.type !== 'category')
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '')).slice(0, 7);
    this.render(recent.map(n => ({ path: n.path, title: n.title, type: n.type, snippet: '' })), 'Zuletzt bewegt');
  },
  async search(q) {
    const seq = ++this.seq;
    let res;
    try { res = await api('/api/search?q=' + encodeURIComponent(q)); }
    catch (err) { if (seq === this.seq) this.render([], `Suche fehlgeschlagen: ${err.message}`); return; }
    if (seq !== this.seq) return;
    this.render(res, res.length ? '' : `Nichts gefunden für „${q}“.`);
  },
  render(items, note) {
    this.items = items;
    this.sel = 0;
    const html = items.map((r, i) => {
      const snip = r.snippet ? esc(r.snippet).replace(/«([^»]*)»/g, '<mark>$1</mark>').replace(/[«»]/g, '') : '';
      return `<li role="option" id="pr-${i}" data-i="${i}" aria-selected="${i === 0}" style="--i:${i}">
        <span class="sw dot-${esc(r.type)}"></span><span class="p-title">${esc(r.title || r.path)}</span>
        <span class="p-path">${esc(r.path)}</span>${snip ? `<span class="p-snip">${snip}</span>` : ''}</li>`;
    }).join('');
    this.list.innerHTML = (note ? `<li class="p-empty" aria-disabled="true">${esc(note)}</li>` : '') + html;
  },
  move(d) {
    if (!this.items.length) return;
    this.sel = (this.sel + d + this.items.length) % this.items.length;
    $$('li[data-i]', this.list).forEach(li => li.setAttribute('aria-selected', String(+li.dataset.i === this.sel)));
    const cur = $(`#pr-${this.sel}`);
    cur?.scrollIntoView({ block: 'nearest' });
    this.input.setAttribute('aria-activedescendant', `pr-${this.sel}`);
  },
  pick(i = this.sel) {
    const it = this.items[i];
    if (!it) return;
    this.close();
    go(it.path);
  },
};
palette.input.addEventListener('input', () => {
  clearTimeout(palette.timer);
  const q = palette.input.value.trim();
  if (!q) { palette.seq++; palette.showRecent(); return; }
  palette.timer = setTimeout(() => palette.search(q), 160);
});
palette.input.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); palette.move(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palette.move(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); palette.pick(); }
});
palette.list.addEventListener('click', e => { const li = e.target.closest('li[data-i]'); if (li) palette.pick(+li.dataset.i); });
palette.el.addEventListener('click', e => { if (e.target === palette.el) palette.close(); });
$('#open-search').addEventListener('click', () => palette.open());

document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette.open(); return; }
  const typing = e.target.closest('input, textarea, [contenteditable]') || palette.el.open;
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); palette.open(); }
  else if (e.key === '1') location.hash = state.current ? memHash(state.current) : '#/';
  else if (e.key === '2') location.hash = '#/karte';
  else if (e.key === '3') location.hash = '#/messwerte';
  else if (e.key === '4') location.hash = '#/vorschlaege';
});

// ── sync ─────────────────────────────────────────────────────────────────
$('#sync-btn').addEventListener('click', async () => {
  const btn = $('#sync-btn');
  btn.setAttribute('aria-busy', 'true');
  $('.sync-label', btn).textContent = 'Synct …';
  try {
    const res = await api('/api/sync', postJSON({}));
    toast(res.message || 'Sync abgeschlossen.', /fehl|error|fail/i.test(res.message || ''), 10000);
    await loadTree();
    stats.invalidate();
    atlas.invalidate();
  } catch (err) {
    toast(`Sync fehlgeschlagen: ${err.message}`, true, 10000);
  } finally {
    btn.removeAttribute('aria-busy');
    $('.sync-label', btn).textContent = 'Sync';
  }
});

// ═════════════════════════════════════════════════════════════════════════
// Sternkarte — force-directed constellations, unlinked memories as field stars
// ═════════════════════════════════════════════════════════════════════════
const atlas = (() => {
  const canvas = $('#atlas');
  const ctx = canvas.getContext('2d');
  let W = 0, H = 0, dpr = 1;
  let nodes = [], edges = [], adj = new Map();
  let cam = { x: 0, y: 0, k: 1 }, tween = null;
  let alpha = 0, active = false, loaded = false, loading = false, raf = 0, introT0 = 0;
  let hover = null, drag = null, pan = null;
  // new connections since the last visit: comet along the edge, flare at the target
  const SEEN_KEY = 'diary.atlas.seenLinks', COMET_MS = 900, FLARE_MS = 700, GLOW_MS = 6000;
  let fresh = [], freshUntil = 0;
  const sprites = {};

  function sprite(type) {
    if (sprites[type]) return sprites[type];
    const hex = TYPE_HEX[type] || TYPE_HEX.category;
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const x = c.getContext('2d');
    const grd = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0, 'rgba(255,247,232,1)');
    grd.addColorStop(.09, 'rgba(255,240,215,.95)');
    grd.addColorStop(.16, `rgba(${r},${g},${b},.9)`);
    grd.addColorStop(.42, `rgba(${r},${g},${b},.22)`);
    grd.addColorStop(1, `rgba(${r},${g},${b},0)`);
    x.fillStyle = grd;
    x.fillRect(0, 0, 64, 64);
    return (sprites[type] = c);
  }

  function hash(str) { let h = 2166136261; for (const ch of str) h = Math.imul(h ^ ch.charCodeAt(0), 16777619); return (h >>> 0) / 4294967296; }

  function resize() {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    dpr = Math.min(devicePixelRatio || 1, 2);
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    paintNow();
    request();
  }
  new ResizeObserver(resize).observe(canvas);

  function simulate(steps) {
    const linked = nodes.filter(n => !n.field && !n.hidden);
    const REP = 160, LEN = 22, SPR = .05, GRAV = .001, PULL = .16;
    for (let s = 0; s < steps && alpha > .004; s++) {
      for (const n of linked) {
        n.fx = -n.x * GRAV + (n.ax - n.x) * PULL;
        n.fy = -n.y * GRAV + (n.ay - n.y) * PULL;
      }
      for (let i = 0; i < linked.length; i++) {
        const a = linked[i];
        for (let j = i + 1; j < linked.length; j++) {
          const b = linked[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 > 90000) continue;
          if (d2 < .01) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = .5; }
          const f = REP / d2;
          a.fx += dx * f; a.fy += dy * f; b.fx -= dx * f; b.fy -= dy * f;
        }
      }
      for (const e of edges) {
        const a = e.a, b = e.b;
        if (a.field || b.field || a.hidden || b.hidden) continue;
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || .01;
        const f = (d - LEN) * SPR / d;
        a.fx += dx * f; a.fy += dy * f; b.fx -= dx * f; b.fy -= dy * f;
      }
      for (const n of linked) {
        if (n.fixed) { n.vx = n.vy = 0; continue; }
        n.vx = (n.vx + n.fx * alpha) * .6;
        n.vy = (n.vy + n.fy * alpha) * .6;
        const sp = Math.hypot(n.vx, n.vy);
        if (sp > 24) { n.vx *= 24 / sp; n.vy *= 24 / sp; }
        n.x += n.vx; n.y += n.vy;
      }
      alpha *= .985;
    }
  }

  // Each project (or top-level branch) gets its own anchor, so constellations form per topic.
  function placeAnchors() {
    const groupOf = n => { const p = n.path.split('/'); return p[1] === 'projects' && p[2] ? 'p:' + p[2] : 'b:' + p[1]; };
    const groups = new Map();
    for (const n of nodes) if (!n.field) { const g = groupOf(n); (groups.get(g) || groups.set(g, []).get(g)).push(n); }
    const order = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
    const GOLDEN = Math.PI * (3 - Math.sqrt(5));
    order.forEach(([, members], i) => {
      const r = 105 * Math.sqrt(i), a = i * GOLDEN;
      for (const n of members) {
        n.ax = Math.cos(a) * r; n.ay = Math.sin(a) * r;
        n.x = n.ax + (hash(n.id + 'j') - .5) * 30; n.y = n.ay + (hash(n.id + 'k') - .5) * 30;
      }
    });
    for (const n of nodes) if (n.field) { n.ax = n.x; n.ay = n.y; }
  }

  function placeField() {
    const linked = nodes.filter(n => !n.field && !n.hidden);
    const radii = linked.map(n => Math.hypot(n.x, n.y)).sort((a, b) => a - b);
    const Rl = radii.length ? radii[Math.floor(radii.length * .95)] || 60 : 0;
    const field = nodes.filter(n => n.field);
    const inner = Rl ? Rl * 1.18 + 30 : 0;
    const band = Math.max(Rl * .9, Math.sqrt(field.length) * 16);
    for (const n of field) {
      const a = hash(n.id) * Math.PI * 2, t = hash(n.id + 'r');
      const r = inner + Math.sqrt(t) * band;
      n.x = Math.cos(a) * r; n.y = Math.sin(a) * r;
    }
  }

  function fitCam(subset) {
    const vis = (subset || nodes).filter(n => !n.hidden);
    if (!vis.length || !W) return { x: W / 2, y: H / 2, k: 1 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of vis) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const k = Math.min(3, Math.min(W / (x1 - x0 + 160), (H - 140) / (y1 - y0 + 160)));
    return { k, x: W / 2 - ((x0 + x1) / 2) * k, y: H / 2 + 30 - ((y0 + y1) / 2) * k };
  }

  function tweenTo(target, dur, done) {
    if (REDUCED.matches) { cam = { ...target }; request(); done?.(); return; }
    tween = { from: { ...cam }, to: target, t0: performance.now(), dur, done };
    request();
  }

  async function load() {
    if (loading) return;
    loading = true;
    const extracted = $('#atlas-extracted').checked;
    let data;
    try { data = await api('/api/graph?include_extracted=' + extracted); }
    catch (err) { loading = false; toast(`Sternkarte konnte nicht geladen werden: ${err.message}`, true); return; }
    loading = false;
    const idx = new Map();
    nodes = data.nodes.map(n => {
      const field = !n.degree;
      const a = hash(n.id) * Math.PI * 2, r = 20 + hash(n.id + 'x') * 160;
      const o = { ...n, field, x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0, fx: 0, fy: 0,
        phase: hash(n.id + 'p') * Math.PI * 2, imp: n.importance ?? .5 };
      o.r = field ? 1 + o.imp * 1.1 : 1.8 + o.imp * 2.2 + Math.sqrt(n.degree) * .75;
      idx.set(n.id, o);
      return o;
    });
    edges = data.edges.map(e => ({ a: idx.get(e.from), b: idx.get(e.to), rel: e.rel_type, origin: e.origin, conf: e.confidence,
      created: e.created_at ? Date.parse(e.created_at) : 0 })).filter(e => e.a && e.b);
    adj = new Map(nodes.map(n => [n, new Set()]));
    for (const e of edges) { adj.get(e.a).add(e.b); adj.get(e.b).add(e.a); }
    placeAnchors();
    applyFieldToggle();
    alpha = 1;
    simulate(420);
    placeField();
    const maxR = Math.max(1, ...nodes.map(n => Math.hypot(n.x, n.y)));
    for (const n of nodes) n.delay = (Math.hypot(n.x, n.y) / maxR) * 1100 + (n.field ? 250 : 0);
    loaded = true;
    $('#atlas-empty').hidden = edges.length > 0;
    findFresh();
    renderLegend();
    const fit = fitCam();
    if (document.hidden || REDUCED.matches) {
      cam = fit;
      introT0 = -1e9;
      scheduleFresh(performance.now());
      paintNow();
      return;
    }
    cam = { x: fit.x, y: fit.y, k: fit.k * .72 };
    introT0 = performance.now();
    tweenTo(fit, 1600);
    scheduleFresh(introT0 + 1500);
  }

  // Server timestamps only (no client clock skew). First visit ever: nothing is "new".
  function findFresh() {
    const newest = edges.reduce((m, e) => Math.max(m, e.created), 0);
    const seen = localStorage.getItem(SEEN_KEY);
    fresh = seen === null ? [] : edges.filter(e => e.created > Number(seen)).sort((x, y) => x.created - y.created);
    if (newest) localStorage.setItem(SEEN_KEY, String(Math.max(newest, Number(seen) || 0)));
    const chip = $('#atlas-fresh');
    chip.hidden = !fresh.length;
    if (fresh.length) $('b', chip).textContent = fmt(fresh.length);
  }

  function scheduleFresh(t0) {
    if (!fresh.length) return;
    const animated = Math.min(fresh.length, 120);
    const stagger = Math.min(140, 4200 / animated);
    fresh.forEach((e, i) => { e.anim = t0 + Math.min(i, animated - 1) * stagger; });
    freshUntil = t0 + animated * stagger + COMET_MS + FLARE_MS + GLOW_MS;
    request();
  }

  let comet = null;
  function cometSprite() {
    if (comet) return comet;
    comet = document.createElement('canvas');
    comet.width = comet.height = 32;
    const g = comet.getContext('2d'), grd = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    grd.addColorStop(0, 'rgba(255,250,235,1)');
    grd.addColorStop(.2, 'rgba(255,214,150,.95)');
    grd.addColorStop(.5, 'rgba(232,168,76,.35)');
    grd.addColorStop(1, 'rgba(232,168,76,0)');
    g.fillStyle = grd;
    g.fillRect(0, 0, 32, 32);
    return comet;
  }

  function drawFresh(t) {
    for (const e of fresh) {
      if (e.a.hidden || e.b.hidden || e.anim == null) continue;
      const [ax, ay] = toScreen(e.a), [bx, by] = toScreen(e.b);
      if (REDUCED.matches) {
        ctx.strokeStyle = 'rgba(232,168,76,.75)'; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        continue;
      }
      const since = t - e.anim;
      if (since < 0) continue;
      const p = Math.min(1, since / COMET_MS), q = easeOut(p);
      const hx = ax + (bx - ax) * q, hy = ay + (by - ay) * q;
      if (p < 1) {
        const grd = ctx.createLinearGradient(ax, ay, hx, hy);
        grd.addColorStop(0, 'rgba(232,168,76,0)');
        grd.addColorStop(1, 'rgba(255,214,150,.95)');
        ctx.strokeStyle = grd; ctx.lineWidth = 1.8;
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(hx, hy); ctx.stroke();
        ctx.drawImage(cometSprite(), hx - 10, hy - 10, 20, 20);
        continue;
      }
      const glow = Math.max(0, 1 - (since - COMET_MS - FLARE_MS) / GLOW_MS);
      if (glow > 0) {
        ctx.strokeStyle = `rgba(232,168,76,${.12 + .6 * Math.min(1, glow)})`; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      }
      const f = (since - COMET_MS) / FLARE_MS;
      if (f < 1) {
        ctx.strokeStyle = `rgba(255,214,150,${.7 * (1 - f)})`; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(bx, by, 4 + 14 * easeOut(f), 0, Math.PI * 2); ctx.stroke();
      }
    }
    ctx.lineWidth = 1;
  }

  // rAF never fires in hidden tabs; paint synchronously so the map is never blank.
  function paintNow() { if (active && W) draw(performance.now()); }

  function applyFieldToggle() {
    const show = $('#atlas-field').checked;
    for (const n of nodes) n.hidden = n.field && !show;
  }

  function renderLegend() {
    const vis = nodes.filter(n => !n.hidden);
    const counts = countTypes(vis);
    const inferred = edges.some(e => e.origin === 'inferred'), bad = edges.some(e => e.rel === 'contradicts');
    $('#atlas-legend').innerHTML = TYPES.filter(t => counts[t]).map(t =>
      `<span><span class="sw dot-${t}"></span>${TYPE_LABEL[t]} <span class="n">${fmt(counts[t])}</span></span>`).join('')
      + `<span><span class="edge"></span>explizit</span>`
      + (inferred ? `<span><span class="edge dashed"></span>abgeleitet</span>` : '')
      + (bad ? `<span><span class="edge bad"></span>▲ Widerspruch</span>` : '')
      + (fresh.length ? `<span><span class="edge fresh"></span>neu</span>` : '');
    $('#atlas-caption').textContent = `${fmt(nodes.filter(n => !n.field).length)} Sterne in Sternbildern, ${fmt(edges.length)} Verbindungen`
      + ($('#atlas-field').checked ? `, ${fmt(nodes.filter(n => n.field).length)} Feldsterne ohne Verbindung.` : '.');
  }

  const toScreen = n => [n.x * cam.k + cam.x, n.y * cam.k + cam.y];

  function draw(t) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!loaded) return;
    const intro = REDUCED.matches ? 1e9 : t - introT0;
    const appear = n => Math.max(0, Math.min(1, (intro - n.delay) / 700));
    const near = hover ? adj.get(hover) : null;
    const scale = Math.min(2.4, Math.max(.55, Math.sqrt(cam.k)));

    ctx.lineWidth = 1;
    for (const e of edges) {
      if (e.a.hidden || e.b.hidden) continue;
      if (e.anim != null && !REDUCED.matches && t - e.anim < COMET_MS) continue;
      const ap = Math.min(appear(e.a), appear(e.b));
      if (!ap) continue;
      const hot = hover && (e.a === hover || e.b === hover);
      const base = e.rel === 'contradicts' ? '224,108,90' : hot ? '232,168,76' : '201,191,173';
      // automatic edges fade with their confidence, deliberate ones stay at full strength
      const weight = e.origin === 'inferred' && e.conf != null ? .35 + .65 * e.conf : 1;
      const a = (e.rel === 'contradicts' ? .6 : hot ? .85 : hover ? .035 : .14) * (hot ? 1 : weight);
      ctx.strokeStyle = `rgba(${base},${a * ap})`;
      ctx.setLineDash(e.origin === 'inferred' ? [3, 4] : []);
      const [ax, ay] = toScreen(e.a), [bx, by] = toScreen(e.b);
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    }
    ctx.setLineDash([]);

    for (const n of nodes) {
      if (n.hidden) continue;
      const ap = appear(n);
      if (!ap) continue;
      const [sx, sy] = toScreen(n);
      if (sx < -40 || sy < -40 || sx > W + 40 || sy > H + 40) continue;
      let a = n.field ? .28 + n.imp * .4 : .45 + n.imp * .55;
      if (!REDUCED.matches) a *= .8 + .2 * Math.sin(t * .0012 + n.phase);
      if (hover && n !== hover && !near.has(n)) a *= .16;
      let r = n.r * scale * (.3 + .7 * easeOut(ap));
      if (n === hover) r *= 1.6;
      ctx.globalAlpha = Math.min(1, a) * ap;
      ctx.drawImage(sprite(n.type), sx - r * 4, sy - r * 4, r * 8, r * 8);
    }
    ctx.globalAlpha = 1;
    drawFresh(t);

    const labels = [];
    if (hover) { labels.push(hover, ...near); }
    else if (cam.k > 1.5) for (const n of nodes) if (!n.hidden && !n.field && n.degree >= 4) labels.push(n);
    ctx.font = '500 11px "JetBrains Mono", monospace';
    ctx.textBaseline = 'middle';
    for (const n of labels) {
      const [sx, sy] = toScreen(n);
      const txt = n.path.split('/').pop();
      const x = sx + n.r * scale * 1.6 + 6;
      ctx.fillStyle = 'rgba(13,11,9,.75)';
      const w = ctx.measureText(txt).width;
      ctx.fillRect(x - 3, sy - 8, w + 6, 16);
      ctx.fillStyle = n === hover ? '#e8a84c' : 'rgba(236,228,212,.88)';
      ctx.fillText(txt, x, sy);
    }
  }

  function frame(t) {
    raf = 0;
    if (!active || document.hidden) return;
    if (tween) {
      const p = Math.min(1, (t - tween.t0) / tween.dur), e = easeInOut(p);
      cam = { x: tween.from.x + (tween.to.x - tween.from.x) * e, y: tween.from.y + (tween.to.y - tween.from.y) * e, k: tween.from.k + (tween.to.k - tween.from.k) * e };
      if (p >= 1) { const done = tween.done; tween = null; done?.(); }
    }
    if (alpha > .004) simulate(1);
    draw(t);
    const introRunning = !REDUCED.matches && t - introT0 < 3000;
    if (!REDUCED.matches || tween || alpha > .004 || introRunning || t < freshUntil) raf = requestAnimationFrame(frame);
  }
  function request() { if (!raf && active) raf = requestAnimationFrame(frame); }
  document.addEventListener('visibilitychange', request);

  function nodeAt(px, py) {
    let best = null, bd = Infinity;
    const scale = Math.min(2.4, Math.max(.55, Math.sqrt(cam.k)));
    for (const n of nodes) {
      if (n.hidden) continue;
      const [sx, sy] = toScreen(n);
      const d = Math.hypot(sx - px, sy - py);
      if (d < Math.max(9, n.r * scale * 2.4) && d < bd) { bd = d; best = n; }
    }
    return best;
  }

  function setHover(n) {
    if (n === hover) return;
    hover = n;
    canvas.classList.toggle('pointing', !!n);
    const card = $('#atlas-card');
    if (!n) { card.hidden = true; paintNow(); request(); return; }
    card.hidden = false;
    card.innerHTML = `<div class="c-type"><span class="sw dot-${esc(n.type)}"></span>${esc(TYPE_LABEL[n.type] || n.type)}</div>
      <h2>${esc(n.title || n.path)}</h2><div class="c-path">${esc(n.path)}</div>
      <div class="c-meta"><span>${fmt(n.degree)} Verbindungen</span><span>Wichtigkeit ${Math.round(n.imp * 100)} %</span></div>`;
    card.style.animation = 'none'; void card.offsetWidth; card.style.animation = '';
    paintNow();
    request();
  }

  function openNode(n) {
    const k = Math.max(cam.k * 1.8, 2.6);
    tweenTo({ k, x: W / 2 - n.x * k, y: H / 2 - n.y * k }, 650, () => go(n.path));
  }

  const pos = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener('pointerdown', e => {
    if (!loaded) return;
    canvas.setPointerCapture(e.pointerId);
    const [px, py] = pos(e);
    const n = nodeAt(px, py);
    tween = null;
    if (n) drag = { n, px, py, moved: false };
    else { pan = { px, py, cx: cam.x, cy: cam.y }; canvas.classList.add('dragging'); }
  });
  canvas.addEventListener('pointermove', e => {
    if (!loaded) return;
    const [px, py] = pos(e);
    if (drag) {
      if (!drag.moved && Math.hypot(px - drag.px, py - drag.py) < 4) return;
      drag.moved = true;
      drag.n.fixed = true;
      drag.n.x = (px - cam.x) / cam.k; drag.n.y = (py - cam.y) / cam.k;
      if (!drag.n.field) alpha = Math.max(alpha, .22);
      request();
    } else if (pan) {
      cam.x = pan.cx + px - pan.px; cam.y = pan.cy + py - pan.py;
      request();
    } else setHover(nodeAt(px, py));
  });
  const end = () => {
    if (drag) { drag.n.fixed = false; if (!drag.moved) openNode(drag.n); }
    drag = null; pan = null;
    canvas.classList.remove('dragging');
  };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  canvas.addEventListener('pointerleave', () => { if (!drag && !pan) setHover(null); });
  canvas.addEventListener('wheel', e => {
    if (!loaded) return;
    e.preventDefault();
    tween = null;
    const [px, py] = pos(e);
    const k = Math.max(.12, Math.min(8, cam.k * Math.exp(-e.deltaY * .0016)));
    cam.x = px - (px - cam.x) * (k / cam.k);
    cam.y = py - (py - cam.y) * (k / cam.k);
    cam.k = k;
    paintNow();
    request();
  }, { passive: false });

  $('#atlas-reset').addEventListener('click', () => tweenTo(fitCam(), 900));
  $('#atlas-fresh').addEventListener('click', () => {
    if (!fresh.length) return;
    if (fresh.length <= 12) tweenTo(fitCam([...new Set(fresh.flatMap(e => [e.a, e.b]))]), 900);
    scheduleFresh(performance.now() + (fresh.length <= 12 ? 700 : 100));
  });
  $('#atlas-extracted').addEventListener('change', () => { loaded = false; load(); });
  $('#atlas-field').addEventListener('change', () => {
    applyFieldToggle();
    if (hover?.hidden) setHover(null);
    renderLegend();
    tweenTo(fitCam(), 900);
  });

  return {
    setActive(on) {
      active = on;
      if (!on) { setHover(null); return; }
      resize();
      if (!loaded) load(); else request();
    },
    invalidate() { loaded = false; if (active) load(); },
  };
})();

// ═════════════════════════════════════════════════════════════════════════
// Messwerte
// ═════════════════════════════════════════════════════════════════════════
const stats = (() => {
  const body = $('#stats-body');
  let days = Number(localStorage.getItem('diary.days')) || 30;
  let data = null, activity = null, health = null, loadedFor = null, seq = 0;
  let observer = null;

  $$('.range button').forEach(b => {
    b.setAttribute('aria-checked', String(Number(b.dataset.days) === days));
    b.addEventListener('click', () => {
      days = Number(b.dataset.days);
      localStorage.setItem('diary.days', days);
      $$('.range button').forEach(x => x.setAttribute('aria-checked', String(x === b)));
      load();
    });
  });

  async function load() {
    const my = ++seq;
    body.setAttribute('aria-busy', 'true');
    if (!data) body.innerHTML = '<p class="loading">Messung läuft …</p>';
    try {
      const [st, act, h] = await Promise.all([
        api(`/api/stats?days=${days}`),
        activity ? Promise.resolve(activity) : api('/api/activity?days=371'),
        health ? Promise.resolve(health) : api('/api/health'),
      ]);
      if (my !== seq) return;
      data = st; activity = act; health = h; loadedFor = days;
      render();
    } catch (err) {
      if (my !== seq) return;
      body.innerHTML = `<p class="loading">Messung fehlgeschlagen: ${esc(err.message)}</p>`;
    } finally {
      if (my === seq) body.setAttribute('aria-busy', 'false');
    }
  }

  const block = (id, title, caption, inner, aside = '') => `
    <section class="block" id="${id}">
      <div class="block-head"><h2>${title}</h2>${aside}${caption ? `<p>${caption}</p>` : ''}</div>
      ${inner}
    </section>`;
  const num = (v, f = 'int') => `<span data-to="${Number(v) || 0}" data-fmt="${f}">0</span>`;
  const counter = (v, label, cls = '', f = 'int') => `<div class="counter ${cls}"><span class="v">${typeof v === 'number' ? num(v, f) : esc(v)}</span><span class="l">${esc(label)}</span></div>`;
  const ring = (v, label, sub, muted = false) => {
    const f = Math.max(0, Math.min(1, v || 0));
    return `<div class="ring"><div class="ring-v"><svg viewBox="0 0 112 112" aria-hidden="true"><circle class="track" cx="56" cy="56" r="46"/><circle class="val${muted ? ' muted' : ''}" cx="56" cy="56" r="46" data-v="${f}"/></svg>
      <span>${num(f * 100, 'pct')}</span></div><div class="ring-l">${esc(label)}</div>${sub ? `<div class="ring-s">${esc(sub)}</div>` : ''}</div>`;
  };
  const bars = (rows, opts = {}) => {
    const max = Math.max(1, ...rows.map(r => r.v));
    return `<div class="bars">${rows.map((r, i) => `<div class="bar-row" data-tip="${esc(r.tip || r.k)}" data-tip-sub="${esc(fmt(r.v) + (opts.unit || ''))}">
      ${r.path ? `<button type="button" class="k link" data-go="${esc(r.path)}">${esc(r.k)}</button>` : `<span class="k">${esc(r.k)}</span>`}
      <span class="bar-track"><i style="--v:${r.v / max};--i:${i};${r.c ? `--c:${r.c}` : ''}"></i></span>
      <span class="n">${fmt(r.v)}${esc(opts.unit || '')}</span></div>`).join('')}</div>`;
  };
  const rank = (rows, unit) => rows.length
    ? `<ol class="rank">${rows.map(([p, n, label]) => `<li><button type="button" data-go="${esc(p)}" title="${esc(p)}"><span class="p">${label ? esc(label) : shortPath(p)}</span><span class="n">${esc(unit(n))}</span></button></li>`).join('')}</ol>`
    : '<p class="note">Noch keine Daten.</p>';
  const shortPath = p => {
    const parts = p.split('/').filter(Boolean);
    if (parts.length < 2) return esc(p);
    return `<small>${esc(parts[parts.length - 2])}/</small>${esc(parts[parts.length - 1].replace(/^reference_/, ''))}`;
  };
  const progress = (label, done, total) => `<div class="progress"><div class="progress-top"><span>${esc(label)}</span><b>${fmt(done)} / ${fmt(total)}</b></div>
    <div class="progress-track"><i style="--v:${total ? done / total : 0}"></i></div></div>`;

  function heatmap(series) {
    const first = parseTs(series[0].date + 'T00:00:00');
    const off = (first.getDay() + 6) % 7;
    const weeks = Math.ceil((off + series.length) / 7);
    const vals = series.map(d => d.created + d.updated).filter(v => v > 0).sort((a, b) => a - b);
    const q = p => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] || 0;
    const [t1, t2, t3] = [q(.3), q(.6), q(.85)];
    const lvl = v => (!v ? 0 : v <= t1 ? 1 : v <= t2 ? 2 : v <= t3 ? 3 : 4);
    const cells = series.map((d, i) => {
      const idx = off + i, w = Math.floor(idx / 7), wd = idx % 7;
      const date = parseTs(d.date + 'T00:00:00');
      const parts = [`${d.created} neu`, `${d.updated} geändert`];
      if (d.logs) parts.push(`${d.logs} Log`);
      const label = `${dateLong(date)}: ${parts.join(', ')}`;
      return `<i class="c l${lvl(d.created + d.updated)}" style="--col:${w + 2};--row:${wd + 2};--d:${w + wd}" data-tip="${esc(dateLong(date))}" data-tip-sub="${esc(parts.join(' · '))}" role="img" aria-label="${esc(label)}"></i>`;
    });
    const months = [];
    const label = (w, d) => `<span class="m" style="--col:${w + 2}">${d.toLocaleDateString('de-DE', { month: 'short' })}</span>`;
    let lastM = -1, lastW = -9;
    for (let w = 0; w < weeks - 2; w++) {
      const d = new Date(first); d.setDate(d.getDate() - off + w * 7 + 6);
      if (d.getMonth() === lastM) continue;
      lastM = d.getMonth();
      if (w - lastW < 3) months.pop();  // a month that barely started loses its label to the next one
      months.push(label(w, d));
      lastW = w;
    }
    const wds = [['Mo', 2], ['Mi', 4], ['Fr', 6]].map(([l, r]) => `<span class="wd" style="--row:${r}">${l}</span>`).join('');
    return `<div class="heat-wrap"><div class="heat" style="--weeks:${weeks}">${months.join('')}${wds}${cells.join('')}</div></div>`;
  }

  function streaks(series) {
    let best = 0, cur = 0, run = 0, top = null;
    for (const d of series) {
      const v = d.created + d.updated;
      run = v ? run + 1 : 0;
      best = Math.max(best, run);
      if (!top || v > top.created + top.updated) top = d;
    }
    for (let i = series.length - 1; i >= 0 && series[i].created + series[i].updated; i--) cur++;
    return { best, cur, top };
  }

  function drawTrend(host, series) {
    const W = Math.max(320, host.clientWidth), H = 220, P = { l: 36, r: 78, t: 14, b: 28 };
    const n = series.length;
    const raw = Math.max(1, ...series.map(d => Math.max(d.created, d.updated)));
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const max = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw);
    const x = i => P.l + (n === 1 ? 0 : (i * (W - P.l - P.r)) / (n - 1));
    const y = v => H - P.b - (v * (H - P.t - P.b)) / max;
    const line = key => series.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join('');
    const area = line('created') + `L${x(n - 1).toFixed(1)},${y(0)}L${x(0).toFixed(1)},${y(0)}Z`;
    const ticksY = [0, max / 2, max].map(v => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}"/>`).join('');
    const labelsY = [0, max / 2, max].map(v => `<text x="${P.l - 8}" y="${y(v) + 3.5}" text-anchor="end">${fmt(v)}</text>`).join('');
    const tickIdx = [...new Set([0, .25, .5, .75, 1].map(f => Math.round(f * (n - 1))))];
    const labelsX = tickIdx.map(i => `<text x="${x(i)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}">${dateShort(parseTs(series[i].date + 'T00:00:00'))}</text>`).join('');
    const last = series[n - 1];
    let yc = y(last.created), yu = y(last.updated);
    if (Math.abs(yc - yu) < 13) { if (yc <= yu) { yc -= 7; yu += 7; } else { yc += 7; yu -= 7; } }
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Neue und geänderte Erinnerungen pro Tag">
      <defs><linearGradient id="trend-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#e8a84c" stop-opacity=".28"/><stop offset="1" stop-color="#e8a84c" stop-opacity="0"/></linearGradient></defs>
      <g class="grid">${ticksY}</g><g class="axis">${labelsY}${labelsX}</g>
      <path class="area" d="${area}"/>
      <path class="line updated" d="${line('updated')}"/>
      <path class="line created draw" d="${line('created')}"/>
      <text class="dl" x="${W - P.r + 8}" y="${yc + 4}">neu</text>
      <text class="dl" x="${W - P.r + 8}" y="${yu + 4}">geändert</text>
      <g class="hover" visibility="hidden"><line class="cross" y1="${P.t}" y2="${H - P.b}"/>
        <circle class="dot du" r="4" fill="#c9bfad"/><circle class="dot dc" r="4" fill="#e8a84c"/></g>
      <rect x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent" class="hit"/>
    </svg>`;
    const drawn = host.querySelector('.draw');
    drawn.style.setProperty('--len', Math.ceil(drawn.getTotalLength()));
    const svg = host.querySelector('svg'), g = svg.querySelector('.hover');
    const hit = svg.querySelector('.hit');
    hit.addEventListener('pointermove', e => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - P.l) / (W - P.l - P.r)) * (n - 1))));
      const d = series[i];
      g.setAttribute('visibility', 'visible');
      g.querySelector('.cross').setAttribute('x1', x(i)); g.querySelector('.cross').setAttribute('x2', x(i));
      g.querySelector('.dc').setAttribute('cx', x(i)); g.querySelector('.dc').setAttribute('cy', y(d.created));
      g.querySelector('.du').setAttribute('cx', x(i)); g.querySelector('.du').setAttribute('cy', y(d.updated));
      tip.dataset.owner = 'trend';
      showTip(e.clientX, e.clientY, dateLong(parseTs(d.date + 'T00:00:00')), `${d.created} neu · ${d.updated} geändert${d.logs ? ` · ${d.logs} Log` : ''}`);
    });
    hit.addEventListener('pointerleave', () => { g.setAttribute('visibility', 'hidden'); delete tip.dataset.owner; hideTip(); });
  }

  function render() {
    const st = data, c = st.corpus || {}, q = st.quality || {}, g = st.graph || {}, j = st.journal || {},
      inj = st.injection || {}, inst = st.instance || {}, dia = st.diary || {}, van = st.vanilla || {};
    const curated = c.curated || 0;
    const series = activity.series;
    const trendSeries = series.slice(-Math.max(14, days));
    const s = streaks(series);
    const startTok = dia.session_start_tokens ?? dia.project_digest_tokens ?? 0;

    const hero = `<div class="hero-nums">
        <div class="hero-num"><span class="v">${num(curated)}</span><span class="l">Erinnerungen</span><span class="s">+${fmt(q.created_30d)} in 30 Tagen</span></div>
        <div class="hero-num"><span class="v">${num(g.links ?? c.links)}</span><span class="l">Verknüpfungen</span><span class="s">${fmt(g.by_origin?.inferred || 0)} davon abgeleitet</span></div>
        <div class="hero-num"><span class="v">${num(c.approx_tokens, 'compact')}</span><span class="l">Tokens erreichbar</span><span class="s">Index auf Abruf: ${compact(dia.index_tokens || 0)}</span></div>
        <div class="hero-num"><span class="v">${num(c.projects)}</span><span class="l">Projekt-Zweige</span><span class="s">${fmt(j.projects_active)} mit Journal</span></div>
      </div>
      <p class="hero-line"><b>${fmt(q.created_7d)}</b> neue und <b>${fmt(q.updated_7d)}</b> geänderte Erinnerungen in den letzten 7 Tagen.
        <b>${pct(c.embedded, curated)}</b> sind semantisch eingebettet, ${fmt(c.extracted)} warten als Extrakt auf Beförderung.</p>`;

    const act = block('b-activity', 'Aktivität', 'Jede Zelle ein Tag im letzten Jahr, Helligkeit nach neuen plus geänderten Erinnerungen. „Geändert“ zählt nur die letzte Bearbeitung.',
      `<div class="panel">${heatmap(series)}
        <div class="heat-foot"><span class="heat-totals"><b>${fmt(activity.totals.created)}</b> neu · <b>${fmt(activity.totals.updated)}</b> zuletzt geändert · <b>${fmt(activity.totals.logs)}</b> Log-Einträge ·
          längste Serie <b>${s.best}</b> Tage${s.cur ? ` · aktuell <b>${s.cur}</b>` : ''}${s.top && s.top.created + s.top.updated ? ` · stärkster Tag <b>${esc(dateShort(parseTs(s.top.date + 'T00:00:00')))}</b>` : ''}</span>
          <span class="heat-legend">weniger<i class="l0"></i><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i>mehr</span></div>
      </div>
      <div class="panel stack"><h3>Verlauf <span>letzte ${trendSeries.length} Tage</span></h3>
        <div class="legend"><span><i></i>neu</span><span><i class="dash"></i>geändert</span></div>
        <div class="trend" id="trend"></div>
        <details class="table-view"><summary>Als Tabelle</summary><table><thead><tr><th>Tag</th><th>neu</th><th>geändert</th><th>Logs</th></tr></thead><tbody>
          ${trendSeries.slice().reverse().map(d => `<tr><td>${esc(d.date)}</td><td>${d.created}</td><td>${d.updated}</td><td>${d.logs}</td></tr>`).join('')}</tbody></table></details>
      </div>`);

    const typeCounts = q.types || {};
    const typeTotal = Object.values(typeCounts).reduce((a, b) => a + b, 0) || 1;
    const branches = Object.entries(c.branches || {}).sort((a, b) => b[1] - a[1]);
    const imp = q.importance || {};
    const dist = block('b-dist', 'Verteilung', 'Wo das Wissen liegt und wie es klassifiziert ist.',
      `<div class="grid-2">
        <div class="panel"><h3>Zweige <span>${branches.length}</span></h3>${bars(branches.map(([k, v]) => ({ k, v, path: state.byPath.has(k) ? k : null, c: 'var(--ink-2)' })))}</div>
        <div class="panel"><h3>Spektrum <span>nach Typ</span></h3>
          <div class="spectrum">${TYPES.filter(t => typeCounts[t]).map((t, i) => `<i class="t-${t}" style="--v:${typeCounts[t]};--i:${i}" data-tip="${TYPE_LABEL[t]}" data-tip-sub="${fmt(typeCounts[t])} · ${pct(typeCounts[t], typeTotal)}"></i>`).join('')}</div>
          <div class="spec-legend">${TYPES.filter(t => typeCounts[t]).map(t => `<div><span class="sw dot-${t}"></span>${TYPE_LABEL[t]}<b>${fmt(typeCounts[t])}</b></div>`).join('')}</div>
          <h3 class="sub">Wichtigkeit</h3>
          ${bars([{ k: 'hoch', v: imp.high || 0, c: '#f0b45a' }, { k: 'mittel', v: imp.mid || 0, c: '#b27a2c' }, { k: 'niedrig', v: imp.low || 0, c: '#7a5520' }])}
        </div>
      </div>`);

    const issues = health.issues || [];
    const kindLabel = { expired: 'abgelaufen', empty_category: 'leere Kategorie', empty_node: 'ohne Inhalt' };
    const quality = block('b-quality', 'Qualität', 'Wie gut das Gedächtnis gepflegt ist. Das Soft-Limit liegt bei 1200 Zeichen pro Erinnerung.',
      `<div class="panel"><div class="rings">
          ${ring(curated ? c.embedded / curated : 0, 'eingebettet', `${fmt(c.embedded)} von ${fmt(curated)}`)}
          ${ring(curated ? 1 - q.never_accessed / curated : 0, 'schon einmal gelesen', `${fmt(q.never_accessed)} nie geöffnet`)}
          ${ring(curated ? 1 - q.oversized / curated : 0, 'im Soft-Limit', `${fmt(q.oversized)} zu lang`, true)}
          ${ring(g.links && curated ? 1 - g.orphans / curated : 0, 'verknüpft', `${fmt(g.orphans)} ohne Link`, true)}
        </div></div>
      <div class="counters stack">
        ${counter(q.expired || 0, 'abgelaufen', q.expired ? 'alert' : 'ok')}
        ${counter(q.no_embedding || 0, 'ohne Embedding', q.no_embedding ? 'alert' : 'ok')}
        ${counter(q.stale_180d || 0, 'seit 180 T unberührt')}
        ${counter(q.avg_tokens || 0, 'Ø Tokens je Erinnerung')}
        ${counter(q.tombstones || 0, 'Tombstones')}
        ${counter(q.extracted_expiring_14d || 0, 'Extrakte laufen in 14 T ab')}
      </div>
      <div class="panel stack"><h3>Health <span>${issues.length ? `${issues.length} Befunde` : 'sauber'}</span></h3>
        ${issues.length ? `<ul class="issues">${issues.slice(0, 14).map(i => `<li><button type="button" data-go="${esc(i.path)}"><span class="kind">${esc(kindLabel[i.kind] || i.kind)}</span><span class="p">${esc(i.path)}</span><span class="d">${esc(i.detail)}</span></button></li>`).join('')}</ul>
          ${issues.length > 14 ? `<p class="note">… und ${issues.length - 14} weitere.</p>` : ''}` : '<p class="all-clear">Keine Befunde. Nichts abgelaufen, keine leeren Knoten.</p>'}
      </div>`);

    const relRows = Object.entries(g.by_type || {}).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => ({ k: (k === 'contradicts' ? '▲ ' : '') + (REL_LABEL[k] || k), v, c: k === 'contradicts' ? 'var(--bad)' : 'var(--ink-2)' }));
    const graph = block('b-graph', 'Sternbilder', 'Die Verbindungen zwischen Erinnerungen, nach Beziehungsart.',
      `<div class="grid-2">
        <div class="panel"><h3>Beziehungen <span>${fmt(g.links)}</span></h3>${relRows.length ? bars(relRows) : '<p class="note">Noch keine Verknüpfungen.</p>'}</div>
        <div class="panel"><h3>Herkunft</h3><div class="rings">
          ${ring(g.links ? (g.by_origin?.explicit || 0) / g.links : 0, 'explizit gesetzt', `${fmt(g.by_origin?.explicit || 0)} Links`)}
          ${ring(g.links ? (g.by_origin?.inferred || 0) / g.links : 0, 'automatisch abgeleitet', `${fmt(g.by_origin?.inferred || 0)} Links`, true)}
        </div>
        <div class="counters stack">${counter(g.contradictions || 0, 'Widersprüche', g.contradictions ? 'alert' : 'ok')}${counter(g.orphans || 0, 'Feldsterne')}</div></div>
      </div>
      <div class="grid-2 stack">
        <div class="panel"><h3>Automatisch verknüpft <span>${g.auto_avg_confidence != null ? `Ø Konfidenz ${String(g.auto_avg_confidence.toFixed(2)).replace('.', ',')}` : ''}</span></h3>
          <div class="counters">${counter(g.auto_links || 0, 'Auto-Links')}${counter(g.suggestions_approved || 0, 'Vorschläge freigegeben')}${counter(g.suggestions_rejected || 0, 'abgelehnt')}</div>
          <p class="note">Ab Konfidenz 0,7 verknüpft diary-mcp selbst: bei Textverweisen, nahezu gleichem Inhalt oder wenn das Modell sicher ist.</p></div>
        <div class="panel"><h3>Prüfliste</h3>
          <div class="counters">${counter(g.suggestions_pending || 0, 'Vorschläge offen')}</div>
          <p class="note">Mittlere Konfidenz (0,35–0,7). Du entscheidest unter <a href="#/vorschlaege">Vorschläge</a>; Claude fasst die Liste nur an, wenn du ausdrücklich darum bittest.</p></div>
      </div>`, '<a class="aside" href="#/karte">Zur Sternkarte →</a>');

    const maxTok = Math.max(1, dia.reachable_tokens || 0, van.approx_tokens || 0);
    const inject = block('b-inject', 'Injection', `Was die Hooks in den letzten ${inj.days ?? days} Tagen automatisch in Claudes Kontext gelegt haben.`,
      `<div class="grid-2">
        <div class="panel"><h3>Treffsicherheit</h3><div class="rings">
          ${ring(inj.hit_rate || 0, 'Prompts mit Treffer', `${fmt(inj.prompts_with_hits)} von ${fmt(inj.prompts)}`)}
          ${ring(inj.semantic_share || 0, 'semantisch gesucht', 'Rest: Volltext', true)}
        </div></div>
        <div class="counters">
          ${counter(inj.sessions || 0, 'Sessions')}
          ${counter(inj.prompts || 0, 'Prompts geprüft')}
          ${counter(inj.avg_tokens_per_session || 0, 'Ø Tokens je Session')}
          ${counter(inj.total_injected_tokens || 0, 'Tokens injiziert')}
          ${counter(`${fmt(inj.avg_latency_ms?.['session-start'] || 0)} ms`, 'Latenz Session-Start')}
          ${counter(`${fmt(inj.avg_latency_ms?.prompt || 0)} ms`, 'Latenz je Prompt')}
        </div>
      </div>
      <div class="grid-2 stack">
        <div class="panel"><h3>Kontext-Ökonomie <span>Tokens</span></h3><div class="compare">${bars([
          { k: 'beim Start geladen', v: startTok, c: 'var(--accent)' },
          { k: 'Index auf Abruf', v: dia.index_tokens || 0, c: '#b27a2c' },
          { k: 'erreichbar gesamt', v: dia.reachable_tokens || c.approx_tokens || 0, c: 'var(--ink-2)' },
          { k: 'Datei-Memory', v: van.approx_tokens || 0, c: 'var(--faint)', tip: 'Claude Codes datei-basiertes Memory zum Vergleich' },
        ].map(r => ({ ...r, v: Math.min(r.v, maxTok) })))}</div>
        <p class="note">${startTok ? `Beim Start landen ${pct(startTok, dia.reachable_tokens || c.approx_tokens)} des Gedächtnisses im Kontext, der Rest wird bei Bedarf geholt.` : 'Ohne Projektkontext lädt der Session-Start nichts; alles wird bei Bedarf geholt.'}</p></div>
        <div class="panel"><h3>Am häufigsten injiziert</h3>${rank((inj.top_injected || []).slice(0, 8), n => `${n} ×`)}</div>
      </div>`);

    const journal = block('b-journal', 'Projekt-Journal', 'Die Diary-Seite: Projekte, Meilensteine, Logbuch.',
      `<div class="grid-2">
        <div class="counters">
          ${counter(j.projects_active || 0, 'aktive Projekte')}
          ${counter(j.projects_archived || 0, 'archiviert')}
          ${counter(j.logs_30d || 0, 'Log-Einträge in 30 T')}
          ${counter(relTime(j.last_log), 'letzter Log')}
          ${counter(j.errors_solutions || 0, 'Fehler & Lösungen')}
          ${counter(j.wiki_pages || 0, 'Wiki-Seiten')}
          ${counter(j.reminders_open || 0, 'offene Erinnerungen')}
          ${counter(j.reminders_overdue || 0, 'überfällig', j.reminders_overdue ? 'alert' : 'ok')}
        </div>
        <div class="panel"><h3>Fortschritt</h3>
          ${progress('Meilensteine erledigt', j.milestones_done || 0, j.milestones_total || 0)}
          ${progress('Aufgaben erledigt', j.tasks_done || 0, j.tasks_total || 0)}
          <p class="note">${fmt(j.logs_total)} Log-Einträge insgesamt.</p>
        </div>
      </div>`);

    const ranks = block('b-ranks', 'Ranglisten', '',
      `<div class="grid-3">
        <div class="panel"><h3>Meistgelesen</h3>${rank((q.most_accessed || []).slice(0, 8), n => `${fmt(n)} ×`)}</div>
        <div class="panel"><h3>Umfangreichste</h3>${rank((q.largest || []).slice(0, 8), n => `≈ ${fmt(n / CHARS_PER_TOKEN)} Tok.`)}</div>
        <div class="panel"><h3>Größte Projekte</h3>${rank((q.top_projects || []).slice(0, 8).map(([slug, n]) => [`/projects/${slug}`, n, slug]), n => fmt(n))}</div>
      </div>`);

    const fed = inst.federation || {};
    const onOff = (v, on = 'aktiv', off = 'aus') => `<span class="status ${v ? 'on' : 'off'}">${v ? on : off}</span>`;
    const instance = block('b-instance', 'Instanz', 'Die Sternwarte selbst.',
      `<div class="grid-2">
        <div class="panel"><dl class="spec">
          <dt>Version</dt><dd>${esc(inst.version)}</dd>
          <dt>Host</dt><dd>${esc(inst.hostname)} · Python ${esc(inst.python)}</dd>
          <dt>Postgres</dt><dd>${esc(inst.postgres_version)} · ${esc(inst.db_host)}:${esc(inst.db_port)}/${esc(inst.db_name)}</dd>
          <dt>Datenbank</dt><dd>${esc(fmtBytes(inst.db_size_bytes || 0))}</dd>
          <dt>pgvector</dt><dd>${inst.pgvector ? `${esc(inst.pgvector_version || '')} · HNSW ${inst.hnsw_index ? 'ja' : 'nein'}` : 'nicht installiert'}</dd>
          <dt>Embedding-Modell</dt><dd>${esc((inst.embed_model || '—').split('/').pop())}</dd>
          <dt>Embed-Server</dt><dd>${onOff(inst.embed_server_alive, 'läuft', 'gestoppt')}</dd>
          <dt>MCP-Server aktiv</dt><dd>${esc(inst.running_servers ?? '—')}</dd>
          <dt>Hook-Log</dt><dd>${esc(fmtBytes(inst.hook_log_bytes || 0))}</dd>
          <dt>Remote-Sync</dt><dd>${esc(relTime(inst.remote_sync?.last_sync))}</dd>
        </dl></div>
        <div class="panel"><h3>Föderation <span>${esc(fed.relay || 'kein Relay')}</span></h3>
          ${fed.identity ? `<p class="note ident">Identität: <b>${esc(fed.identity)}</b></p>` : ''}
          ${(fed.links || []).length ? `<table class="fed"><thead><tr><th>Alias</th><th>Peer</th><th>Tags</th><th>Sync</th></tr></thead><tbody>
            ${fed.links.map(l => `<tr><td class="mono">${esc(l.alias)}</td><td>${esc(l.peer)}</td><td class="mono">${esc((l.sync_tags || []).join(', ') || '—')}</td><td class="mono">${esc(relTime(l.last_synced))}</td></tr>`).join('')}
          </tbody></table>` : '<p class="note">Keine verknüpften Diaries.</p>'}
          <h3 class="sub">Datei-Memory <span>${esc(van.root || '')}</span></h3>
          ${progress('ins Diary importiert', van.imported || 0, van.files || 0)}
          ${(van.not_imported_examples || []).length ? `<p class="note">Noch nicht importiert: ${van.not_imported_examples.slice(0, 4).map(p => `<code>${esc(p.split('/').pop())}</code>`).join(', ')}${van.not_imported > 4 ? ' …' : ''}</p>` : ''}
        </div>
      </div>`);

    body.innerHTML = hero + act + dist + quality + graph + inject + journal + ranks + instance;
    drawTrend($('#trend'), trendSeries);
    observe();
  }

  function observe() {
    observer?.disconnect();
    observer = new IntersectionObserver(entries => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const el = e.target;
        el.classList.add('in');
        $$('[data-to]', el).forEach(countUp);
        $$('.ring .val', el).forEach(c => { c.style.strokeDashoffset = String(289 * (1 - Number(c.dataset.v))); });
        observer.unobserve(el);
      }
    }, { root: $('#view-stats'), threshold: .12 });
    observer.observe($('.hero-nums', body));
    $$('.block', body).forEach(b => observer.observe(b));
  }

  body.addEventListener('click', e => { const b = e.target.closest('[data-go]'); if (b) go(b.dataset.go); });

  let resizeTimer;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.view === 'stats' && data && $('#trend')) drawTrend($('#trend'), activity.series.slice(-Math.max(14, days))); }, 150);
  });

  return {
    enter() { if (!data || loadedFor !== days) load(); },
    invalidate() { data = null; activity = null; health = null; if (state.view === 'stats') load(); },
  };
})();

// ═════════════════════════════════════════════════════════════════════════
// Vorschläge — medium-confidence link suggestions, decided by hand
// ═════════════════════════════════════════════════════════════════════════
const review = (() => {
  const list = $('#review-list');
  let items = [], relTypes = ['related'], thresholds = { auto: .7, suggest: .35 }, busy = false;

  function setBadge(n, bump = false) {
    const b = $('#review-badge');
    b.hidden = !n;
    b.textContent = n > 99 ? '99+' : String(n);
    if (bump && !REDUCED.matches) { b.classList.remove('bump'); void b.offsetWidth; b.classList.add('bump'); }
  }

  async function refreshBadge() {
    try { setBadge((await api('/api/suggestions?limit=1')).pending); } catch { /* badge is decoration */ }
  }

  async function load() {
    list.innerHTML = '<p class="loading">Lade Vorschläge …</p>';
    let data;
    try { data = await api('/api/suggestions?limit=200'); }
    catch (err) { list.innerHTML = `<p class="loading">Konnte Vorschläge nicht laden: ${esc(err.message)}</p>`; return; }
    items = data.items; relTypes = data.rel_types; thresholds = data.thresholds;
    setBadge(data.pending);
    const pct = v => String(v).replace('.', ',');
    $('#review-sub').textContent = `Paare mit mittlerer Konfidenz (${pct(thresholds.suggest)} bis ${pct(thresholds.auto)}). `
      + 'Ab ' + pct(thresholds.auto) + ' verknüpft diary-mcp selbst, darunter entscheidest du. Abgelehnte Paare kommen nie wieder.';
    render();
  }

  const side = s => `<a class="sugg-side" href="${esc(memHash(s.path))}">
      <span class="s-type"><span class="sw dot-${esc(s.type)}"></span>${esc(TYPE_LABEL[s.type] || s.type)}</span>
      <h2>${esc(s.title || s.path)}</h2><span class="s-path">${esc(s.path)}</span>
      ${s.hook ? `<p class="s-hook">${esc(s.hook)}</p>` : ''}</a>`;

  function render() {
    if (!items.length) {
      list.innerHTML = '<p class="review-empty"><b>Keine offenen Vorschläge.</b> Neue kommen beim nächsten Nachtlauf (04:30) oder beim Speichern von Memories dazu.</p>';
      return;
    }
    list.innerHTML = items.map((it, i) => `
      <div class="sugg-wrap" data-id="${esc(it.id)}">
        <article class="sugg" tabindex="0" style="--i:${Math.min(i, 12)}" aria-label="Vorschlag: ${esc(it.a.title)} und ${esc(it.b.title)}">
          <div class="sugg-top">
            <span class="conf" title="Konfidenz; der Strich markiert die Schwelle für automatisches Verknüpfen">
              <span class="conf-track" style="--v:${it.confidence};--auto:${thresholds.auto}"><i></i></span>${esc(it.confidence.toFixed(2).replace('.', ','))}</span>
            <span class="sugg-why">${it.evidence.split('; ').filter(Boolean).map(r => `<span>${esc(r)}</span>`).join('')}</span>
          </div>
          <div class="sugg-pair">${side(it.a)}
            <svg class="bridge" viewBox="0 0 96 24" aria-hidden="true"><line x1="8" y1="12" x2="88" y2="12"/><line class="solid" x1="8" y1="12" x2="88" y2="12"/>
              <circle cx="6" cy="12" r="3.5"/><circle cx="90" cy="12" r="3.5"/></svg>
            ${side(it.b)}</div>
          <div class="sugg-actions">
            <label class="sr" for="rel-${esc(it.id)}">Beziehung</label>
            <select id="rel-${esc(it.id)}">${relTypes.map(t => `<option value="${esc(t)}">${esc(REL_LABEL[t] || t)}</option>`).join('')}</select>
            <span class="spacer"></span>
            <button type="button" class="act reject" data-act="reject">Ablehnen <kbd>R</kbd></button>
            <button type="button" class="act approve" data-act="approve">Verknüpfen <kbd>A</kbd></button>
          </div>
        </article>
      </div>`).join('');
    const first = list.querySelector('.sugg');
    if (first) first.classList.add('current');
  }

  function focusCard(card) {
    if (!card) return;
    $$('.sugg.current', list).forEach(c => c.classList.remove('current'));
    card.classList.add('current');
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: 'nearest', behavior: REDUCED.matches ? 'auto' : 'smooth' });
  }

  async function act(wrap, decision) {
    if (busy || !wrap || wrap.classList.contains('gone')) return;
    const card = wrap.querySelector('.sugg');
    const rel = card.querySelector('select').value;
    busy = true;
    card.querySelectorAll('.act').forEach(b => { b.disabled = true; });
    try {
      const res = await api('/api/suggestions/decide', postJSON({ ids: [wrap.dataset.id], decision, rel_type: rel }));
      if (!res.done) throw new Error('bereits entschieden');
    } catch (err) {
      card.querySelectorAll('.act').forEach(b => { b.disabled = false; });
      busy = false;
      toast(`Konnte nicht speichern: ${err.message}`, true);
      return;
    }
    card.classList.add(decision === 'approve' ? 'linked' : 'rejected');
    const next = wrap.nextElementSibling?.querySelector('.sugg') || wrap.previousElementSibling?.querySelector('.sugg');
    items = items.filter(it => it.id !== wrap.dataset.id);
    setBadge(items.length, true);
    stats.invalidate();
    atlas.invalidate();
    setTimeout(() => {
      wrap.classList.add('gone');
      focusCard(next);
      setTimeout(() => { wrap.remove(); if (!items.length) render(); }, REDUCED.matches ? 0 : 700);
      busy = false;
    }, REDUCED.matches ? 0 : 520);
  }

  list.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (b) act(b.closest('.sugg-wrap'), b.dataset.act);
    else if (!e.target.closest('a, select')) focusCard(e.target.closest('.sugg'));
  });
  list.addEventListener('keydown', e => {
    const card = e.target.closest('.sugg');
    if (!card || e.target.closest('select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const wrap = card.closest('.sugg-wrap');
    const k = e.key.toLowerCase();
    if (k === 'a') { e.preventDefault(); act(wrap, 'approve'); }
    else if (k === 'r') { e.preventDefault(); act(wrap, 'reject'); }
    else if (k === 'j' || e.key === 'ArrowDown') { e.preventDefault(); focusCard(wrap.nextElementSibling?.querySelector('.sugg')); }
    else if (k === 'k' || e.key === 'ArrowUp') { e.preventDefault(); focusCard(wrap.previousElementSibling?.querySelector('.sugg')); }
  });

  // ── Auto-Connect: preview (dry run) first, then run on confirmation ──
  const TRIGGER_LABEL = { nightly: 'nachts', web: 'von Hand', manual: 'von Hand' };
  const ACTION_LABEL = { auto: 'verknüpfen', spine: 'Rückgrat', suggest: 'vormerken' };
  const startBtn = $('#autorun-start'), result = $('#autorun-result');

  async function loadInfo() {
    try {
      const { last_run: lr } = await api('/api/links/auto');
      $('#autorun-last').textContent = lr
        ? `Letzter Lauf ${relTime(lr.at)} · ${TRIGGER_LABEL[lr.trigger] || lr.trigger} · ${fmt(lr.auto)} verknüpft, `
          + `${fmt(lr.spine || 0)} Rückgrat, ${fmt(lr.suggested)} vorgemerkt`
        : 'Noch kein Lauf erfasst. Nachts um 04:30 läuft er automatisch.';
    } catch { /* info line only */ }
  }

  function summary(r, preview) {
    const parts = [];
    if (r.auto) parts.push(`<b>${fmt(r.auto)}</b> Paare ${preview ? 'verknüpfen' : 'verknüpft'}`);
    if (r.spine) parts.push(`<b>${fmt(r.spine)}</b> Projekt-Rückgrat-Links ${preview ? 'setzen' : 'gesetzt'}`);
    if (r.suggested) parts.push(`<b>${fmt(r.suggested)}</b> ${preview ? 'vormerken' : 'vorgemerkt'}`);
    if (!parts.length) return 'Alles verbunden, nichts zu tun.';
    return (preview ? 'Würde ' : 'Fertig: ') + parts.join(', ') + '.';
  }

  async function runAuto(dryRun) {
    startBtn.setAttribute('aria-busy', 'true');
    $$('button', result).forEach(b => { b.disabled = true; });
    try {
      return await api('/api/links/auto', postJSON({ dry_run: dryRun }));
    } catch (err) {
      toast(err.message.startsWith('409') ? 'Ein Verknüpfungs-Lauf ist gerade aktiv. Versuch es gleich nochmal.'
        : `Auto-Connect fehlgeschlagen: ${err.message}`, true);
      return null;
    } finally {
      startBtn.removeAttribute('aria-busy');
    }
  }

  startBtn.addEventListener('click', async () => {
    const r = await runAuto(true);
    if (!r) return;
    const nothing = !(r.auto || r.spine || r.suggested);
    result.hidden = false;
    result.innerHTML = `<p class="ar-sum">${summary(r, true)}</p>
      ${r.examples.length ? `<ul>${r.examples.map(e => `<li><span class="k">${esc(ACTION_LABEL[e.action] || e.action)}</span>
        <span class="c">${esc(e.confidence.toFixed(2).replace('.', ','))}</span>
        <span class="pp" title="${esc(e.evidence)}">${esc(e.a)} ↔ ${esc(e.b)}</span></li>`).join('')}</ul>` : ''}
      <div class="autorun-actions">${nothing ? '<button type="button" class="act reject" data-ar="close">Schließen</button>'
        : '<button type="button" class="act approve" data-ar="run">Jetzt ausführen</button><button type="button" class="act reject" data-ar="close">Abbrechen</button>'}</div>`;
  });

  result.addEventListener('click', async e => {
    const b = e.target.closest('[data-ar]');
    if (!b) return;
    if (b.dataset.ar === 'close') { result.hidden = true; return; }
    const r = await runAuto(false);
    if (!r) { $$('button', result).forEach(x => { x.disabled = false; }); return; }
    result.innerHTML = `<p class="ar-sum">${summary(r, false)}</p>
      <div class="autorun-actions">${r.auto || r.spine ? '<a href="#/karte">In der Sternkarte ansehen →</a>' : ''}
      <button type="button" class="act reject" data-ar="close">Schließen</button></div>`;
    stats.invalidate();
    atlas.invalidate();
    loadInfo();
    await load();
  });

  return {
    enter() { loadInfo(); load().then(() => focusCard(list.querySelector('.sugg'))); },
    refreshBadge,
  };
})();

// ── boot ─────────────────────────────────────────────────────────────────
async function loadTree() {
  const nodes = await api('/api/tree');
  state.nodes = nodes;
  state.byPath = new Map(nodes.map(n => [n.path, n]));
  state.bySlug = new Map();
  for (const n of nodes) {
    const slug = n.path.split('/').pop();
    if (!state.bySlug.has(slug)) state.bySlug.set(slug, []);
    state.bySlug.get(slug).push(n.path);
  }
  renderTree();
  renderWelcome();
}

(async () => {
  setTimeout(() => document.body.classList.remove('boot'), 2600);
  try { await loadTree(); }
  catch (err) { toast(`Baum konnte nicht geladen werden: ${err.message}`, true, 15000); }
  route();
  review.refreshBadge();
})();
