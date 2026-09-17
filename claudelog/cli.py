"""Command line interface: ``claudelog index | serve | search | show | export``."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap

from . import __version__
from .index import Index
from .server import DEFAULT_LIMIT, serve

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".cache", "claudelog", "claudelog.db")


def tailnet_ip() -> str | None:
    """This host's Tailscale IPv4 address, or ``None`` if not on a tailnet.

    Binding to this address (rather than ``0.0.0.0``) publishes the UI to the
    tailnet only -- Tailscale's firewall chain accepts everything arriving on
    ``tailscale0``, so no iptables change is needed.
    """
    for cmd in (["tailscale", "ip", "-4"], ["/usr/bin/tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
            if ip:
                return ip
    return None


def _index(args) -> int:
    with Index(args.db, root=args.root) as ix:
        stats = ix.index(rebuild=args.rebuild, prune=not args.no_prune,
                         progress=lambda m: print(m, file=sys.stderr))
    print(f"Indexed {stats.sessions} sessions "
           f"(+{stats.added} added / {stats.updated} updated / {stats.skipped} unchanged "
           f"/ {stats.removed} removed / {stats.errors} errors) "
           f"— {stats.events} events, {stats.seconds:.1f}s")
    return 0


def _serve(args) -> int:
    host = args.host
    if args.tailnet:
        host = tailnet_ip() or host
        if host == args.host:
            print("Warning: could not determine the Tailscale IP; "
                  f"binding {args.host} instead.", file=sys.stderr)
    serve(args.db, host=host, port=args.port, root=args.root,
          verbose=args.verbose)
    return 0


def _search(args) -> int:
    with Index(args.db, root=args.root) as ix:
        hits = ix.search(args.query, mode=args.mode, limit=args.limit,
                         project=args.project, kinds=args.kind)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("No matches", file=sys.stderr)
        return 1
    for h in hits:
        snip = h["snippet"].replace("<mark>", "\033[43;30m").replace("</mark>", "\033[0m")
        snip = textwrap.shorten(snip, 300, placeholder=" …")
        print(f"\033[36m{h['session_id'][:8]}\033[0m "
              f"\033[2m{h['kind']:<12}\033[0m "
              f"\033[33m{(h['tool'] or ''):<10}\033[0m "
              f"\033[2m{(h['ts'] or '')[:19]}\033[0m")
        print(f"  {h['session_title'] or ''}")
        print(f"  {snip}\n")
    return 0


def _show(args) -> int:
    with Index(args.db, root=args.root) as ix:
        row = ix.resolve_session(args.session)
        if not row:
            print(f"No such session: {args.session}", file=sys.stderr)
            return 1
        events = ix.events(row["id"])
    print(f"# {row['title'] or row['id']}")
    print(f"# {row['cwd'] or row['project']}  {row['first_ts']} → {row['last_ts']}")
    for ev in events:
        if ev["kind"] in args.hide:
            continue
        text = ev["text"] or ""
        if args.limit and len(text) > args.limit:
            text = text[:args.limit] + f"\n… ({len(ev['text']) - args.limit} characters hidden)"
        head = f"[{ev['kind']}]"
        if ev["tool"]:
            head += f" {ev['tool']}"
        if ev["title"]:
            head += f" — {ev['title']}"
        print(f"\n\033[1m{head}\033[0m \033[2m{(ev['ts'] or '')[:19]}\033[0m")
        print(text)
    return 0


def _export(args) -> int:
    """Prompts + assistant conclusions only — the 'what was decided' view."""
    with Index(args.db, root=args.root) as ix:
        row = ix.resolve_session(args.session)
        if not row:
            print(f"No such session: {args.session}", file=sys.stderr)
            return 1
        events = ix.events(row["id"])

    keep = {"prompt", "assistant"}
    if args.thinking:
        keep.add("thinking")
    if args.tools:
        keep.add("tool_use")

    out = [f"# {row['title'] or row['id']}", "",
           f"- id: `{row['id']}`",
           f"- cwd: `{row['cwd'] or row['project']}`",
           f"- range: {row['first_ts']} → {row['last_ts']}", ""]
    for ev in events:
        if ev["kind"] not in keep:
            continue
        who = {"prompt": "## 👤 Prompt", "assistant": "## 🤖 Reply",
               "thinking": "## 💭 Thinking", "tool_use": "## 🔧 Tool"}[ev["kind"]]
        text = (ev["text"] or "").strip()
        if ev["kind"] == "tool_use":
            text = f"`{ev['tool']}` {ev['title'] or ''}"
        out += [who, "", text, ""]
    print("\n".join(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claudelog",
        description="Index, search and read past Claude Code transcripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              claudelog index                 # incremental index
              claudelog serve                 # web UI on http://127.0.0.1:8787
              claudelog search "FTS5"         # full-text search
              claudelog show <session>        # print the timeline
              claudelog export <session>      # prompts + conclusions as Markdown
            """))
    p.add_argument("-V", "--version", action="version",
                   version=f"claudelog {__version__}")
    p.add_argument("--db", default=os.environ.get("CLAUDELOG_DB", DEFAULT_DB),
                   help=f"index database path (default: {DEFAULT_DB})")
    p.add_argument("--root", default=None,
                   help="transcript root (default: ~/.claude/projects)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="index the transcripts")
    pi.add_argument("--rebuild", action="store_true", help="rebuild from scratch")
    pi.add_argument("--no-prune", action="store_true",
                    help="keep entries for files that disappeared")
    pi.set_defaults(func=_index)

    ps = sub.add_parser("serve", help="serve the web UI")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8787)
    ps.add_argument("--tailnet", action="store_true",
                    help="bind to the Tailscale IP and expose on the tailnet")
    ps.add_argument("-v", "--verbose", action="store_true")
    ps.set_defaults(func=_serve)

    pq = sub.add_parser("search", help="full-text search")
    pq.add_argument("query")
    pq.add_argument("--mode", choices=["and", "or"], default="and")
    pq.add_argument("--project", default=None)
    pq.add_argument("--kind", action="append",
                    choices=["prompt", "assistant", "thinking",
                             "tool_use", "tool_result"],
                    help="restrict to these event kinds (repeatable)")
    pq.add_argument("-n", "--limit", type=int, default=20)
    pq.add_argument("--json", action="store_true")
    pq.set_defaults(func=_search)

    ph = sub.add_parser("show", help="print one session")
    ph.add_argument("session", help="session id (prefix ok) or path")
    ph.add_argument("--hide", action="append", default=[],
                    choices=["thinking", "tool_use", "tool_result", "image"])
    ph.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ph.set_defaults(func=_show)

    pe = sub.add_parser("export", help="print prompts and conclusions as Markdown")
    pe.add_argument("session")
    pe.add_argument("--thinking", action="store_true", help="include thinking")
    pe.add_argument("--tools", action="store_true", help="include tool calls")
    pe.set_defaults(func=_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
