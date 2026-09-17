"""SQLite + FTS5 index over Claude Code transcripts.

Design notes
------------
* **One copy of the text.**  ``events`` holds the full text; ``events_fts`` is
  an FTS5 *external content* table, so the search index stores only postings,
  not a second copy of every tool result.
* **Incremental.**  A session is re-parsed only when its ``(size, mtime)``
  changed.  ``--rebuild`` drops everything first.
* **Images** are decoded out of the JSONL into ``<db-dir>/images/`` and
  referenced by hash, so the web UI can serve them without re-reading 600 MB
  of transcripts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import parser

SCHEMA_VERSION = 4

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    project      TEXT,
    cwd          TEXT,
    git_branch   TEXT,
    version      TEXT,
    title        TEXT,
    slug         TEXT,
    first_ts     TEXT,
    last_ts      TEXT,
    n_user       INTEGER DEFAULT 0,
    n_assistant  INTEGER DEFAULT 0,
    n_tool       INTEGER DEFAULT 0,
    n_thinking   INTEGER DEFAULT 0,
    n_events     INTEGER DEFAULT 0,
    models       TEXT,
    tools        TEXT,
    size         INTEGER DEFAULT 0,
    mtime        REAL DEFAULT 0,
    indexed_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_last_ts ON sessions(last_ts DESC);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS sessions_path    ON sessions(path);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    uuid        TEXT,
    parent_uuid TEXT,
    kind        TEXT NOT NULL,
    ts          TEXT,
    tool        TEXT,
    title       TEXT,
    text        TEXT,
    meta        TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);

-- trigram, not unicode61: unicode61 cannot split CJK at all (a query for
-- a CJK query matches nothing), while trigram indexes CJK, English and code
-- with the same tokenizer.  The trade-off is that trigram needs >= 3
-- characters, so shorter queries fall back to LIKE (see ``_like_search``).
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    text,
    title,
    tool,
    content='events',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, text, title, tool)
    VALUES (new.id, new.text, new.title, new.tool);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, text, title, tool)
    VALUES ('delete', old.id, old.text, old.title, old.tool);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, text, title, tool)
    VALUES ('delete', old.id, old.text, old.title, old.tool);
    INSERT INTO events_fts(rowid, text, title, tool)
    VALUES (new.id, new.text, new.title, new.tool);
END;
"""


# --------------------------------------------------------------------------- #
# query building
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r'"[^"]*"|\S+')
#: trigram indexes nothing shorter than this, so such terms use LIKE instead.
TRIGRAM_MIN = 3


@dataclass
class Term:
    body: str
    negate: bool = False
    phrase: bool = False

    @property
    def searchable(self) -> bool:
        """True when the term is long enough for the trigram index."""
        return len(self.body) >= TRIGRAM_MIN


def parse_query(user_query: str) -> list[Term]:
    """Split a human query into terms.

    Bare words, ``"quoted phrases"`` and ``-exclusions`` are all supported.
    """
    terms: list[Term] = []
    for raw in _WORD_RE.findall(user_query or ""):
        negate = raw.startswith("-") and len(raw) > 1
        token = raw[1:] if negate else raw
        phrase = token.startswith('"') and token.endswith('"') and len(token) > 1
        body = token[1:-1] if phrase else token
        body = body.strip()
        if body:
            terms.append(Term(body=body, negate=negate, phrase=phrase))
    return terms


def build_match_query(user_query: str, mode: str = "and") -> str:
    """Turn a human query into a safe FTS5 MATCH expression.

    Everything is quoted, so FTS5 operators typed by the user cannot raise a
    syntax error.  Returns ``""`` when no term is long enough for trigram
    search — the caller then uses the LIKE fallback.
    """
    terms = [t for t in parse_query(user_query) if t.searchable]
    if not terms:
        return ""
    exprs = []
    for t in terms:
        body = t.body.replace('"', '""')
        expr = f'"{body}"' if t.phrase else f'"{body}"*'
        exprs.append(f"NOT {expr}" if t.negate else expr)
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(exprs)


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #

@dataclass
class IndexStats:
    sessions: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    errors: int = 0
    events: int = 0
    seconds: float = 0.0


class Index:
    def __init__(self, db_path: str, root: str | None = None):
        self.db_path = os.path.abspath(db_path)
        self.dir = os.path.dirname(self.db_path) or "."
        self.image_dir = os.path.join(self.dir, "images")
        self.root = root or parser.default_root()
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.image_dir, exist_ok=True)
        #: one connection *per thread*.  ``ThreadingHTTPServer`` handles every
        #: request on its own thread, and a sqlite3 connection may only be used
        #: by the thread that created it -- sharing one would 500 every request.
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
            (str(SCHEMA_VERSION),))
        self.conn.commit()

    # -- lifecycle --------------------------------------------------------- #

    @property
    def conn(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            # WAL is persisted in the db file, but the busy timeout is not.
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conn_lock:
                self._all_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._conn_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        self._local = threading.local()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def reset(self) -> None:
        for table in ("events_fts", "events", "sessions"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # -- indexing ---------------------------------------------------------- #

    def known(self) -> dict[str, tuple[int, float]]:
        rows = self.conn.execute("SELECT path, size, mtime FROM sessions").fetchall()
        return {r["path"]: (r["size"], r["mtime"]) for r in rows}

    def index(self, rebuild: bool = False, prune: bool = True,
              limit: int | None = None, progress=None) -> IndexStats:
        t0 = time.time()
        stats = IndexStats()
        if rebuild:
            self.reset()

        known = self.known()
        files = parser.discover(self.root)
        if limit:
            files = files[:limit]
        seen: set[str] = set()

        for i, (path, size, mtime) in enumerate(files, 1):
            seen.add(path)
            prev = known.get(path)
            if prev and prev[0] == size and abs(prev[1] - mtime) < 1e-6:
                stats.skipped += 1
                continue
            try:
                sess = parser.parse_session(path, self.root)
            except Exception as exc:  # pragma: no cover - defensive
                stats.errors += 1
                if progress:
                    progress(f"  ! {os.path.basename(path)}: {exc}")
                continue
            if prev:
                stats.updated += 1
            else:
                stats.added += 1
            self._store(sess)
            stats.events += len(sess.events)
            if progress and i % 25 == 0:
                progress(f"  indexed {i}/{len(files)} …")
        self.conn.commit()

        if prune:
            stale = set(known) - seen
            for path in stale:
                row = self.conn.execute(
                    "SELECT id FROM sessions WHERE path = ?", (path,)).fetchone()
                if row:
                    self._delete_session(row["id"])
                    stats.removed += 1
            self.conn.commit()

        stats.sessions = self.conn.execute(
            "SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        stats.seconds = time.time() - t0
        return stats

    def _delete_session(self, sid: str) -> None:
        self.conn.execute("DELETE FROM events WHERE session_id = ?", (sid,))
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    def _store(self, sess: parser.Session) -> None:
        self._delete_session(sess.id)
        self.conn.execute(
            """INSERT INTO sessions
               (id, path, project, cwd, git_branch, version, title, slug,
                first_ts, last_ts, n_user, n_assistant, n_tool, n_thinking,
                n_events, models, tools, size, mtime, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sess.id, sess.path, sess.project, sess.cwd, sess.git_branch,
             sess.version, sess.title, sess.slug, sess.first_ts, sess.last_ts,
             sess.n_user, sess.n_assistant, sess.n_tool, sess.n_thinking,
             len(sess.events), json.dumps(sess.models, ensure_ascii=False),
             json.dumps(sess.tools, ensure_ascii=False), sess.size, sess.mtime,
             time.time()))

        rows = []
        for seq, ev in enumerate(sess.events):
            meta = dict(ev.meta)
            text = ev.text
            if ev.kind == "image":
                meta = self._save_image(meta)
                text = "[image]"
            rows.append((sess.id, seq, ev.uuid, ev.parent_uuid, ev.kind, ev.ts,
                         ev.tool, ev.title, text,
                         json.dumps(meta, ensure_ascii=False) if meta else None))
        self.conn.executemany(
            """INSERT INTO events
               (session_id, seq, uuid, parent_uuid, kind, ts, tool, title, text, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)

    def _save_image(self, meta: dict) -> dict:
        data = meta.get("data")
        if not data:
            return {}
        try:
            blob = base64.b64decode(data, validate=False)
        except Exception:
            return {}
        digest = hashlib.sha1(blob).hexdigest()
        ext = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/gif": ".gif", "image/webp": ".webp"}.get(
            meta.get("media_type", ""), ".bin")
        name = digest + ext
        path = os.path.join(self.image_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(blob)
        return {"file": name, "media_type": meta.get("media_type", ""),
                "bytes": len(blob)}

    # -- queries ----------------------------------------------------------- #

    def search(self, query: str, mode: str = "and", limit: int = 50,
               offset: int = 0, project: str | None = None,
               kinds: Iterable[str] | None = None) -> list[dict]:
        """Full text search.

        Terms of 3+ characters go through the trigram FTS5 index (ranked by
        bm25); anything shorter — or a query made only of short words — falls
        back to a LIKE scan, because trigram cannot index 1-2 characters.
        """
        match = build_match_query(query, mode)
        if match:
            hits = self._fts_search(match, limit, offset, project, kinds)
            if hits or any(t.searchable for t in parse_query(query)):
                return hits
        return self._like_search(query, mode, limit, offset, project, kinds)

    def _fts_search(self, match: str, limit: int, offset: int,
                    project: str | None, kinds: Iterable[str] | None) -> list[dict]:
        where = ["events_fts MATCH ?"]
        params: list[Any] = [match]
        where, params = _add_filters(where, params, project, kinds)
        params.extend([limit, offset])
        sql = f"""
            SELECT e.id, e.session_id, e.seq, e.kind, e.tool, e.ts, e.title,
                   s.title AS session_title, s.project, s.cwd, s.last_ts,
                   snippet(events_fts, 0, '\x01', '\x02', ' … ', 24) AS snip,
                   bm25(events_fts, 10.0, 6.0, 3.0) AS rank
            FROM events_fts
            JOIN events e   ON e.id = events_fts.rowid
            JOIN sessions s ON s.id = e.session_id
            WHERE {' AND '.join(where)}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["snippet"] = _clean_snippet(d.pop("snip") or "")
            out.append(d)
        return out

    def _like_search(self, query: str, mode: str, limit: int, offset: int,
                     project: str | None, kinds: Iterable[str] | None) -> list[dict]:
        """Fallback for 1-2 character terms, which trigram cannot index."""
        terms = parse_query(query)
        if not terms:
            return []
        joiner = " OR " if mode == "or" else " AND "
        clauses = []
        params: list[Any] = []
        for t in terms:
            clause = "(COALESCE(e.text,'') LIKE ? ESCAPE '\\'"
            like = "%" + _like_escape(t.body) + "%"
            clause += " OR COALESCE(e.title,'') LIKE ? ESCAPE '\\')"
            params.extend([like, like])
            clauses.append(f"NOT {clause}" if t.negate else clause)
        where = [joiner.join(clauses)]
        where, params = _add_filters(where, params, project, kinds)
        params.extend([limit, offset])
        sql = f"""
            SELECT e.id, e.session_id, e.seq, e.kind, e.tool, e.ts, e.title,
                   s.title AS session_title, s.project, s.cwd, s.last_ts,
                   e.text AS snip, 0 AS rank
            FROM events e
            JOIN sessions s ON s.id = e.session_id
            WHERE {' AND '.join(where)}
            ORDER BY s.last_ts DESC, e.seq
            LIMIT ? OFFSET ?
        """
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["snippet"] = _clean_snippet(_excerpt(d.pop("snip") or "", terms))
            out.append(d)
        return out

    def sessions(self, project: str | None = None, limit: int = 200,
                 offset: int = 0, order: str = "last_ts",
                 query: str | None = None) -> list[dict]:
        cols = ("id, project, cwd, git_branch, version, title, slug, first_ts,"
                " last_ts, n_user, n_assistant, n_tool, n_thinking, n_events,"
                " models, tools, size")
        where, params = [], []
        if project:
            where.append("project = ?")
            params.append(project)
        if query:
            where.append("(title LIKE ? OR cwd LIKE ? OR id LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        sql = f"SELECT {cols} FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = {"last_ts": "last_ts DESC", "first_ts": "first_ts DESC",
                 "size": "size DESC", "events": "n_events DESC"}.get(
            order, "last_ts DESC")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def session(self, sid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None

    def resolve_session(self, sid: str) -> dict | None:
        """Accept a full id, a unique prefix, or a path."""
        row = self.session(sid)
        if row:
            return row
        base = os.path.basename(sid)
        if base.endswith(".jsonl"):
            base = base[:-6]
        row = self.session(base)
        if row:
            return row
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE id LIKE ? LIMIT 2",
            (base + "%",)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def events(self, sid: str, limit: int = 100000) -> list[dict]:
        rows = self.conn.execute(
            """SELECT seq, uuid, parent_uuid, kind, ts, tool, title, text, meta
               FROM events WHERE session_id = ? ORDER BY seq LIMIT ?""",
            (sid, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
            except Exception:
                d["meta"] = {}
            out.append(d)
        return out

    def projects(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT project, COUNT(*) n, MAX(last_ts) last_ts,
                      SUM(size) bytes, SUM(n_events) events
               FROM sessions GROUP BY project ORDER BY last_ts DESC""").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        s = self.conn.execute(
            """SELECT COUNT(*) sessions, COALESCE(SUM(n_events),0) events,
                      COALESCE(SUM(n_tool),0) tools, COALESCE(SUM(size),0) bytes,
                      MIN(first_ts) first, MAX(last_ts) last
               FROM sessions""").fetchone()
        ev = self.conn.execute(
            "SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        tools = self.conn.execute(
            """SELECT tool, COUNT(*) n FROM events
               WHERE kind='tool_use' AND tool IS NOT NULL
               GROUP BY tool ORDER BY n DESC LIMIT 30""").fetchall()
        models: dict[str, int] = {}
        for r in self.conn.execute("SELECT models FROM sessions"):
            try:
                for m in json.loads(r["models"] or "[]"):
                    models[m] = models.get(m, 0) + 1
            except Exception:
                pass
        return {
            "sessions": s["sessions"], "events": s["events"],
            "tools": s["tools"], "bytes": s["bytes"],
            "first": s["first"], "last": s["last"],
            "by_kind": [dict(r) for r in ev],
            "top_tools": [dict(r) for r in tools],
            "models": sorted(models.items(), key=lambda kv: -kv[1]),
            "db_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
        }

    def timeline(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT substr(last_ts,1,10) day, COUNT(*) n, SUM(n_events) events
               FROM sessions WHERE last_ts IS NOT NULL AND last_ts != ''
               GROUP BY day ORDER BY day""").fetchall()
        return [dict(r) for r in rows]


def _add_filters(where: list[str], params: list[Any],
                 project: str | None,
                 kinds: Iterable[str] | None) -> tuple[list[str], list[Any]]:
    """Append the shared ``project`` / ``kind`` predicates."""
    if project:
        where.append("s.project = ?")
        params.append(project)
    kinds = list(kinds or [])
    if kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)
    return where, params


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _excerpt(text: str, terms: list[Term], width: int = 240) -> str:
    """Build a snippet around the first matching term for the LIKE path."""
    low = text.lower()
    pos, length = -1, 0
    for t in terms:
        if t.negate:
            continue
        pos = low.find(t.body.lower())
        if pos >= 0:
            length = len(t.body)
            break
    if pos < 0:
        return text[:width]
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    prefix = "… " if start else ""
    suffix = " …" if end < len(text) else ""
    hit = text[pos:pos + length]
    return (f"{prefix}{text[start:pos]}\x01{hit}\x02"
            f"{text[pos + length:end]}{suffix}")


def _clean_snippet(s: str) -> str:
    """Turn FTS5 snippet markers into HTML-safe <mark> spans."""
    s = s.replace("\x01", "\x00MARK_OPEN\x00").replace("\x02", "\x00MARK_CLOSE\x00")
    s = _html_escape(s)
    return s.replace("\x00MARK_OPEN\x00", "<mark>").replace(
        "\x00MARK_CLOSE\x00", "</mark>")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
