/* Sanjaya — live agent trajectory viewer for the Antigravity / Jetski AuxPane. */
(function () {
  'use strict';

  // ------------------------------------------------------------------ state
  const S = {
    convs: [],
    convId: null,
    attachedId: (window.sidecar && window.sidecar.conversationId) || null,
    stack: [],            // parent conversations when drilling into subagents
    model: null,
    version: null,
    annotations: {},
    tab: 'graph',
    mode: 'phases',
    filter: '',
    selected: null,       // {type:'step', i} | {type:'group', kind, index}
    follow: true,
    lastChangeAt: 0,
    view: { x: 16, y: 16, k: 1 },
    autoTail: true,
    layout: null,
    details: new Map(),
    subtree: null,
    config: { agentapi: false },
  };
  const POLL_MS = 2000;
  const LIST_POLL_MS = 10000;
  const GAP_CAP_S = 90;
  const CHAR_W = 6.1;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const esc = (v) => String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const SVGNS = 'http://www.w3.org/2000/svg';

  // ------------------------------------------------------------------ api
  function tokenParam() {
    const t = window.sidecar && window.sidecar.token;
    return t ? `token=${encodeURIComponent(t)}` : '';
  }
  async function api(path, params = {}, opts = {}) {
    const qs = new URLSearchParams(params).toString();
    const url = path + (qs ? `?${qs}` : '');
    const doFetch = (window.sidecar && window.sidecar.fetch) || fetch;
    const res = await doFetch(url, opts);
    const ctype = res.headers.get('Content-Type') || '';
    const body = ctype.includes('json') ? await res.json() : await res.text();
    if (!res.ok) throw new Error((body && body.error) || res.statusText);
    return body;
  }
  const post = (path, body) => api(path, {}, { method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' } });

  // ------------------------------------------------------------------ utils
  function fmtDur(s) {
    if (s == null || isNaN(s)) return '—';
    if (s < 1) return `${Math.round(s * 1000)}ms`;
    if (s < 60) return `${s.toFixed(1)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  }
  function fmtNum(n) {
    if (n == null) return '—';
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
    return String(n);
  }
  function relTime(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return '';
    const d = (Date.now() - t) / 1000;
    if (d < 60) return 'just now';
    if (d < 3600) return `${Math.floor(d / 60)}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    return `${Math.floor(d / 86400)}d ago`;
  }
  function clipText(text, px) {
    const max = Math.max(4, Math.floor(px / CHAR_W));
    text = String(text || '').replace(/\s+/g, ' ');
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
  }
  function toast(msg, ms = 2600) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms);
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    ta.remove();
    return ok;
  }
  const catColor = (c) => `var(--c-${c || 'other'})`;
  const kindColor = (k) => `var(--c-${k || 'system'})`;
  function stepByIndex(i) {
    if (!S.model) return null;
    if (!S.model._byIndex) {
      S.model._byIndex = new Map(S.model.steps.map((s) => [s.i, s]));
    }
    return S.model._byIndex.get(i) || null;
  }
  const isErr = (s) => s && (s.status === 'ERROR' || s.kind === 'error');

  // ------------------------------------------------------------------ filter
  function parseFilter(q) {
    const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return terms.map((t) => {
      if (t.startsWith('#') && /^#\d+$/.test(t)) return { type: 'idx', v: +t.slice(1) };
      if (t.startsWith('is:')) return { type: 'is', v: t.slice(3) };
      return { type: 'text', v: t };
    });
  }
  function stepMatches(s, terms) {
    if (!terms.length) return true;
    return terms.every((t) => {
      if (t.type === 'idx') return s.i === t.v;
      if (t.type === 'is') {
        if (t.v === 'error') return isErr(s) || (s.tool_calls || []).some((c) => c.status === 'error');
        return s.kind === t.v || s.category === t.v;
      }
      const hay = [s.label, s.preview, s.tool_name, s.error, s.category,
        ...(s.tool_calls || []).map((c) => `${c.name} ${c.summary}`)].join(' ').toLowerCase();
      return hay.includes(t.v);
    });
  }
  function matchSet() {
    if (!S.model || !S.filter.trim()) return null;
    const terms = parseFilter(S.filter);
    return new Set(S.model.steps.filter((s) => stepMatches(s, terms)).map((s) => s.i));
  }

  // ------------------------------------------------------------------ loading
  async function loadConversations() {
    try {
      const data = await api('/api/conversations', { limit: 80 });
      S.convs = data.conversations || [];
    } catch (err) {
      S.convs = [];
      toast(`Could not list conversations: ${err.message}`);
    }
    renderPicker();
  }

  function renderPicker() {
    const sel = $('#conv-select');
    const opts = S.convs.slice();
    if (S.convId && !opts.some((c) => c.id === S.convId)) {
      opts.unshift({ id: S.convId, title: (S.model && S.model.title) || S.convId, updated: '' });
    }
    sel.innerHTML = opts.map((c) => {
      const tag = c.id === S.attachedId ? '● ' : '';
      const when = c.updated ? ` · ${relTime(c.updated)}` : '';
      return `<option value="${esc(c.id)}">${esc(tag + (c.title || c.id).slice(0, 80) + when)}</option>`;
    }).join('');
    if (S.convId) sel.value = S.convId;
    $('#back').hidden = S.stack.length === 0;
  }

  async function openConversation(id, { push = false, keepView = false } = {}) {
    if (!id) return;
    if (push && S.convId && S.convId !== id) S.stack.push(S.convId);
    S.convId = id;
    S.version = null;
    S.model = null;
    S.subtree = null;
    S.details.clear();
    S.selected = null;
    if (!keepView) { S.autoTail = true; }
    closeInspector();
    renderPicker();
    await refresh(true);
  }

  async function refresh(force = false) {
    if (!S.convId) return;
    try {
      if (!force) {
        const meta = await api('/api/meta', { id: S.convId });
        if (meta.version === S.version) { updateLive(); return; }
      }
      const prevSteps = S.model ? S.model.steps.length : 0;
      const [model, notes] = await Promise.all([
        api('/api/trajectory', { id: S.convId }),
        api('/api/annotations', { id: S.convId }).catch(() => ({ annotations: {} })),
      ]);
      if (S.version && model.version !== S.version) S.lastChangeAt = Date.now();
      if (!S.version) S.lastChangeAt = model.mtime ? model.mtime * 1000 : 0;
      S.model = model;
      S.version = model.version;
      S.annotations = notes.annotations || {};
      if (model.steps.length !== prevSteps) S.subtree = null;
      renderAll();
    } catch (err) {
      $('#stats').innerHTML = `<span class="err"><b>Error:</b> ${esc(err.message)}</span>`;
    }
    updateLive();
  }

  function updateLive() {
    const on = S.lastChangeAt && Date.now() - S.lastChangeAt < 60000;
    $('#live').classList.toggle('on', !!on);
    $('#live').title = on ? 'Live — transcript updated in the last minute' : 'Idle';
  }

  async function followTick() {
    if (!S.follow || S.stack.length) return;
    if (S.attachedId) {
      if (S.convId !== S.attachedId) await openConversation(S.attachedId);
      return;
    }
    await loadConversations();
    const newest = S.convs[0];
    if (newest && newest.id !== S.convId) await openConversation(newest.id);
  }

  // ------------------------------------------------------------------ render
  function renderAll() {
    renderStats();
    renderTab();
    if (S.selected) renderInspector();
  }

  function renderStats() {
    const m = S.model;
    if (!m) { $('#stats').innerHTML = ''; return; }
    const st = m.stats;
    const totalTokens = (st.input_tokens || 0) + (st.output_tokens || 0) + (st.cache_read_tokens || 0);
    const cacheRatio = (st.input_tokens + st.cache_read_tokens)
      ? Math.round((100 * st.cache_read_tokens) / (st.input_tokens + st.cache_read_tokens)) : 0;
    const tokenSpan = totalTokens > 0
      ? `<span title="Fresh input / output / cache-read tokens"><b>${fmtNum(st.input_tokens)}</b> in · <b>${fmtNum(st.output_tokens)}</b> out · <b>${cacheRatio}%</b> cached</span>`
      : '';
    $('#stats').innerHTML = [
      `<span><b>${st.steps}</b> steps</span>`,
      `<span><b>${st.turns}</b> turns</span>`,
      `<span><b>${st.tool_calls}</b> tool calls</span>`,
      `<span class="${st.errors ? 'err' : ''}"><b>${st.errors}</b> errors</span>`,
      `<span><b>${st.subagents}</b> subagents</span>`,
      `<span><b>${st.compactions}</b> compactions</span>`,
      tokenSpan,
      `<span><b>${fmtDur(st.duration_s)}</b></span>`,
    ].filter(Boolean).join('');
  }

  function renderTab() {
    $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === S.tab));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${S.tab}`));
    $('#graph-controls').style.display = S.tab === 'graph' ? '' : 'none';
    $('#fit').style.display = S.tab === 'graph' ? '' : 'none';
    if (!S.model) return;
    if (S.tab === 'graph') renderGraph();
    else if (S.tab === 'timeline') renderTimeline();
    else if (S.tab === 'tools') renderTools();
    else if (S.tab === 'context') renderContext();
    else if (S.tab === 'subagents') renderSubagents();
    else if (S.tab === 'notes') renderNotes();
  }

  // ------------------------------------------------------------------ graph layout
  function layoutPhases(m) {
    const nodes = []; const edges = [];
    let y = 0;
    m.phases.forEach((p, idx) => {
      const isReq = p.category === 'request';
      const isMsg = p.category === 'message';
      const isFull = isReq || isMsg;
      const h = isFull ? 46 : 40;
      const sub = isReq
        ? `turn ${(p.turn || 0) + 1} · step #${p.start}`
        : isMsg
        ? `message · step #${p.start}`
        : `#${p.start}–${p.end} · ${p.tool_calls} tools · ${fmtDur(p.duration_s)}${p.errors ? ` · ${p.errors} err` : ''}`;
      const title = isFull ? p.label : `${p.category} · ${p.label}`;
      nodes.push({ id: `p${idx}`, x: isFull ? 0 : 24, y, w: isFull ? 320 : 296, h,
        color: catColor(p.category), title,
        sub, steps: p.steps, error: p.errors > 0,
        sel: { type: 'group', kind: 'phase', index: idx } });
      if (idx > 0) edges.push({ from: `p${idx - 1}`, to: `p${idx}` });
      y += h + 14;
    });
    return { nodes, edges };
  }

  function layoutTurns(m) {
    const nodes = []; const edges = [];
    let y = 0;
    m.turns.forEach((t, idx) => {
      const tools = Object.entries(t.tools || {}).sort((a, b) => b[1] - a[1]).slice(0, 5);
      const h = 62;
      const turnY = y;
      nodes.push({ id: `t${idx}`, x: 0, y: turnY, w: 320, h, color: 'var(--c-user)',
        title: `${idx + 1}. ${t.request}`,
        sub: `${t.steps.length} steps · ${t.tool_calls} tools · ${fmtDur(t.duration_s)}${t.errors ? ` · ${t.errors} err` : ''}`,
        pills: tools.map(([n, c]) => (c > 1 ? `${n}×${c}` : n)),
        steps: t.steps, error: t.errors > 0, sel: { type: 'group', kind: 'turn', index: idx } });
      if (idx > 0) edges.push({ from: `t${idx - 1}`, to: `t${idx}` });
      const subs = m.subagents.filter((s) => t.steps.includes(s.result_step));
      const subRows = Math.ceil(subs.length / 2);
      subs.forEach((s, j) => {
        const id = `t${idx}s${j}`;
        const col = j % 2;
        const row = Math.floor(j / 2);
        nodes.push({ id, x: 350 + col * 186, y: turnY + row * 54, w: 176, h: 46, color: 'var(--c-delegate)',
          title: s.title || s.role || s.id, sub: s.exists ? 'subagent · click to open' : 'subagent (no transcript)',
          steps: [s.result_step], subagent: s.id, exists: s.exists });
        edges.push({ from: `t${idx}`, to: id, type: 'sub', side: true });
      });
      const slotH = Math.max(h, subRows * 54);
      y += slotH + 18;
    });
    return { nodes, edges };
  }

  function layoutSteps(m) {
    const nodes = []; const edges = [];
    const spine = m.steps.filter((s) => s.kind !== 'tool' && s.kind !== 'system');
    const TW = 150; const TH = 32; const PER_ROW = 4;
    let y = 0; let prev = null;
    for (const s of spine) {
      const calls = s.tool_calls || [];
      const extra = [];
      calls.forEach((c) => {
        extra.push({ call: c });
        const res = c.result_step != null ? stepByIndex(c.result_step) : null;
        (res && res.children || []).forEach((cid) => {
          const sub = m.subagents.find((x) => x.id === cid);
          extra.push({ sub, parentCall: c.id });
        });
      });
      const rows = Math.max(1, Math.ceil(extra.length / PER_ROW));
      const h = 40;
      const rowH = Math.max(h, rows * (TH + 6) - 6);
      const id = `s${s.i}`;
      const title = s.kind === 'user' ? s.label
        : s.kind === 'model' ? (calls.length ? `${calls.length} call${calls.length > 1 ? 's' : ''}: ${s.label}` : (s.preview || 'Response'))
        : s.label;
      const ctxTokens = s.tokens ? (s.tokens.in + s.tokens.cached) : 0;
      const tk = ctxTokens > 0 ? ` · ${fmtNum(ctxTokens)} ctx` : '';
      nodes.push({ id, x: 0, y, w: 240, h, color: s.kind === 'model' ? catColor(s.category) : kindColor(s.kind),
        title, sub: `#${s.i} · ${s.kind}${s.gap_s != null ? ` · +${fmtDur(s.gap_s)}` : ''}${tk}`,
        steps: [s.i], error: isErr(s), sel: { type: 'step', i: s.i } });
      if (prev) edges.push({ from: prev, to: id });
      prev = id;
      extra.forEach((e, j) => {
        const col = j % PER_ROW; const row = Math.floor(j / PER_ROW);
        const x = 270 + col * (TW + 8); const ty = y + row * (TH + 6);
        if (e.call) {
          const c = e.call;
          const res = c.result_step != null ? stepByIndex(c.result_step) : null;
          const nid = `c${c.id}`;
          nodes.push({ id: nid, x, y: ty, w: TW, h: TH,
            color: c.status === 'error' ? 'var(--c-error)' : catColor(c.category),
            title: c.name, sub: c.status === 'pending' ? 'running…' : `${fmtDur(c.duration_s)} · ${c.summary}`,
            steps: res ? [s.i, res.i] : [s.i], error: c.status === 'error', pending: c.status === 'pending',
            sel: { type: 'step', i: res ? res.i : s.i }, small: true });
          edges.push({ from: id, to: nid, side: true });
        } else if (e.sub) {
          const nid = `sub${e.sub.id}`;
          nodes.push({ id: nid, x, y: ty, w: TW, h: TH, color: 'var(--c-delegate)',
            title: e.sub.title || e.sub.role || 'subagent', sub: e.sub.exists ? 'open subagent ↗' : 'no transcript',
            steps: [e.sub.result_step], subagent: e.sub.id, exists: e.sub.exists, small: true });
          edges.push({ from: `c${e.parentCall}`, to: nid, type: 'sub', side: true });
        }
      });
      y += rowH + 14;
    }
    return { nodes, edges };
  }

  function renderGraph() {
    const m = S.model;
    const svg = $('#graph');
    const empty = $('#graph-empty');
    $$('#graph-controls button').forEach((b) => b.classList.toggle('active', b.dataset.mode === S.mode));
    const L = S.mode === 'turns' ? layoutTurns(m) : S.mode === 'steps' ? layoutSteps(m) : layoutPhases(m);
    S.layout = L;
    svg.innerHTML = '';
    if (!L.nodes.length) {
      empty.hidden = false;
      empty.textContent = 'No steps yet.';
      return;
    }
    empty.hidden = true;
    const matches = matchSet();
    const byId = new Map(L.nodes.map((n) => [n.id, n]));
    const vp = document.createElementNS(SVGNS, 'g');
    vp.setAttribute('id', 'viewport');
    svg.appendChild(vp);

    const eg = document.createElementNS(SVGNS, 'g');
    vp.appendChild(eg);
    for (const e of L.edges) {
      const a = byId.get(e.from); const b = byId.get(e.to);
      if (!a || !b) continue;
      const p = document.createElementNS(SVGNS, 'path');
      let d;
      if (e.side) {
        const x1 = a.x + a.w; const y1 = a.y + Math.min(a.h / 2, 20);
        const x2 = b.x; const y2 = b.y + b.h / 2;
        const mx = (x1 + x2) / 2;
        d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
      } else {
        const x1 = a.x + 18; const y1 = a.y + a.h;
        const x2 = b.x + 18; const y2 = b.y;
        d = `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`;
      }
      p.setAttribute('d', d);
      p.setAttribute('class', `edge${e.type === 'sub' ? ' sub' : ''}`);
      eg.appendChild(p);
    }

    const ng = document.createElementNS(SVGNS, 'g');
    vp.appendChild(ng);
    for (const n of L.nodes) {
      const g = document.createElementNS(SVGNS, 'g');
      const cls = ['node'];
      if (n.error) cls.push('error');
      if (matches) cls.push(n.steps.some((i) => matches.has(i)) ? 'match' : 'dim');
      if (isSelectedNode(n)) cls.push('selected');
      g.setAttribute('class', cls.join(' '));
      g.setAttribute('transform', `translate(${n.x},${n.y})`);
      g.dataset.id = n.id;
      const box = document.createElementNS(SVGNS, 'rect');
      box.setAttribute('class', 'box');
      box.setAttribute('width', n.w); box.setAttribute('height', n.h);
      box.setAttribute('rx', 6);
      if (n.pending) box.setAttribute('stroke-dasharray', '4 3');
      g.appendChild(box);
      const bar = document.createElementNS(SVGNS, 'rect');
      bar.setAttribute('class', 'bar');
      bar.setAttribute('width', 4); bar.setAttribute('height', n.h - 8);
      bar.setAttribute('x', 4); bar.setAttribute('y', 4); bar.setAttribute('rx', 2);
      bar.setAttribute('style', `fill:${n.color}`);
      g.appendChild(bar);
      const title = document.createElementNS(SVGNS, 'text');
      title.setAttribute('x', 14); title.setAttribute('y', n.small ? 13 : 16);
      title.textContent = clipText(n.title, n.w - 22);
      g.appendChild(title);
      const sub = document.createElementNS(SVGNS, 'text');
      sub.setAttribute('class', 'sub');
      sub.setAttribute('x', 14); sub.setAttribute('y', n.small ? 26 : 31);
      sub.textContent = clipText(n.sub, n.w - 22);
      g.appendChild(sub);
      if (n.pills && n.pills.length) {
        let px = 14;
        for (const label of n.pills) {
          const w = Math.min(110, label.length * 5.8 + 12);
          if (px + w > n.w - 6) break;
          const pg = document.createElementNS(SVGNS, 'g');
          pg.setAttribute('class', 'pill');
          const r = document.createElementNS(SVGNS, 'rect');
          r.setAttribute('x', px); r.setAttribute('y', 40); r.setAttribute('width', w);
          r.setAttribute('height', 15); r.setAttribute('rx', 7);
          r.setAttribute('style', 'fill:var(--s-secondary);stroke:var(--s-border)');
          const t = document.createElementNS(SVGNS, 'text');
          t.setAttribute('x', px + 6); t.setAttribute('y', 51);
          t.textContent = clipText(label, w - 8);
          pg.appendChild(r); pg.appendChild(t); g.appendChild(pg);
          px += w + 4;
        }
      }
      const tip = document.createElementNS(SVGNS, 'title');
      tip.textContent = `${n.title}\n${n.sub}`;
      g.appendChild(tip);
      g.addEventListener('click', (ev) => { ev.stopPropagation(); onNodeClick(n); });
      ng.appendChild(g);
    }
    if (S.autoTail) tailView(); else applyView();
  }

  function isSelectedNode(n) {
    const sel = S.selected;
    if (!sel) return false;
    if (n.sel && sel.type === n.sel.type) {
      if (sel.type === 'step') return n.sel.i === sel.i;
      return sel.kind === n.sel.kind && sel.index === n.sel.index;
    }
    return false;
  }

  function onNodeClick(n) {
    if (n.subagent) {
      if (n.exists) openConversation(n.subagent, { push: true });
      else toast('Subagent transcript not found on this machine');
      return;
    }
    if (n.sel) select(n.sel);
  }

  // ------------------------------------------------------------------ pan / zoom
  function applyView() {
    const vp = $('#viewport');
    if (vp) vp.setAttribute('transform', `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
  }
  function bounds() {
    const L = S.layout;
    if (!L || !L.nodes.length) return { x: 0, y: 0, w: 1, h: 1 };
    let x1 = Infinity; let y1 = Infinity; let x2 = -Infinity; let y2 = -Infinity;
    for (const n of L.nodes) {
      x1 = Math.min(x1, n.x); y1 = Math.min(y1, n.y);
      x2 = Math.max(x2, n.x + n.w); y2 = Math.max(y2, n.y + n.h);
    }
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  }
  function fitView() {
    const svg = $('#graph'); const b = bounds();
    const cw = svg.clientWidth || 400; const ch = svg.clientHeight || 400;
    const k = Math.max(0.2, Math.min(1.2, (cw - 24) / b.w, (ch - 24) / b.h));
    S.view = { k, x: (cw - b.w * k) / 2 - b.x * k, y: 12 - b.y * k };
    applyView();
  }
  function tailView() {
    const svg = $('#graph'); const b = bounds();
    const cw = svg.clientWidth || 400; const ch = svg.clientHeight || 400;
    const k = Math.max(0.45, Math.min(1, (cw - 24) / Math.min(b.w, 900)));
    S.view = { k, x: 12 - b.x * k, y: Math.min(12, ch - 24 - (b.y + b.h) * k) };
    applyView();
  }
  function centerOn(node) {
    const svg = $('#graph');
    const ch = svg.clientHeight || 400;
    S.view.y = ch / 3 - (node.y + node.h / 2) * S.view.k;
    S.view.x = Math.min(S.view.x, 12 - node.x * S.view.k);
    applyView();
  }
  function initPanZoom() {
    const svg = $('#graph');
    let drag = null;
    svg.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      S.autoTail = false;
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left; const my = ev.clientY - rect.top;
      if (ev.ctrlKey || ev.metaKey) {
        const factor = Math.exp(-ev.deltaY * 0.0022);
        const k = Math.max(0.15, Math.min(3, S.view.k * factor));
        S.view.x = mx - ((mx - S.view.x) * k) / S.view.k;
        S.view.y = my - ((my - S.view.y) * k) / S.view.k;
        S.view.k = k;
      } else {
        S.view.x -= ev.deltaX;
        S.view.y -= ev.deltaY;
      }
      applyView();
    }, { passive: false });
    svg.addEventListener('pointerdown', (ev) => {
      if (ev.target.closest && ev.target.closest('.node')) return;
      drag = { x: ev.clientX, y: ev.clientY, vx: S.view.x, vy: S.view.y };
      svg.classList.add('dragging');
      svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener('pointermove', (ev) => {
      if (!drag) return;
      S.autoTail = false;
      S.view.x = drag.vx + ev.clientX - drag.x;
      S.view.y = drag.vy + ev.clientY - drag.y;
      applyView();
    });
    const end = () => { drag = null; svg.classList.remove('dragging'); };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
  }

  // ------------------------------------------------------------------ timeline
  function renderTimeline() {
    const m = S.model;
    const el = $('#view-timeline');
    const matches = matchSet();
    const showSystem = /is:system/.test(S.filter);
    let steps = m.steps.filter((s) => showSystem || s.kind !== 'system');
    if (matches) steps = steps.filter((s) => matches.has(s.i));
    const truncated = steps.length > 1500;
    if (truncated) steps = steps.slice(-1500);
    // Compressed time axis: idle gaps are capped so long user pauses don't
    // squash active work into a sliver.
    let pos = 0; const rows = [];
    for (const s of steps) {
      const gap = Math.max(0, s.gap_s || 0);
      const seg = Math.min(gap, GAP_CAP_S);
      rows.push({ s, start: pos, len: seg, idle: gap > GAP_CAP_S ? gap : 0 });
      pos += seg;
    }
    const total = Math.max(pos, 1);
    let html = truncated ? '<div class="gap-break">Showing the latest 1,500 steps</div>' : '';
    let turn = null;
    for (const r of rows) {
      const s = r.s;
      if (s.turn !== turn) {
        turn = s.turn;
        const t = m.turns[turn];
        if (t) html += `<div class="timeline-turn">Turn ${turn + 1}: ${esc(t.request.split('\n')[0].slice(0, 120))}</div>`;
      }
      if (r.idle) html += `<div class="gap-break">⋯ idle ${fmtDur(r.idle)}</div>`;
      const left = (100 * r.start) / total;
      const width = Math.max(0.4, (100 * r.len) / total);
      const color = s.kind === 'tool' || s.kind === 'model' ? catColor(s.category) : kindColor(s.kind);
      const dur = s.duration_s != null ? s.duration_s : s.gap_s;
      const label = `${s.kind === 'tool' ? '← ' + (s.tool_name || '') + ': ' : ''}${s.label || ''}`;
      html += `<div class="timeline-row${S.selected && S.selected.type === 'step' && S.selected.i === s.i ? ' selected' : ''}" data-i="${s.i}">
        <div class="idx">#${s.i}</div>
        <div class="timeline-label">${isErr(s) ? '❌ ' : ''}${esc(label)} <span style="color:var(--s-muted)">${fmtDur(dur)}</span></div>
        <div class="timeline-track">
          <div class="timeline-bar" style="left:${left}%;width:${width}%;background:${isErr(s) ? 'var(--c-error)' : color}"></div>
        </div></div>`;
    }
    el.innerHTML = html || '<div class="empty">No matching steps.</div>';
    el.querySelectorAll('.timeline-row').forEach((row) =>
      row.addEventListener('click', () => select({ type: 'step', i: +row.dataset.i })));
    if (S.autoTail) el.scrollTop = el.scrollHeight;
  }

  // ------------------------------------------------------------------ tools
  function renderTools() {
    const m = S.model;
    const el = $('#view-tools');
    if (!m.tools.length) { el.innerHTML = '<div class="empty">No tool calls yet.</div>'; return; }
    const max = Math.max(...m.tools.map((t) => t.count));
    const cats = {};
    m.tools.forEach((t) => { cats[t.category] = (cats[t.category] || 0) + t.count; });
    const catHtml = Object.entries(cats).sort((a, b) => b[1] - a[1])
        .map(([c, n]) => `<span class="chip" style="background:${catColor(c)}">${esc(c)} ${n}</span>`).join('');
    el.innerHTML = `<div class="card"><div class="meta">Work mix by category</div>${catHtml}</div>
      <table><thead><tr><th>Tool</th><th>Calls</th><th class="num">Err</th><th class="num">Avg</th><th class="num">Total</th></tr></thead><tbody>
      ${m.tools.map((t) => `<tr data-tool="${esc(t.name)}" class="link-row">
        <td><span class="chip" style="background:${catColor(t.category)}">&nbsp;</span><span class="link">${esc(t.name)}</span></td>
        <td style="min-width:90px"><div style="display:flex;align-items:center;gap:6px"><div class="hbar${t.errors ? ' err' : ''}" style="width:${Math.round((70 * t.count) / max)}%"></div>${t.count}</div></td>
        <td class="num">${t.errors || ''}</td><td class="num">${fmtDur(t.avg_s)}</td><td class="num">${fmtDur(t.total_s)}</td></tr>`).join('')}
      </tbody></table>`;
    el.querySelectorAll('tr[data-tool]').forEach((tr) => tr.addEventListener('click', () => {
      S.filter = tr.dataset.tool; $('#search').value = S.filter; switchTab('timeline');
    }));
  }

  // ------------------------------------------------------------------ context
  function renderContext() {
    const m = S.model;
    const el = $('#view-context');
    const pts = m.steps.filter((s) => s.tokens);
    if (!pts.length) { el.innerHTML = '<div class="empty">No token data in this transcript.</div>'; return; }
    const ctx = pts.map((s) => s.tokens.in + s.tokens.cached);
    const peak = Math.max(...ctx);
    const st = m.stats;
    const W = Math.max(320, el.clientWidth - 24); const H = 180; const P = 34;
    const bw = Math.max(1, (W - P - 6) / pts.length);
    const y = (v) => H - 18 - ((H - 30) * v) / (peak || 1);
    let bars = '';
    pts.forEach((s, j) => {
      const x = P + j * bw;
      const yc = y(s.tokens.cached); const yt = y(s.tokens.in + s.tokens.cached);
      bars += `<g class="ctx-bar" data-i="${s.i}" style="cursor:pointer"><title>#${s.i}: ${fmtNum(s.tokens.in)} fresh + ${fmtNum(s.tokens.cached)} cached in, ${fmtNum(s.tokens.out)} out</title>
        <rect x="${x}" y="${yc}" width="${Math.max(1, bw - 1)}" height="${H - 18 - yc}" style="fill:color-mix(in srgb, var(--s-primary) 35%, transparent)"></rect>
        <rect x="${x}" y="${yt}" width="${Math.max(1, bw - 1)}" height="${Math.max(0, yc - yt)}" style="fill:var(--s-primary)"></rect></g>`;
    });
    let marks = '';
    m.steps.filter((s) => s.kind === 'checkpoint').forEach((c) => {
      const j = pts.findIndex((p) => p.i > c.i);
      const x = P + (j < 0 ? pts.length : j) * bw;
      marks += `<line x1="${x}" x2="${x}" y1="8" y2="${H - 18}" style="stroke:var(--c-checkpoint);stroke-dasharray:3 3"><title>Compaction at #${c.i}</title></line>`;
    });
    const ticks = [0, 0.5, 1].map((f) => `<text x="2" y="${y(peak * f) + 3}" style="fill:var(--s-muted);font-size:9px">${fmtNum(Math.round(peak * f))}</text>
      <line x1="${P}" x2="${W}" y1="${y(peak * f)}" y2="${y(peak * f)}" style="stroke:var(--s-border);stroke-width:.5"></line>`).join('');
    const outMax = Math.max(...pts.map((s) => s.tokens.out), 1);
    const outLine = pts.map((s, j) => `${P + j * bw + bw / 2},${H - 18 - (40 * s.tokens.out) / outMax}`).join(' ');
    const ratio = st.input_tokens + st.cache_read_tokens
      ? Math.round((100 * st.cache_read_tokens) / (st.input_tokens + st.cache_read_tokens)) : 0;
    el.innerHTML = `<div class="card"><div class="kv">
        <span>Peak context <b>${fmtNum(peak)}</b></span>
        <span>Fresh input <b>${fmtNum(st.input_tokens)}</b></span>
        <span>Cache read <b>${fmtNum(st.cache_read_tokens)}</b> (${ratio}%)</span>
        <span>Output <b>${fmtNum(st.output_tokens)}</b></span>
        <span>Compactions <b>${st.compactions}</b></span></div></div>
      <div class="card"><div class="meta">Context per model call (dark = fresh input, light = cache read; line = output; dashed = compaction)</div>
      <svg width="${W}" height="${H}">${ticks}${bars}${marks}
        <polyline points="${outLine}" style="fill:none;stroke:var(--c-delegate);stroke-width:1.2"></polyline></svg></div>`;
    el.querySelectorAll('.ctx-bar').forEach((g) =>
      g.addEventListener('click', () => select({ type: 'step', i: +g.dataset.i })));
  }

  // ------------------------------------------------------------------ subagents
  async function renderSubagents() {
    const el = $('#view-subagents');
    if (!S.model.subagents.length) {
      el.innerHTML = '<div class="empty">No subagents spawned in this conversation.</div>';
      return;
    }
    if (!S.subtree) {
      el.innerHTML = '<div class="empty">Loading subagent tree…</div>';
      try { S.subtree = await api('/api/subagents', { id: S.convId }); } catch (err) {
        el.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return;
      }
      if (S.tab !== 'subagents') return;
    }
    const nodeHtml = (n) => {
      const st = n.stats || {};
      const inTokens = (st.input_tokens || 0) + (st.cache_read_tokens || 0);
      const inPart = inTokens > 0 ? ` · ${fmtNum(inTokens)} in` : '';
      const card = `<div class="card">
          <div><b>${esc(n.role === 'root' ? 'Conductor' : (n.role || 'subagent'))}</b> — ${esc(n.title || n.id)}</div>
          <div class="meta">${n.missing ? 'transcript missing' : `${st.steps} steps · ${st.tool_calls} tools · ${st.errors} errors · ${fmtDur(st.duration_s)}${inPart}`}</div>
          ${n.missing || n.id === S.convId ? '' : `<div class="row"><span class="link" data-open="${esc(n.id)}">Open trajectory</span><span class="link" data-switch="${esc(n.id)}">Switch chat ↗</span></div>`}
        </div>`;
      const kids = (n.children || []).map(nodeHtml).join('');
      return `<li>${card}${kids ? `<ul>${kids}</ul>` : ''}</li>`;
    };
    el.innerHTML = `<div class="tree"><ul>${nodeHtml(S.subtree)}</ul></div>`;
    el.querySelectorAll('[data-open]').forEach((a) =>
      a.addEventListener('click', () => openConversation(a.dataset.open, { push: true })));
    el.querySelectorAll('[data-switch]').forEach((a) =>
      a.addEventListener('click', () => window.sidecar && window.sidecar.ui.toggleConversation(a.dataset.switch)));
  }

  // ------------------------------------------------------------------ notes
  function renderNotes() {
    const el = $('#view-notes');
    const notes = S.annotations || {};
    const traj = notes._ || [];
    const stepKeys = Object.keys(notes).filter((k) => k !== '_').sort((a, b) => +a - +b);
    el.innerHTML = `<div class="card"><div class="meta">Trajectory notes</div>
        ${traj.map((n, j) => noteHtml('_', n, j)).join('') || '<div class="meta">None yet.</div>'}
        <textarea id="traj-note" placeholder="Add a note about this whole trajectory…"></textarea>
        <div class="row"><button class="primary" id="traj-note-add">Add note</button></div></div>
      <div class="card"><div class="meta">Step notes</div>
        ${stepKeys.map((k) => notes[k].map((n, j) => `<div><span class="link" data-step="${k}">#${k}</span> ${noteHtml(k, n, j)}</div>`).join('')).join('') || '<div class="meta">Select a step and annotate it from the inspector.</div>'}
      </div>`;
    $('#traj-note-add').addEventListener('click', async () => {
      await addNote('_', $('#traj-note').value);
    });
    bindNoteActions(el);
    el.querySelectorAll('[data-step]').forEach((a) =>
      a.addEventListener('click', () => select({ type: 'step', i: +a.dataset.step })));
  }
  function noteHtml(step, n, j) {
    return `<div class="note"><div class="text">${esc(n.note)}<div class="when">${esc(relTime(n.created_at))}</div></div>
      <button class="icon-btn" data-del-step="${esc(step)}" data-del-idx="${j}" title="Delete">✕</button></div>`;
  }
  function bindNoteActions(root) {
    root.querySelectorAll('[data-del-step]').forEach((b) => b.addEventListener('click', async () => {
      try {
        const res = await post('/api/annotations', { id: S.convId, step: b.dataset.delStep, action: 'delete', index: +b.dataset.delIdx });
        S.annotations = res.annotations || {};
        renderAfterNotes();
      } catch (err) { toast(err.message); }
    }));
  }
  async function addNote(step, text) {
    if (!text.trim()) return;
    try {
      const res = await post('/api/annotations', { id: S.convId, step, note: text });
      S.annotations = res.annotations || {};
      renderAfterNotes();
      toast('Note saved');
    } catch (err) { toast(`Could not save note: ${err.message}`); }
  }
  function renderAfterNotes() {
    if (S.tab === 'notes') renderNotes();
    if (S.selected) renderInspector();
  }

  // ------------------------------------------------------------------ inspector
  function select(sel) {
    S.selected = sel;
    S.autoTail = false;
    $('#inspector').hidden = false;
    renderInspector();
    if (S.tab === 'graph') {
      renderGraph();
      const n = S.layout && S.layout.nodes.find((x) => isSelectedNode(x));
      if (n) centerOn(n);
    } else if (S.tab === 'timeline') {
      $$('.timeline-row').forEach((r) => r.classList.toggle('selected', sel.type === 'step' && +r.dataset.i === sel.i));
      const row = document.querySelector(`.timeline-row[data-i="${sel.i}"]`);
      if (row) row.scrollIntoView({ block: 'nearest' });
    }
  }
  function closeInspector() {
    S.selected = null;
    $('#inspector').hidden = true;
    if (S.tab === 'graph' && S.model) renderGraph();
  }
  function navigable() {
    return S.model.steps.filter((s) => s.kind !== 'system').map((s) => s.i);
  }
  function stepNav(delta) {
    if (!S.model) return;
    const list = navigable();
    let pos = 0;
    if (S.selected && S.selected.type === 'step') pos = list.indexOf(S.selected.i) + delta;
    else if (S.selected && S.selected.type === 'group') {
      const g = groupFor(S.selected);
      pos = g ? list.indexOf(delta > 0 ? g.steps[0] : g.steps[g.steps.length - 1]) : 0;
    } else pos = delta > 0 ? 0 : list.length - 1;
    pos = Math.max(0, Math.min(list.length - 1, pos));
    if (list[pos] != null) select({ type: 'step', i: list[pos] });
  }
  function groupFor(sel) {
    if (!S.model) return null;
    if (sel.kind === 'phase') return S.model.phases[sel.index];
    if (sel.kind === 'turn') return S.model.turns[sel.index];
    return null;
  }

  async function renderInspector() {
    const sel = S.selected;
    if (!sel || !S.model) return;
    const body = $('#insp-body');
    if (sel.type === 'group') {
      const g = groupFor(sel);
      if (!g) { closeInspector(); return; }
      const title = sel.kind === 'turn' ? `Turn ${sel.index + 1}` : `Phase · ${g.category}`;
      $('#insp-title').textContent = `${title}: ${(g.label || g.request || '').split('\n')[0]}`;
      const rows = g.steps.map(stepByIndex).filter((s) => s && s.kind !== 'system');
      body.innerHTML = `<div class="kv"><span>Steps <b>${rows.length}</b></span><span>Tools <b>${g.tool_calls}</b></span>
          <span>Errors <b>${g.errors}</b></span><span>Duration <b>${fmtDur(g.duration_s)}</b></span></div>
        ${g.request ? `<h4>Request</h4><pre>${esc(g.request)}</pre>` : ''}
        <h4>Steps</h4>${rows.map((s) => `<div class="note"><span class="chip kind-${s.kind}">${s.kind}</span>
          <span class="link text" data-step="${s.i}">#${s.i} ${esc(s.kind === 'tool' ? (s.tool_name || '') + ': ' : '')}${esc(s.label)}</span>
          <span class="when">${fmtDur(s.duration_s != null ? s.duration_s : s.gap_s)}</span></div>`).join('')}`;
      body.querySelectorAll('[data-step]').forEach((a) =>
        a.addEventListener('click', () => select({ type: 'step', i: +a.dataset.step })));
      return;
    }
    const s = stepByIndex(sel.i);
    if (!s) { closeInspector(); return; }
    $('#insp-title').innerHTML = `<span class="chip kind-${s.kind}">${s.kind}</span> #${s.i} · ${esc(s.kind === 'tool' ? s.tool_name : s.label)}`;
    const key = `${S.convId}:${s.i}:${S.version}`;
    let detail = S.details.get(key);
    const head = inspectorHead(s);
    if (!detail) {
      body.innerHTML = `${head}<div class="meta">Loading full step…</div>`;
      try {
        detail = await api('/api/step', { id: S.convId, i: s.i });
        S.details.set(key, detail);
      } catch (err) {
        body.innerHTML = `${head}<pre class="err">${esc(err.message)}</pre>`;
        return;
      }
      if (!S.selected || S.selected.type !== 'step' || S.selected.i !== s.i) return;
    }
    body.innerHTML = head + inspectorDetail(s, detail) + inspectorActions(s);
    wireInspector(s, detail);
  }

  function inspectorHead(s) {
    const t = s.tokens;
    const parent = s.result_of ? stepByIndex(+s.result_of.split('.')[0]) : null;
    return `<div class="kv">
      <span>Status <b>${esc(s.status || '—')}</b></span>
      <span>At <b>${esc((s.created_at || '').replace('T', ' ').replace('Z', ''))}</b></span>
      <span>Since prev <b>${fmtDur(s.gap_s)}</b></span>
      ${s.duration_s != null ? `<span>Tool time <b>${fmtDur(s.duration_s)}</b></span>` : ''}
      ${t ? `<span>Tokens <b>${fmtNum(t.in)}</b> in · <b>${fmtNum(t.cached)}</b> cached · <b>${fmtNum(t.out)}</b> out</span>` : ''}
      ${s.turn != null ? `<span>Turn <b>${s.turn + 1}</b></span>` : ''}
      ${parent ? `<span>Called by <span class="link" data-step="${parent.i}">#${parent.i}</span></span>` : ''}
    </div>`;
  }

  function pretty(v) {
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
  }

  function inspectorDetail(s, d) {
    let html = '';
    if (d.error) html += `<h4>Error</h4><pre class="err">${esc(d.error)}</pre>`;
    if (d.thinking) html += `<details ${s.kind === 'model' ? 'open' : ''}><summary>Thinking</summary><pre>${esc(d.thinking)}</pre></details>`;
    if (d.tool_calls && d.tool_calls.length) {
      html += '<h4>Tool calls</h4>';
      d.tool_calls.forEach((c, j) => {
        const meta = (s.tool_calls || [])[j] || {};
        const res = meta.result_step != null ? `<span class="link" data-step="${meta.result_step}">result #${meta.result_step}</span>` : '<span class="meta">no result yet</span>';
        html += `<details ${d.tool_calls.length <= 3 ? 'open' : ''}><summary><b>${esc(c.name)}</b> · ${esc(meta.summary || '')} · ${fmtDur(meta.duration_s)} · ${res}</summary><pre>${esc(pretty(c.args))}</pre></details>`;
      });
    }
    if (d.content) {
      const label = s.kind === 'tool' ? 'Result' : s.kind === 'user' ? 'Request' : s.kind === 'checkpoint' ? 'Compaction summary' : 'Content';
      html += `<h4>${label}${d.source_file === 'transcript_full.jsonl' ? '' : ' (may be truncated)'}</h4><pre>${esc(d.content)}</pre>`;
    }
    const kids = s.children || [];
    if (kids.length) {
      html += '<h4>Subagents spawned</h4>' + kids.map((id) => {
        const sub = S.model.subagents.find((x) => x.id === id) || { id };
        return `<div class="note"><span class="text">${esc(sub.title || sub.role || id)}</span>${sub.exists ? `<span class="link" data-open="${esc(id)}">Open</span>` : '<span class="meta">missing</span>'}</div>`;
      }).join('');
    }
    if (d.media && d.media.length) {
      html += `<h4>Media</h4>${d.media.map((m) => `<div class="meta">${esc(m.mime_type)} · ${esc(m.uri)}</div>`).join('')}`;
    }
    const notes = (S.annotations || {})[String(s.i)] || [];
    html += `<h4>Notes</h4>${notes.map((n, j) => noteHtml(String(s.i), n, j)).join('')}
      <textarea id="step-note" placeholder="Annotate this step…"></textarea>
      <div class="row"><button id="step-note-add">Add note</button></div>`;
    return html;
  }

  function inspectorActions(s) {
    const canAct = !!(window.sidecar && window.sidecar.embedded && S.config.agentapi);
    return `<h4>Steer · Fork</h4>
      <textarea id="guidance" placeholder="Guidance, e.g. 'Stop retrying the build; check the BUILD deps first.'"></textarea>
      <div class="row">
        <button class="primary" id="steer" ${canAct ? '' : 'disabled'} title="Send guidance into this conversation, anchored at step #${s.i}">Steer from #${s.i}</button>
        <button id="fork" ${canAct ? '' : 'disabled'} title="Start a new conversation with a handoff of everything up to step #${s.i}">Fork at #${s.i}</button>
        <button id="copy-fork" title="Copy the fork handoff prompt">Copy fork prompt</button>
        <button id="copy-json" title="Copy this step as JSON">Copy JSON</button>
      </div>
      ${canAct ? '' : '<div class="meta">Steer and Fork need the host bridge and the agentapi CLI. Use “Copy fork prompt” instead.</div>'}`;
  }

  function wireInspector(s, d) {
    const body = $('#insp-body');
    body.querySelectorAll('[data-step]').forEach((a) =>
      a.addEventListener('click', () => select({ type: 'step', i: +a.dataset.step })));
    body.querySelectorAll('[data-open]').forEach((a) =>
      a.addEventListener('click', () => openConversation(a.dataset.open, { push: true })));
    bindNoteActions(body);
    $('#step-note-add').addEventListener('click', () => addNote(String(s.i), $('#step-note').value));
    const guidance = () => $('#guidance').value.trim();
    const steer = $('#steer');
    if (steer) steer.addEventListener('click', async () => {
      if (!guidance()) { toast('Add guidance first'); return; }
      const msg = `[Sanjaya · steer from step #${s.i}]\n${guidance()}\n\nContext: step #${s.i} was ${s.kind} — ${s.label}.`;
      try { await window.sidecar.agent.sendMessage(msg, S.convId); toast('Guidance sent'); }
      catch (err) { toast(`Send failed: ${err.message}`); }
    });
    const forkPrompt = async () => (await post('/api/fork-prompt', { id: S.convId, step: s.i, guidance: guidance() })).prompt;
    const fork = $('#fork');
    if (fork) fork.addEventListener('click', async () => {
      try {
        const prompt = await forkPrompt();
        await window.sidecar.agent.startConversation(prompt, `Fork of ${S.model.title.slice(0, 40)} @#${s.i}`);
        toast('Fork started in a new conversation');
      } catch (err) { toast(`Fork failed: ${err.message}`); }
    });
    $('#copy-fork').addEventListener('click', async () => {
      try { toast((await copy(await forkPrompt())) ? 'Fork prompt copied' : 'Copy failed'); }
      catch (err) { toast(err.message); }
    });
    $('#copy-json').addEventListener('click', async () => {
      toast((await copy(JSON.stringify(d, null, 2))) ? 'Step JSON copied' : 'Copy failed');
    });
  }

  // ------------------------------------------------------------------ export
  async function exportMarkdown() {
    if (!S.convId) return;
    try {
      const md = await api('/api/export', { id: S.convId });
      const copied = await copy(md);
      try {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
        a.download = `trajectory-${S.convId.slice(0, 8)}.md`;
        document.body.appendChild(a); a.click(); a.remove();
      } catch (_) { /* downloads may be blocked inside the host iframe */ }
      toast(copied ? 'Markdown report copied (and downloaded if allowed)' : 'Report downloaded');
    } catch (err) { toast(`Export failed: ${err.message}`); }
  }

  // ------------------------------------------------------------------ wiring
  function switchTab(tab) {
    S.tab = tab;
    renderTab();
  }

  function wire() {
    $$('.tabs button').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    $$('#graph-controls button').forEach((b) => b.addEventListener('click', () => {
      S.mode = b.dataset.mode; S.autoTail = true; renderGraph();
    }));
    $('#conv-select').addEventListener('change', (ev) => {
      S.stack = [];
      S.follow = false; $('#follow').checked = false;
      openConversation(ev.target.value);
    });
    $('#follow').addEventListener('change', (ev) => {
      S.follow = ev.target.checked;
      if (S.follow) { S.stack = []; followTick(); }
    });
    $('#back').addEventListener('click', () => {
      const parent = S.stack.pop();
      if (parent) openConversation(parent);
    });
    let t = null;
    $('#search').addEventListener('input', (ev) => {
      clearTimeout(t);
      t = setTimeout(() => { S.filter = ev.target.value; renderTab(); }, 150);
    });
    $('#fit').addEventListener('click', () => { S.autoTail = false; fitView(); });
    $('#jump-latest').addEventListener('click', () => {
      S.autoTail = true;
      if (S.tab === 'graph') tailView(); else renderTab();
    });
    $('#export').addEventListener('click', exportMarkdown);
    $('#insp-close').addEventListener('click', closeInspector);
    $('#insp-prev').addEventListener('click', () => stepNav(-1));
    $('#insp-next').addEventListener('click', () => stepNav(1));
    document.addEventListener('keydown', (ev) => {
      if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
      if (ev.key === 'Escape') closeInspector();
      else if (ev.key === 'j' || ev.key === 'ArrowDown') { ev.preventDefault(); stepNav(1); }
      else if (ev.key === 'k' || ev.key === 'ArrowUp') { ev.preventDefault(); stepNav(-1); }
      else if (ev.key === '/') { ev.preventDefault(); $('#search').focus(); }
    });
    window.addEventListener('resize', () => { if (S.tab === 'graph' && S.autoTail) tailView(); });
    initPanZoom();
  }

  async function init() {
    wire();
    try { S.config = await api('/api/config'); } catch (_) { /* optional */ }
    await loadConversations();
    const initial = S.attachedId || (S.convs[0] && S.convs[0].id);
    if (initial) await openConversation(initial);
    else $('#stats').innerHTML = '<span>No conversations found. Set SANJAYA_APP_DIRS if your app data dir is non-standard.</span>';
    setInterval(() => { refresh(false); }, POLL_MS);
    setInterval(() => { followTick(); }, LIST_POLL_MS);
    setInterval(updateLive, 5000);
  }

  // Expose a tiny hook for debugging in the browser console.
  window.__sanjaya = S;
  init();
})();
