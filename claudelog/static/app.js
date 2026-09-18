/* claudelog web UI — vanilla JS, no build step. */
'use strict';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const KIND_LABEL = {
  prompt: 'Prompt', assistant: 'Reply', thinking: 'Thinking',
  tool_use: 'Tool', tool_result: 'Result', image: 'Image',
};

const state = {
  view: 'search',
  hits: [], offset: 0, limit: 50, lastQuery: '',
  session: null, events: [], rawLimit: 4000,
  hideKinds: new Set(),
  collapse: false,
  conclusion: false,   // conclusions-only view: keep prompts and replies
  md: true,            // render prose as Markdown
};

/*: kinds rendered as Markdown (tool payloads stay raw text) */
const PROSE_KINDS = new Set(['prompt', 'assistant', 'thinking', 'system']);

/* ── helpers ─────────────────────────────────────────────── */

async function api(path, params) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (Array.isArray(v)) v.forEach(x => url.searchParams.append(k, x));
    else if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtDur(sec) {
  if (!sec) return '—';
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${(sec / 60).toFixed(1)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 1800);
}

/* ── routing ─────────────────────────────────────────────── */

function showView(name) {
  state.view = name;
  $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  if (name === 'sessions') loadSessions();
  if (name === 'stats') loadStats();
}

$$('.tabs button').forEach(b => b.onclick = () => {
  history.replaceState(null, '', '#');
  showView(b.dataset.view);
});
$('#brand').onclick = () => showView('search');

/* ── search ──────────────────────────────────────────────── */

$('#searchform').onsubmit = e => {
  e.preventDefault();
  showView('search');
  state.offset = 0;
  state.hits = [];
  runSearch();
};

$('#morebtn').onclick = () => {
  state.offset += state.limit;
  runSearch(true);
};

function selectedKinds() {
  return $$('#searchfilters input[type=checkbox]:checked').map(c => c.value);
}

async function runSearch(append = false) {
  const q = $('#q').value.trim();
  state.lastQuery = q;
  if (!q) {
    $('#results').innerHTML = '<div class="empty">Type a query to search</div>';
    $('#searchmeta').textContent = '';
    $('#morebtn').hidden = true;
    return;
  }
  $('#searchmeta').textContent = 'Searching…';
  let data;
  try {
    data = await api('/api/search', {
      q, mode: $('#mode').value, project: $('#project').value,
      kind: selectedKinds(), limit: state.limit, offset: state.offset,
    });
  } catch (err) {
    $('#searchmeta').textContent = `Error: ${err.message}`;
    return;
  }
  state.hits = append ? state.hits.concat(data.hits) : data.hits;
  renderHits();
  $('#searchmeta').textContent =
    `${state.hits.length} shown${data.has_more ? ' (more available)' : ''}`;
  $('#morebtn').hidden = !data.has_more;
}

function renderHits() {
  const root = $('#results');
  if (!state.hits.length) {
    root.innerHTML = '<div class="empty">No matches</div>';
    return;
  }
  root.innerHTML = state.hits.map(h => `
    <div class="hit k-${esc(h.kind)}" data-sid="${esc(h.session_id)}" data-seq="${h.seq}">
      <div class="hit-head">
        <span class="kindtag k-${esc(h.kind)}">${esc(KIND_LABEL[h.kind] || h.kind)}</span>
        ${h.tool ? `<span class="hit-title">${esc(h.tool)}</span>` : ''}
        <span class="hit-title">${esc(h.session_title || h.session_id.slice(0, 8))}</span>
        <span class="spacer"></span>
        <span>${esc(fmtTime(h.ts))}</span>
      </div>
      <div class="hit-snip">${h.snippet || ''}</div>
      <div class="hit-foot">${esc(h.cwd || h.project || '')}</div>
    </div>`).join('');

  $$('#results .hit').forEach(el => el.onclick = () => {
    openSession(el.dataset.sid, Number(el.dataset.seq));
  });
}

$$('#searchfilters input').forEach(c => c.onchange = () => runSearch());

/* ── session list ────────────────────────────────────────── */

let sessTimer;
$('#sessq').oninput = () => { clearTimeout(sessTimer); sessTimer = setTimeout(loadSessions, 220); };
$('#sessorder').onchange = loadSessions;

async function loadSessions() {
  const data = await api('/api/sessions', {
    q: $('#sessq').value, order: $('#sessorder').value,
    project: $('#project').value, limit: 500,
  });
  const root = $('#sesslist');
  if (!data.sessions.length) {
    root.innerHTML = '<div class="empty">No sessions yet — build the index first.</div>';
    return;
  }
  root.innerHTML = data.sessions.map(s => `
    <div class="sess" data-sid="${esc(s.id)}">
      <div class="sess-title"><span class="src src-${esc(s.source || 'claude')}">${esc(s.source || 'claude')}</span>${esc(s.title || '(untitled)')}</div>
      <div class="sess-nums">
        <span>prompts <b>${s.n_user}</b></span>
        <span>replies <b>${s.n_assistant}</b></span>
        <span>tools <b>${s.n_tool}</b></span>
        <span>${fmtBytes(s.size)}</span>
      </div>
      <div class="sess-sub">${esc(s.cwd || s.project || '')} · ${esc(fmtTime(s.last_ts))}</div>
    </div>`).join('');
  $$('#sesslist .sess').forEach(el => el.onclick = () => openSession(el.dataset.sid));
  $('#sessmeta').textContent = `${data.sessions.length} sessions`;
}

/* ── stats ───────────────────────────────────────────────── */

async function loadStats() {
  const s = await api('/api/stats');
  const maxDay = Math.max(1, ...s.timeline.map(d => d.events || 0));
  const kindTotal = s.by_kind.reduce((a, b) => a + b.n, 0) || 1;

  $('#stats').innerHTML = `
    <div class="card">
      <h3>Overview</h3>
      <div class="kv"><span>Sessions</span><span>${s.sessions}</span></div>
      <div class="kv"><span>Events</span><span>${s.events}</span></div>
      <div class="kv"><span>Tool calls</span><span>${s.tools}</span></div>
      <div class="kv"><span>Log size</span><span>${fmtBytes(s.bytes)}</span></div>
      <div class="kv"><span>Index size</span><span>${fmtBytes(s.db_bytes)}</span></div>
      <div class="kv"><span>Range</span><span>${esc((s.first || '').slice(0, 10))} → ${esc((s.last || '').slice(0, 10))}</span></div>
    </div>

    <div class="card">
      <h3>Event kinds</h3>
      ${s.by_kind.map(k => `
        <div class="kv"><span>${esc(KIND_LABEL[k.kind] || k.kind)}</span><span>${k.n}</span></div>
        <div class="bar"><i style="width:${(k.n / kindTotal * 100).toFixed(1)}%;
             background:var(--k-${esc(k.kind)}, var(--accent))"></i></div>`).join('')}
    </div>

    <div class="card">
      <h3>Top tools</h3>
      ${s.top_tools.slice(0, 12).map(t => `
        <div class="kv"><span>${esc(t.tool)}</span><span>${t.n}</span></div>`).join('')}
    </div>

    <div class="card">
      <h3>Models</h3>
      ${s.models.map(([m, n]) => `
        <div class="kv"><span>${esc(m)}</span><span>${n}</span></div>`).join('')}
    </div>

    <div class="card wide">
      <h3>Daily activity</h3>
      <div class="spark">
        ${s.timeline.map(d => `<div style="height:${Math.max(2, (d.events || 0) / maxDay * 100)}%"
             title="${esc(d.day)}: ${d.n} sessions / ${d.events} events"></div>`).join('')}
      </div>
      <div class="kv" style="margin-top:8px">
        <span class="muted">${esc(s.timeline[0]?.day || '')}</span>
        <span class="muted">${esc(s.timeline.at(-1)?.day || '')}</span>
      </div>
    </div>

    <div class="card wide">
      <h3>Index</h3>
      <div class="kv"><span>root</span><span>${esc(s.root)}</span></div>
      <div class="kv"><span>db</span><span>${esc(s.db)}</span></div>
      <div style="margin-top:10px">
        <button id="reindexbtn">Reindex (incremental)</button>
        <button id="rebuildbtn" class="ghost">Rebuild from scratch</button>
      </div>
    </div>`;

  $('#reindexbtn').onclick = () => doReindex(false);
  $('#rebuildbtn').onclick = () => doReindex(true);
}

async function doReindex(rebuild) {
  if (rebuild && !confirm('Rebuild the whole index from scratch?')) return;
  toast(rebuild ? 'Rebuilding…' : 'Reindexing…');
  try {
    const r = await fetch(`/api/reindex?rebuild=${rebuild ? 1 : 0}`, { method: 'POST' });
    const d = await r.json();
    toast(`Done: +${d.added} added, ${d.updated} updated, ${d.removed} removed (${d.seconds}s)`);
    loadStats();
    loadProjects();
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
}

/* ── session detail ──────────────────────────────────────── */

async function openSession(sid, focusSeq) {
  showView('detail');
  $('#timeline').innerHTML = '<div class="empty">Loading…</div>';
  let data;
  try {
    data = await api(`/api/session/${encodeURIComponent(sid)}`, { limit: state.rawLimit });
  } catch (e) {
    $('#timeline').innerHTML = `<div class="empty">Load failed: ${esc(e.message)}</div>`;
    return;
  }
  state.session = data.session;
  state.events = data.events;
  state.hideKinds = new Set();
  $$('#detailfilters input[type=checkbox]').forEach(c => {
    if (c.id !== 'collapse') c.checked = true;
  });
  history.replaceState(null, '', `#s=${encodeURIComponent(sid)}`);
  renderDetail(focusSeq);
}

function renderDetail(focusSeq) {
  const s = state.session;
  $('#dtitle').textContent = s.title || '(untitled)';
  $('#dmeta').innerHTML = [
    `<span class="src src-${esc(s.source || 'claude')}">${esc(s.source || 'claude')}</span>`,
    `id ${esc(s.id.slice(0, 8))}`,
    esc(s.cwd || s.project || ''),
    `${esc(fmtTime(s.first_ts))} → ${esc(fmtTime(s.last_ts))}`,
    fmtDur((new Date(s.last_ts) - new Date(s.first_ts)) / 1000),
    `${s.n_events} events`,
    (s.models || []).join(', '),
    `claude ${esc(s.version || '?')}`,
  ].filter(Boolean).map(x => `<span>${x}</span>`).join('');

  const root = $('#timeline');
  root.innerHTML = timelineHtml();
  bindEvents();

  if (focusSeq != null) {
    const el = root.querySelector(`.ev[data-seq="${focusSeq}"]`);
    if (el) {
      el.classList.remove('collapsed');
      el.scrollIntoView({ block: 'center' });
      el.style.outline = '2px solid var(--accent)';
      setTimeout(() => { el.style.outline = ''; }, 2200);
    }
  }
}

/* Conclusions only: keep prompts and replies, grouped into turns.
   Tool calls collapse to a one-line digest so the shape of the work survives. */
const CONCLUSION_KINDS = new Set(['prompt', 'assistant']);

function timelineHtml() {
  const events = state.events;
  if (!state.conclusion) return events.map(ev => eventHtml(ev)).join('');

  const out = [];
  let turn = 0;
  let tools = [];
  const flushTools = () => {
    if (!tools.length) return;
    const names = [...new Set(tools.map(t => t.tool || '?'))];
    out.push(`<div class="toolstrip">🔧 ${tools.length} tool calls — ${esc(names.join(', '))}</div>`);
    tools = [];
  };

  for (const ev of events) {
    if (ev.kind === 'prompt') {
      flushTools();
      turn++;
      out.push(`<div class="turnsep"><span>Turn ${turn}</span></div>`);
      out.push(eventHtml(ev));
    } else if (ev.kind === 'assistant') {
      flushTools();
      out.push(eventHtml(ev));
    } else if (ev.kind === 'tool_use') {
      tools.push(ev);
    }
  }
  flushTools();
  return out.join('') || '<div class="empty">No prompts or replies</div>';
}

function eventHtml(ev) {
  const hidden = state.hideKinds.has(ev.kind) ? ' hidden' : '';
  const collapsed = state.collapse ? ' collapsed' : '';
  const err = ev.meta && ev.meta.is_error ? ' err' : '';
  const label = KIND_LABEL[ev.kind] || ev.kind;

  let head = `<span class="kindtag k-${esc(ev.kind)}">${esc(label)}</span>`;
  if (ev.kind === 'tool_use' || ev.kind === 'tool_result') {
    head += `<span class="toolname">${esc(ev.tool || '?')}</span>`;
  }
  if (ev.title) head += `<span class="title">${esc(ev.title)}</span>`;
  head += `<span class="ts">${esc(fmtTime(ev.ts))}</span>`;
  head += `<span class="caret">▾</span>`;

  let body;
  if (ev.kind === 'image' && ev.meta && ev.meta.file) {
    body = `<img src="/images/${encodeURIComponent(ev.meta.file)}" alt="image" loading="lazy">`;
  } else if (state.md && PROSE_KINDS.has(ev.kind)) {
    body = `<div class="md">${mdToHtml(ev.text)}</div>`;
    if (ev.truncated) {
      body += `<div class="trunc">… ${ev.chars - ev.text.length} more characters hidden
        <button data-expand="${ev.seq}">Show all</button></div>`;
    }
  } else {
    const cls = (ev.kind === 'tool_use' || ev.kind === 'tool_result') ? 'code' : 'prose';
    body = `<div class="${cls}">${esc(ev.text)}</div>`;
    if (ev.truncated) {
      body += `<div class="trunc">… ${ev.chars - ev.text.length} more characters hidden
        <button data-expand="${ev.seq}">Show all</button></div>`;
    }
  }

  return `<div class="ev k-${esc(ev.kind)}${err}${collapsed}${hidden}" data-seq="${ev.seq}">
      <div class="ev-head">${head}</div>
      <div class="ev-body">${body}</div>
    </div>`;
}

function bindEvents() {
  $$('#timeline .ev-head').forEach(h => h.onclick = () => {
    h.parentElement.classList.toggle('collapsed');
  });
  $$('#timeline [data-expand]').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const seq = Number(b.dataset.expand);
    const ev = state.events.find(x => x.seq === seq);
    if (!ev) return;
    const full = await api(`/api/session/${encodeURIComponent(state.session.id)}`, { limit: 0 });
    const src = full.events.find(x => x.seq === seq);
    if (!src) return;
    ev.text = src.text;
    ev.truncated = false;
    const el = $(`#timeline .ev[data-seq="${seq}"]`);
    el.outerHTML = eventHtml(ev);
    bindEvents();
  });
}

$$('#detailfilters input[type=checkbox]').forEach(c => c.onchange = () => {
  if (c.id === 'collapse') {
    state.collapse = c.checked;
    $$('#timeline .ev').forEach(el => el.classList.toggle('collapsed', c.checked));
    return;
  }
  if (c.checked) state.hideKinds.delete(c.value);
  else state.hideKinds.add(c.value);
  $$('#timeline .ev').forEach(el => {
    el.hidden = state.hideKinds.has(el.classList[1].slice(2));
  });
});

$('#backbtn').onclick = () => {
  history.replaceState(null, '', '#');
  showView(state.session ? 'sessions' : 'search');
};

$('#rawbtn').onclick = () => {
  state.rawLimit = state.rawLimit === 0 ? 4000 : 0;
  $('#rawbtn').textContent = state.rawLimit === 0 ? 'Truncated' : 'Full text';
  if (state.session) openSession(state.session.id);
};

/* Download as a standalone HTML file -- folding and Markdown still work */
$('#htmlbtn').onclick = () => {
  if (!state.session) return;
  const url = `/api/html/${encodeURIComponent(state.session.id)}`
            + (state.rawLimit === 0 ? '?limit=0' : '');
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('Downloading HTML…');
};

$('#mdbtn').onclick = () => {
  state.md = !state.md;
  $('#mdbtn').classList.toggle('on', state.md);
  if (state.session) renderDetail();
};

$('#conclbtn').onclick = () => {
  state.conclusion = !state.conclusion;
  $('#conclbtn').classList.toggle('on', state.conclusion);
  $('#conclbtn').textContent = state.conclusion ? 'Show all' : 'Conclusions';
  if (state.session) renderDetail();
};

/* Markdown export: prompts + assistant text, thinking/tool optional */
$('#extractbtn').onclick = async () => {
  if (!state.session) return;
  const data = await api(`/api/export/${encodeURIComponent(state.session.id)}`);
  const md = toMarkdown(data);
  const w = window.open('', '_blank');
  w.document.write(`<meta charset="utf-8"><title>${esc(state.session.title || '')}</title>
    <pre style="white-space:pre-wrap;font:13px/1.6 ui-monospace,monospace;
    background:#0e1116;color:#d7dee8;padding:20px;margin:0">${esc(md)}</pre>`);
  w.document.close();
};

$('#copybtn').onclick = async () => {
  if (!state.session) return;
  const data = await api(`/api/export/${encodeURIComponent(state.session.id)}`);
  try {
    await navigator.clipboard.writeText(toMarkdown(data));
    toast('Markdown copied');
  } catch {
    toast('Copy failed');
  }
};

function toMarkdown(data) {
  const s = data.session;
  const out = [`# ${s.title || s.id}`, '',
    `- id: \`${s.id}\``, `- cwd: \`${s.cwd || s.project || ''}\``,
    `- range: ${fmtTime(s.first_ts)} → ${fmtTime(s.last_ts)}`, ''];
  for (const t of data.turns) {
    const who = { user: '## 👤 Prompt', assistant: '## 🤖 Reply',
                  thinking: '## 💭 Thinking', tool: '## 🔧 Tool' }[t.role] || `## ${t.role}`;
    out.push(who, '', t.text.trim(), '');
  }
  return out.join('\n');
}

/* ── projects dropdown ───────────────────────────────────── */

async function loadProjects() {
  const { projects } = await api('/api/projects');
  const sel = $('#project');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All projects</option>' +
    projects.map(p => `<option value="${esc(p.project)}">${esc(p.project)} (${p.n})</option>`).join('');
  sel.value = cur;
}
$('#project').onchange = () => {
  if (state.view === 'sessions') loadSessions();
  else if (state.lastQuery) { state.offset = 0; runSearch(); }
};

/* ── boot ────────────────────────────────────────────────── */

/* Follow #s=... changes within the same tab (no reload happens) */
window.addEventListener('hashchange', () => {
  const m = location.hash.match(/^#s=(.+)$/);
  if (m) {
    const sid = decodeURIComponent(m[1]);
    if (!state.session || state.session.id !== sid) openSession(sid);
  } else if (state.view === 'detail') {
    showView('sessions');
  }
});

(async function init() {
  try {
    const s = await api('/api/stats');
    $('#ver').textContent = 'v' + s.version;
  } catch { /* ignore */ }
  await loadProjects();

  const hash = location.hash;
  const m = hash.match(/^#s=(.+)$/);
  if (m) openSession(decodeURIComponent(m[1]));
  else {
    $('#results').innerHTML = '<div class="empty">Type a query to search</div>';
    $('#q').focus();
  }
})();
