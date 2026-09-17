"""Standalone, self-contained HTML export of one session.

The result is a single ``.html`` file with no external references: the
stylesheet, the Markdown renderer and the timeline data are all inlined, so
it opens offline, survives being mailed around, and every block can still be
folded open or shut.

The timeline is rendered in the browser from an inlined JSON payload rather
than baked into markup -- that is what keeps fold/unfold, the Markdown toggle
and the "conclusions only" view alive in the exported file.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Iterable

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: per-event text cap; a 288 MB transcript must not become a 288 MB HTML file
DEFAULT_LIMIT = 4000

#: images are inlined as data URIs, but only up to this much in total
DEFAULT_IMAGE_BUDGET = 8 * 1024 * 1024

#: layout the app stylesheet does not provide (it styles the app shell)
_LAYOUT_CSS = """
body { margin: 0; background: var(--bg); color: var(--fg);
        font-family: var(--sans); font-size: 14px; line-height: 1.6; }
header { position: sticky; top: 0; z-index: 10; background: rgba(14,17,22,.94);
         backdrop-filter: blur(8px); border-bottom: 1px solid var(--border);
         padding: 12px 20px; }
header h1 { margin: 0 0 6px; font-size: 16px; font-weight: 600; }
.meta { font-family: var(--mono); font-size: 11.5px; color: var(--fg-dim);
        display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
.bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.bar .spacer { flex: 1; }
main { max-width: 1100px; margin: 0 auto; padding: 18px 20px 60px; }
.timeline { display: flex; flex-direction: column; gap: 8px; }
.ev[hidden] { display: none; }
.kindtag { font-size: 11px; font-weight: 600; padding: 1px 7px;
           border-radius: 4px; background: rgba(255,255,255,.06); }
.kindtag.k-prompt { color: var(--k-prompt); }
.kindtag.k-assistant { color: var(--k-assistant); }
.kindtag.k-thinking { color: var(--k-thinking); }
.kindtag.k-tool_use { color: var(--k-tool_use); }
.kindtag.k-tool_result { color: var(--k-tool_result); }
.kindtag.k-image { color: var(--k-image); }
.kindtag.k-system { color: var(--fg-dim); }
.ev.k-system { border-left-color: var(--fg-faint); }
.ev.k-system .ev-body { color: var(--fg-dim); }
.chip.k-system { color: var(--fg-dim); }
.turnsep { display: flex; align-items: center; gap: 10px; margin: 18px 0 2px;
           color: var(--fg-faint); font-family: var(--mono); font-size: 11px;
           letter-spacing: .08em; }
.turnsep::before, .turnsep::after { content: ''; height: 1px;
           background: var(--border-hi); flex: 1; }
.toolstrip { font-family: var(--mono); font-size: 11.5px; color: var(--fg-faint);
             padding: 3px 12px; border-left: 3px solid var(--k-tool_use);
             background: rgba(255,255,255,.012); border-radius: 0 6px 6px 0;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { color: var(--fg-faint); padding: 40px 0; text-align: center; }
@media (max-width: 720px) {
  .ev-head .title { max-width: 100%; }
  main { padding: 14px 12px 40px; }
}
"""


def _read_static(name: str) -> str:
    with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _json_for_script(obj: Any) -> str:
    """JSON safe to embed inside a ``<script>`` block.

    Escaping ``<`` (not just ``</``) matters: a transcript containing the
    literal ``<!--`` puts the HTML parser into its escaped-text state, after
    which the closing ``</script>`` stops being recognised and the rest of
    the document is swallowed.  ``<`` is valid JSON and decodes back to
    ``<``, so the payload is unaffected.
    """
    return (json.dumps(obj, ensure_ascii=False, default=str)
            .replace("<", "\\u003c")
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029"))


def _html_escape(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _inline_images(events: Iterable[dict], image_dir: str, budget: int) -> int:
    """Rewrite image events to carry a data URI.  Returns bytes inlined."""
    used = 0
    for ev in events:
        if ev.get("kind") != "image":
            continue
        meta = ev.get("meta") or {}
        name = meta.get("file")
        if not name:
            continue
        path = os.path.join(image_dir, os.path.basename(name))
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if used + size > budget:
            ev["title"] = ((ev.get("title") or "") + " (skipped, over budget)").strip()
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        used += len(blob)
        mime = meta.get("media_type") or "image/png"
        ev["img"] = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
    return used


def _payload(events: list[dict], limit: int) -> list[dict]:
    """Trim each event's text and shape it for the browser."""
    out: list[dict] = []
    for ev in events:
        text = ev.get("text") or ""
        truncated = False
        if limit > 0 and len(text) > limit:
            text = text[:limit]
            truncated = True
        out.append({
            "seq": ev.get("seq"), "kind": ev.get("kind"),
            "ts": ev.get("ts") or "", "tool": ev.get("tool") or "",
            "title": ev.get("title") or "", "text": text,
            "truncated": truncated, "chars": len(ev.get("text") or ""),
            "meta": ev.get("meta") or {},
        })
    return out


def build_html(session: dict, events: list[dict], *, image_dir: str = "",
               limit: int = DEFAULT_LIMIT,
               image_budget: int = DEFAULT_IMAGE_BUDGET) -> str:
    """Render one session as a standalone HTML document."""
    payload = _payload(events, limit)
    if image_dir:
        _inline_images(payload, image_dir, image_budget)

    title = session.get("title") or session.get("id") or "claudelog"
    bits = [
        f"id {str(session.get('id') or '')[:8]}",
        session.get("cwd") or session.get("project") or "",
        f"{session.get('first_ts') or ''} → {session.get('last_ts') or ''}",
        f"{len(payload)} events",
        f"claude {session.get('version') or '?'}",
    ]
    meta_html = "".join(f"<span>{_html_escape(b)}</span>" for b in bits if b)

    kinds = ["prompt", "assistant", "thinking", "tool_use", "tool_result",
             "image", "system"]
    labels = {"prompt": "Prompt", "assistant": "Reply", "thinking": "Thinking",
              "tool_use": "Tool calls", "tool_result": "Tool results",
              "image": "Images", "system": "System"}
    present = {ev["kind"] for ev in payload}
    chips = "".join(
        f'<label class="chip k-{k}"><input type="checkbox" value="{k}" checked>'
        f' {labels[k]}</label>' for k in kinds if k in present)

    data = {"session": {k: session.get(k) for k in
                        ("id", "title", "cwd", "project", "first_ts", "last_ts",
                         "version", "models")},
            "events": payload}

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)} — claudelog</title>
<style>
{_read_static('style.css')}
{_LAYOUT_CSS}
</style>
</head>
<body>

<header>
  <h1>{_html_escape(title)}</h1>
  <div class="meta">{meta_html}</div>
  <div class="bar">
    <button id="foldbtn">Collapse all</button>
    <button id="conclbtn">Conclusions</button>
    <button id="mdbtn" class="on">Markdown</button>
    <span class="spacer"></span>
    <span id="filters">{chips}</span>
  </div>
</header>

<main><div id="timeline" class="timeline"></div></main>

<script type="application/json" id="cl-data">{_json_for_script(data)}</script>
<script>
{_read_static('md.js')}
{_read_static('export.js')}
</script>
</body>
</html>
"""
