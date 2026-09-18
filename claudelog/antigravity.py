"""Parse antigravity-cli conversations into the shared event stream.

antigravity-cli keeps one SQLite database per conversation under
``~/.gemini/antigravity-cli/conversations/<uuid>.db``, plus a
``conversation_summaries.db`` holding each conversation's title and
workspace.  The transcript itself is ``steps.step_payload`` -- protobuf,
with no published ``.proto`` -- so this module walks the wire format
directly (see :func:`fields`) and reads the fields that carry text:

  step_type=14   user turn        f19.f2      prompt text
  step_type=15   model turn       f20.f3      thinking text
                                  f20.f1/.f8  assistant prose
                                  f20.f7      tool call (id / name / args)
  step_type=132  tool result      f5.f4       tool call id and name
                                  f140.f2.f1  output text
  step_type=101  system notice    f114        notice line

Every payload carries its timestamp at ``f5.f1`` (seconds + nanos).
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from typing import Any, Iterator

from .parser import Event, Session, _is_noise, _summarize_tool_input

STEP_USER = 14
STEP_MODEL = 15
STEP_SYSTEM = 101
STEP_RESULT = 132


# --------------------------------------------------------------------------- #
# protobuf wire format
# --------------------------------------------------------------------------- #

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    L = len(buf)
    while i < L:
        x = buf[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not (x & 0x80):
            return val, i
        shift += 7
    return val, i


def fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """Walk a protobuf binary buffer, yielding (field_number, wire_type, value)."""
    i = 0
    L = len(buf)
    res: list[tuple[int, int, Any]] = []
    while i < L:
        key, i = _varint(buf, i)
        fnum = key >> 3
        wtype = key & 7
        if wtype == 0:  # varint
            val, i = _varint(buf, i)
            res.append((fnum, wtype, val))
        elif wtype == 2:  # length-delimited
            length, i = _varint(buf, i)
            val = buf[i:i + length]
            i += length
            res.append((fnum, wtype, val))
        elif wtype == 1:  # 64-bit
            val = buf[i:i + 8]
            i += 8
            res.append((fnum, wtype, val))
        elif wtype == 5:  # 32-bit
            val = buf[i:i + 4]
            i += 4
            res.append((fnum, wtype, val))
        else:
            break
    return res


def _parse_ts(payload: bytes) -> str:
    for fn, wt, val in fields(payload):
        if fn == 5 and wt == 2:
            for sfn, swt, sval in fields(val):
                if sfn == 1 and swt == 2:
                    secs = nanos = 0
                    for tfn, twt, tval in fields(sval):
                        if tfn == 1:
                            secs = tval
                        elif tfn == 2:
                            nanos = tval
                    dt = datetime.datetime.fromtimestamp(
                        secs + nanos / 1e9, tz=datetime.timezone.utc)
                    return dt.isoformat()
    return ""


# --------------------------------------------------------------------------- #
# metadata cache
# --------------------------------------------------------------------------- #

_SUMMARIES_CACHE: dict[str, dict[str, str]] | None = None
_SUMMARIES_MTIME: float = 0.0


def _get_summaries() -> dict[str, dict[str, str]]:
    global _SUMMARIES_CACHE, _SUMMARIES_MTIME
    sum_path = os.environ.get("CLAUDELOG_ANTIGRAVITY_SUMMARIES") or os.path.join(
        os.path.expanduser("~"), ".gemini", "antigravity-cli", "conversation_summaries.db")
    if not os.path.exists(sum_path):
        return {}
    try:
        mtime = os.path.getmtime(sum_path)
        if _SUMMARIES_CACHE is not None and mtime == _SUMMARIES_MTIME:
            return _SUMMARIES_CACHE
        con = sqlite3.connect(sum_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT conversation_id, title, workspace_uris FROM conversation_summaries"
        ).fetchall()
        cache: dict[str, dict[str, str]] = {}
        for r in rows:
            cache[r["conversation_id"]] = {
                "title": r["title"] or "",
                "workspace_uris": r["workspace_uris"] or "[]",
            }
        _SUMMARIES_CACHE = cache
        _SUMMARIES_MTIME = mtime
        return cache
    except Exception:
        return {}


def _project_from_uris(uris_json: str) -> str:
    if not uris_json:
        return "antigravity"
    try:
        uris = json.loads(uris_json)
        if isinstance(uris, list) and uris:
            uri = uris[0]
            if uri.startswith("file://"):
                path = uri[len("file://"):]
                name = os.path.basename(path.rstrip("/"))
                return name or "antigravity"
    except Exception:
        pass
    return "antigravity"


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def parse_session(path: str, root: str = "") -> Session:
    """Parse an antigravity-cli conversation database into a :class:`Session`."""
    st = os.stat(path)
    sid = os.path.splitext(os.path.basename(path))[0]

    summaries = _get_summaries()
    meta = summaries.get(sid, {})
    title = meta.get("title", "")
    project = _project_from_uris(meta.get("workspace_uris", ""))

    sess = Session(id=sid, path=path, size=st.st_size, mtime=st.st_mtime,
                   project=project, source="antigravity", title=title)

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"
    ).fetchall()

    tools: dict[str, int] = {}
    prompts: list[str] = []
    tool_names: dict[str, str] = {}
    models: dict[str, None] = {"gemini": None}

    for row in rows:
        idx = row["idx"]
        stype = row["step_type"]
        payload = row["step_payload"]
        if not payload:
            continue

        ts = _parse_ts(payload)
        if ts:
            sess.first_ts = sess.first_ts or ts
            sess.last_ts = ts

        f = fields(payload)

        # ── User prompt (step_type=14) ────────────────────────────────── #
        if stype == STEP_USER:
            prompt_text = ""
            for fn, wt, val in f:
                if fn == 19 and wt == 2:
                    for sfn, swt, sval in fields(val):
                        if sfn == 2 and swt == 2:
                            try:
                                prompt_text = sval.decode("utf-8")
                            except Exception:
                                pass
                            break
                        if sfn == 3 and swt == 2:
                            for s2fn, s2wt, s2val in fields(sval):
                                if s2fn == 1 and s2wt == 2:
                                    try:
                                        prompt_text = s2val.decode("utf-8")
                                    except Exception:
                                        pass
                                    break
                if prompt_text:
                    break
            if prompt_text.strip() and not _is_noise(prompt_text):
                sess.n_user += 1
                prompts.append(prompt_text.strip())
                sess.events.append(Event(
                    uuid=f"{sid}-{idx}", parent_uuid=None,
                    kind="prompt", ts=ts, text=prompt_text))
            continue

        # ── Model response / tool call (step_type=15) ─────────────────── #
        if stype == STEP_MODEL:
            for fn, wt, val in f:
                if fn == 20 and wt == 2:
                    sub = fields(val)
                    # 1. Thinking / reasoning
                    for sfn, swt, sval in sub:
                        if sfn == 3 and swt == 2:
                            try:
                                think_text = sval.decode("utf-8")
                                if think_text.strip():
                                    sess.n_thinking += 1
                                    sess.events.append(Event(
                                        uuid=f"{sid}-{idx}-think", parent_uuid=None,
                                        kind="thinking", ts=ts, text=think_text))
                            except Exception:
                                pass
                    # 2. Tool calls
                    for sfn, swt, sval in sub:
                        if sfn == 7 and swt == 2:
                            cid = name = args_str = ""
                            for tfn, twt, tval in fields(sval):
                                if tfn == 1:
                                    cid = tval.decode("utf-8", "replace")
                                elif tfn == 2:
                                    name = tval.decode("utf-8", "replace")
                                elif tfn == 3:
                                    args_str = tval.decode("utf-8", "replace")
                            tool_name = name or "?"
                            tool_names[cid] = tool_name
                            tools[tool_name] = tools.get(tool_name, 0) + 1
                            sess.n_tool += 1

                            parsed_args: Any = args_str
                            try:
                                parsed_args = json.loads(args_str)
                            except Exception:
                                pass
                            title_summary = _summarize_tool_input(tool_name, parsed_args) if isinstance(parsed_args, dict) else ""

                            sess.events.append(Event(
                                uuid=f"{sid}-{idx}-call", parent_uuid=None,
                                kind="tool_use", ts=ts, tool=tool_name,
                                tool_use_id=cid, title=title_summary,
                                text=args_str))
                    # 3. Assistant prose message
                    asst_text = ""
                    for sfn, swt, sval in sub:
                        if sfn in (1, 8) and swt == 2:
                            try:
                                asst_text = sval.decode("utf-8")
                                if asst_text.strip():
                                    break
                            except Exception:
                                pass
                    if asst_text.strip() and not _is_noise(asst_text):
                        sess.n_assistant += 1
                        sess.events.append(Event(
                            uuid=f"{sid}-{idx}-asst", parent_uuid=None,
                            kind="assistant", ts=ts, text=asst_text))
            continue

        # ── Tool execution result (step_type=132) ─────────────────────── #
        if stype == STEP_RESULT:
            call_id = tool_name = result_text = ""
            for fn, wt, val in f:
                if fn == 5 and wt == 2:
                    for sfn, swt, sval in fields(val):
                        if sfn == 4 and swt == 2:
                            for tfn, twt, tval in fields(sval):
                                if tfn == 1:
                                    call_id = tval.decode("utf-8", "replace")
                                elif tfn == 2:
                                    tool_name = tval.decode("utf-8", "replace")
                elif fn == 140 and wt == 2:
                    for sfn, swt, sval in fields(val):
                        if sfn == 2 and swt == 2:
                            for rfn, rwt, rval in fields(sval):
                                if rfn == 1 and rwt == 2:
                                    try:
                                        result_text = rval.decode("utf-8", "replace")
                                    except Exception:
                                        pass
            final_tool = tool_name or tool_names.get(call_id, "")
            sess.events.append(Event(
                uuid=f"{sid}-{idx}-res", parent_uuid=None,
                kind="tool_result", ts=ts, text=result_text,
                tool=final_tool, tool_use_id=call_id))
            continue

        # ── System notices (step_type=101) ────────────────────────────── #
        if stype == STEP_SYSTEM:
            notice_text = ""
            for fn, wt, val in f:
                if fn == 114 and wt == 2:
                    try:
                        notice_text = val.decode("utf-8", "replace")
                    except Exception:
                        pass
            if notice_text.strip() and not _is_noise(notice_text):
                sess.events.append(Event(
                    uuid=f"{sid}-{idx}-sys", parent_uuid=None,
                    kind="system", ts=ts, text=notice_text))
            continue

    sess.models = list(models)
    sess.tools = tools
    sess.prompts = prompts
    if not sess.title and prompts:
        sess.title = prompts[0].strip().splitlines()[0][:120]
    return sess
