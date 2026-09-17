# claudelog

Search, colour-code and read your past Claude Code sessions — locally, with no
dependencies beyond the Python standard library.

Claude Code writes every session to `~/.claude/projects/<project>/<id>.jsonl`.
Those files are complete but unreadable: a single session can be 288 MB of
interleaved prompts, replies, thinking blocks, tool calls and tool results.
claudelog indexes them into SQLite and gives you a web UI for reading what
actually happened.

```
claudelog index     # build/refresh the index
claudelog serve     # http://127.0.0.1:8787
```

## What it gives you

**Everything is colour-coded by kind.** Prompts, replies, thinking, tool calls,
tool results and images each get their own hue, used consistently across the
search chips, the timeline gutter and the event border. You can see the shape of
a session at a glance.

**Tool output folds away.** Click any block header to collapse it, or collapse
the whole timeline at once. Tool results are capped at 4,000 characters by
default with a *Show all* button per block — a 288 MB transcript still opens
instantly.

**A conclusions-only view.** One click drops tool traffic and shows just the
prompts and replies, grouped into turns, with each turn's tool calls reduced to
a one-line digest (`🔧 20 tool calls — Bash, Read, Edit`). This is the view for
"what was decided, and in what order" without the noise.

**Markdown is rendered.** Prose blocks are rendered as Markdown — headings,
lists, tables, code fences, links. Toggle it off to see the raw text.

**Search that understands the structure.** SQLite FTS5 full-text search with
AND/OR modes, project filtering, and kind filtering, so you can search only
prompts, or only tool results. Results link straight to the matching block in
the timeline.

**Standalone HTML export.** *Download HTML* produces a single self-contained
`.html` file — styles, Markdown renderer and data all inlined, no external
requests. Folding, the Markdown toggle and the conclusions-only view still work
in the exported file, so it can be mailed to someone or archived and read
offline.

## Install

No dependencies. Python 3.10+.

```bash
git clone https://github.com/shi3z/agent_logviewer
cd agent_logviewer
pip install -e .        # or just run: python3 -m claudelog.cli
```

## Usage

```bash
claudelog index                  # incremental — only changed files are re-parsed
claudelog serve                  # web UI on 127.0.0.1:8787
claudelog serve --tailnet        # bind to your Tailscale IP (see below)
claudelog search "FTS5"          # full-text search from the terminal
claudelog show <session>         # print a timeline
claudelog export <session>       # prompts + conclusions as Markdown
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--db PATH` | index database (default `~/.cache/claudelog/claudelog.db`) |
| `--root PATH` | transcript root (default `~/.claude/projects`) |
| `--rebuild` | rebuild the index from scratch |
| `--kind prompt` | restrict a search to one event kind (repeatable) |
| `--mode or` | OR instead of AND between search terms |

## Reading it from another device

`claudelog serve --tailnet` binds to your Tailscale IP rather than `0.0.0.0`.
Tailscale's firewall chain already accepts everything arriving on
`tailscale0`, so nothing else needs configuring, and the UI is reachable from
any device on your tailnet at `http://<host>.<tailnet>.ts.net:8787` — but not
from the public internet.

## How it works

```
claudelog/
  parser.py       JSONL -> flat event stream (prompt/reply/thinking/tool_use/...)
  index.py        SQLite + FTS5 index, incremental, images decoded to disk
  server.py       stdlib HTTP server and the JSON API
  html_export.py  self-contained single-file HTML export
  cli.py          index | serve | search | show | export
  static/         the web UI (vanilla JS, no build step)
```

Two details worth knowing:

**Not every `type: "user"` record is a prompt.** Background-task completions,
hook output and SDK traffic arrive as user turns too. The parser reads
`origin.kind` and `promptSource` to tell them apart, so a finished background
job never shows up as something you asked for.

**One SQLite connection per thread.** `ThreadingHTTPServer` handles each request
on its own thread, and a sqlite3 connection may only be used by the thread that
created it.

## Licence

MIT
