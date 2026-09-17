/* Timeline renderer for the standalone HTML export.
 *
 * Reads the session payload inlined by ``html_export.py`` and rebuilds the
 * same interactive timeline the web UI shows -- fold/unfold, kind filters,
 * Markdown rendering and a conclusions-only view -- with no network access. */
'use strict';

(function () {
  const DATA = JSON.parse(document.getElementById('cl-data').textContent);
  const EVENTS = DATA.events;

  const LABEL = {
    prompt: 'Prompt', assistant: 'Reply', thinking: 'Thinking', tool_use: 'Tool',
    tool_result: 'Result', image: 'Image', system: 'System',
  };
  const PROSE = new Set(['prompt', 'assistant', 'thinking', 'system']);

  const state = { md: true, conclusion: false, collapse: false, hide: new Set() };

  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];
  const esc = s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
         + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function eventHtml(ev) {
    const hidden = state.hide.has(ev.kind) ? ' hidden' : '';
    const collapsed = state.collapse ? ' collapsed' : '';
    const err = ev.meta && ev.meta.is_error ? ' err' : '';
    const label = LABEL[ev.kind] || ev.kind;

    let head = `<span class="kindtag k-${esc(ev.kind)}">${esc(label)}</span>`;
    if (ev.kind === 'tool_use' || ev.kind === 'tool_result') {
      head += `<span class="toolname">${esc(ev.tool || '?')}</span>`;
    }
    if (ev.title) head += `<span class="title">${esc(ev.title)}</span>`;
    head += `<span class="ts">${esc(fmtTime(ev.ts))}</span><span class="caret">▾</span>`;

    let body;
    if (ev.kind === 'image' && ev.img) {
      body = `<img src="${ev.img}" alt="image" loading="lazy">`;
    } else if (state.md && PROSE.has(ev.kind)) {
      body = `<div class="md">${mdToHtml(ev.text)}</div>`;
    } else {
      const cls = (ev.kind === 'tool_use' || ev.kind === 'tool_result')
        ? 'code' : 'prose';
      body = `<div class="${cls}">${esc(ev.text)}</div>`;
    }
    if (ev.truncated) {
      body += `<div class="trunc">… ${ev.chars - ev.text.length} more characters hidden`
            + ` (${ev.chars} total)</div>`;
    }

    return `<div class="ev k-${esc(ev.kind)}${err}${collapsed}${hidden}"`
         + ` data-seq="${ev.seq}"><div class="ev-head">${head}</div>`
         + `<div class="ev-body">${body}</div></div>`;
  }

  /* Conclusions only: keep prompts and replies, digest the tool calls */
  function timelineHtml() {
    if (!state.conclusion) return EVENTS.map(eventHtml).join('');

    const out = [];
    let turn = 0, tools = [];
    const flush = () => {
      if (!tools.length) return;
      const names = [...new Set(tools.map(t => t.tool || '?'))];
      out.push(`<div class="toolstrip">🔧 ${tools.length} tool calls — `
             + `${esc(names.join(', '))}</div>`);
      tools = [];
    };
    for (const ev of EVENTS) {
      if (ev.kind === 'prompt') {
        flush();
        out.push(`<div class="turnsep"><span>Turn ${++turn}</span></div>`);
        out.push(eventHtml(ev));
      } else if (ev.kind === 'assistant') {
        flush();
        out.push(eventHtml(ev));
      } else if (ev.kind === 'tool_use') {
        tools.push(ev);
      }
    }
    flush();
    return out.join('') || '<div class="empty">No prompts or replies</div>';
  }

  function render() {
    $('#timeline').innerHTML = timelineHtml();
    $$('#timeline .ev-head').forEach(h => {
      h.onclick = () => h.parentElement.classList.toggle('collapsed');
    });
  }

  /* ── controls ──────────────────────────────────────────── */

  $('#mdbtn').onclick = () => {
    state.md = !state.md;
    $('#mdbtn').classList.toggle('on', state.md);
    render();
  };

  $('#conclbtn').onclick = () => {
    state.conclusion = !state.conclusion;
    $('#conclbtn').classList.toggle('on', state.conclusion);
    $('#conclbtn').textContent = state.conclusion ? 'Show all' : 'Conclusions';
    render();
  };

  $('#foldbtn').onclick = () => {
    state.collapse = !state.collapse;
    $('#foldbtn').textContent = state.collapse ? 'Expand all' : 'Collapse all';
    $$('#timeline .ev').forEach(el => el.classList.toggle('collapsed', state.collapse));
  };

  $$('#filters input[type=checkbox]').forEach(c => c.onchange = () => {
    if (c.checked) state.hide.delete(c.value);
    else state.hide.add(c.value);
    $$('#timeline .ev').forEach(el => {
      el.hidden = state.hide.has(el.classList[1].slice(2));
    });
  });

  render();
})();
