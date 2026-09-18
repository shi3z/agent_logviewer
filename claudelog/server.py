"""Zero-dependency HTTP server for the claudelog web UI.

Uses only ``http.server`` from the standard library so the tool runs anywhere
Python 3.10+ exists -- no pip install, no node build step.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, html_export
from .index import Index

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: how much of a tool result / thinking block the timeline ships to the browser
DEFAULT_LIMIT = 4000


class Api:
    """Thin query layer shared by the HTTP handler and the CLI."""

    def __init__(self, index: Index, root: str | None = None):
        self.index = index
        self.root = root or index.root
        self.lock = threading.Lock()
        self.last_index: dict[str, Any] = {}

    # -- helpers ----------------------------------------------------------- #

    def reindex(self, rebuild: bool = False) -> dict:
        with self.lock:
            stats = self.index.index(rebuild=rebuild)
        self.last_index = {
            "at": time.time(), "added": stats.added, "updated": stats.updated,
            "skipped": stats.skipped, "removed": stats.removed,
            "errors": stats.errors, "seconds": round(stats.seconds, 2),
            "sessions": stats.sessions,
        }
        return self.last_index

    # -- endpoints --------------------------------------------------------- #

    def search(self, q: dict) -> dict:
        query = (q.get("q") or [""])[0]
        mode = (q.get("mode") or ["and"])[0]
        project = (q.get("project") or [""])[0] or None
        kinds = [k for k in (q.get("kind") or []) if k]
        try:
            limit = min(int((q.get("limit") or ["50"])[0]), 200)
            offset = max(int((q.get("offset") or ["0"])[0]), 0)
        except ValueError:
            limit, offset = 50, 0
        if not query.strip():
            return {"query": query, "hits": [], "total": 0}
        hits = self.index.search(query, mode=mode, limit=limit, offset=offset,
                                 project=project, kinds=kinds)
        return {"query": query, "mode": mode, "hits": hits,
                "limit": limit, "offset": offset,
                "has_more": len(hits) == limit}

    def sessions(self, q: dict) -> dict:
        project = (q.get("project") or [""])[0] or None
        query = (q.get("q") or [""])[0] or None
        source = (q.get("source") or [""])[0] or None
        order = (q.get("order") or ["last_ts"])[0]
        try:
            limit = min(int((q.get("limit") or ["200"])[0]), 1000)
            offset = max(int((q.get("offset") or ["0"])[0]), 0)
        except ValueError:
            limit, offset = 200, 0
        rows = self.index.sessions(project=project, limit=limit, offset=offset,
                                   order=order, query=query, source=source)
        for r in rows:
            r["models"] = _loads(r.get("models"), [])
            r["tools"] = _loads(r.get("tools"), {})
        return {"sessions": rows, "limit": limit, "offset": offset,
                "has_more": len(rows) == limit}

    def session(self, sid: str, q: dict) -> dict | None:
        row = self.index.resolve_session(sid)
        if not row:
            return None
        try:
            limit = int((q.get("limit") or [str(DEFAULT_LIMIT)])[0])
        except ValueError:
            limit = DEFAULT_LIMIT
        events = self.index.events(row["id"])
        out = []
        for ev in events:
            text = ev.get("text") or ""
            truncated = False
            if limit > 0 and len(text) > limit:
                text = text[:limit]
                truncated = True
            out.append({
                "seq": ev["seq"], "kind": ev["kind"], "ts": ev["ts"],
                "tool": ev["tool"], "title": ev["title"], "text": text,
                "truncated": truncated, "meta": ev["meta"],
                "chars": len(ev.get("text") or ""),
            })
        row["models"] = _loads(row.get("models"), [])
        row["tools"] = _loads(row.get("tools"), {})
        return {"session": row, "events": out,
                "stats": _session_stats(out)}

    def projects(self, _q: dict) -> dict:
        return {"projects": self.index.projects()}

    def stats(self, _q: dict) -> dict:
        s = self.index.stats()
        s["timeline"] = self.index.timeline()
        s["root"] = self.root
        s["db"] = self.index.db_path
        s["version"] = __version__
        s["last_index"] = self.last_index
        return s

    def export(self, sid: str, q: dict) -> dict | None:
        """Human-readable extraction: prompts + assistant conclusions only."""
        row = self.index.resolve_session(sid)
        if not row:
            return None
        include_thinking = (q.get("thinking") or ["0"])[0] in ("1", "true", "yes")
        include_tools = (q.get("tools") or ["0"])[0] in ("1", "true", "yes")
        events = self.index.events(row["id"])
        turns: list[dict] = []
        for ev in events:
            kind = ev["kind"]
            if kind == "prompt":
                turns.append({"role": "user", "ts": ev["ts"], "text": ev["text"]})
            elif kind == "assistant":
                turns.append({"role": "assistant", "ts": ev["ts"],
                              "text": ev["text"]})
            elif kind == "thinking" and include_thinking:
                turns.append({"role": "thinking", "ts": ev["ts"],
                              "text": ev["text"]})
            elif kind == "tool_use" and include_tools:
                turns.append({"role": "tool", "ts": ev["ts"],
                              "text": f"{ev['tool']}: {ev['title'] or ''}".strip()})
        return {"session": row, "turns": turns}

    def html(self, sid: str, q: dict) -> tuple[str, str] | None:
        """Standalone HTML document for one session, plus a download name.

        ``limit=0`` inlines every event in full; the default caps each event
        so a huge transcript does not produce an unusable file.
        """
        row = self.index.resolve_session(sid)
        if not row:
            return None
        try:
            limit = int((q.get("limit") or [str(html_export.DEFAULT_LIMIT)])[0])
        except ValueError:
            limit = html_export.DEFAULT_LIMIT
        events = self.index.events(row["id"])
        doc = html_export.build_html(row, events,
                                     image_dir=self.index.image_dir, limit=limit)
        title = (row.get("title") or row.get("id") or "session").strip()
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title)[:80] or "session"
        return doc, f"{safe} ({str(row.get('id'))[:8]}).html"


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _session_stats(events: list[dict]) -> dict:
    counts: dict[str, int] = {}
    chars: dict[str, int] = {}
    for ev in events:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
        chars[ev["kind"]] = chars.get(ev["kind"], 0) + ev.get("chars", 0)
    return {"counts": counts, "chars": chars}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = f"claudelog/{__version__}"
    protocol_version = "HTTP/1.1"

    api: Api  # injected by serve()

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message, "status": code}, code)

    def _query(self) -> dict[str, list[str]]:
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        q = self._query()
        try:
            if path.startswith("/api/"):
                return self._api(path, q)
            if path.startswith("/images/"):
                return self._image(path[len("/images/"):])
            return self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"{type(exc).__name__}: {exc}")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/reindex":
            q = self._query()
            rebuild = (q.get("rebuild") or ["0"])[0] in ("1", "true", "yes")
            return self._json(self.api.reindex(rebuild=rebuild))
        return self._error(404, "not found")

    def _api(self, path: str, q: dict[str, list[str]]) -> None:
        api = self.api
        if path == "/api/search":
            return self._json(api.search(q))
        if path == "/api/sessions":
            return self._json(api.sessions(q))
        if path == "/api/projects":
            return self._json(api.projects(q))
        if path == "/api/stats":
            return self._json(api.stats(q))
        if path == "/api/reindex":
            return self._json(api.reindex(
                rebuild=(q.get("rebuild") or ["0"])[0] in ("1", "true", "yes")))
        m = re.fullmatch(r"/api/session/(.+)", path)
        if m:
            data = api.session(m.group(1), q)
            return self._json(data) if data else self._error(404, "no such session")
        m = re.fullmatch(r"/api/export/(.+)", path)
        if m:
            data = api.export(m.group(1), q)
            return self._json(data) if data else self._error(404, "no such session")
        m = re.fullmatch(r"/api/html/(.+)", path)
        if m:
            return self._html_download(m.group(1), q)
        return self._error(404, "unknown endpoint")

    def _html_download(self, sid: str, q: dict[str, list[str]]) -> None:
        """Serve one session as a standalone, self-contained HTML file."""
        doc = self.api.html(sid, q)
        if doc is None:
            return self._error(404, "no such session")
        body, filename = doc
        # RFC 5987 so non-ASCII session titles survive the round trip
        quoted = urllib.parse.quote(filename)
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Disposition":
                f"attachment; filename=\"claudelog.html\"; filename*=UTF-8''{quoted}",
        })

    def _image(self, name: str) -> None:
        name = posixpath.basename(name)
        path = os.path.join(self.api.index.image_dir, name)
        if not os.path.isfile(path):
            return self._error(404, "no such image")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype, {"Cache-Control": "public, max-age=86400"})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith("..") or os.path.isabs(rel):
            return self._error(403, "forbidden")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False


def serve(db: str, host: str = "127.0.0.1", port: int = 8787,
          root: str | None = None, verbose: bool = False) -> None:
    index = Index(db, root=root)
    api = Api(index, root=root)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = Server((host, port), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    print(f"claudelog {__version__}  →  {url}")
    print(f"  root : {api.root}")
    print(f"  db   : {index.db_path}")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
        index.close()
