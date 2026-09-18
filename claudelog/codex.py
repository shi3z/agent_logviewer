"""Parse Codex CLI rollouts into the same event stream as Claude Code.

Codex writes one JSONL file per session under
``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl``.  Every line
is ``{timestamp, ordinal, type, payload}``; the conversation lives in
``response_item`` records:

  * ``message``               -- role ``user`` / ``assistant`` / ``developer``
  * ``custom_tool_call``      -- the ``exec`` tool (input is a JS snippet)
  * ``custom_tool_call_output``
  * ``function_call``         -- named tools such as ``wait``
  * ``function_call_output``
  * ``reasoning``             -- encrypted; only ``summary`` is readable

``event_msg`` records duplicate much of the above for the TUI, and
``world_state`` / ``turn_context`` / ``token_usage_record`` are bookkeeping,
so all of those are skipped.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterator

from .parser import Event, Session, _is_noise, _summarize_tool_input

#: ``message`` roles that are harness instructions, not conversation
_SKIP_ROLES = {"developer", "system"}

#: user-role records the harness injects: environment dumps, skill lists, ...
_HARNESS_RE = re.compile(
    r"^\s*<(?:environment_context|skills_instructions|user_instructions"
    r"|system-reminder|command-name|local-command-stdout)")


def _is_harness(text: str) -> bool:
    return bool(_HARNESS_RE.match(text))


def iter_lines(path: str) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                yield obj


def _text_of(content: Any) -> str:
    """Flatten a Codex content array into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") in ("input_text", "output_text", "text"):
                parts.append(block.get("text") or "")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _output_of(payload: dict) -> str:
    """Flatten a ``*_call_output`` payload's ``output`` array."""
    out = payload.get("output")
    if isinstance(out, str):
        return out
    return _text_of(out)


def _tool_title(name: str, raw: Any) -> str:
    """One-line summary of a Codex tool call."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw.strip().splitlines()[0][:160] if raw.strip() else ""
    if isinstance(raw, dict):
        # ``exec`` wraps the real call in a JS snippet; the arguments object
        # inside it is the useful part.
        inner = raw.get("cmd") or raw.get("command") or raw.get("path")
        if isinstance(inner, str) and inner.strip():
            return inner.strip().splitlines()[0][:160]
        return _summarize_tool_input(name, raw)
    return ""


def parse_session(path: str, root: str = "") -> Session:
    """Parse one Codex rollout file into a :class:`Session`."""
    st = os.stat(path)
    sid = os.path.splitext(os.path.basename(path))[0]
    sess = Session(id=sid, path=path, size=st.st_size, mtime=st.st_mtime,
                   project=_project_of(path, root), source="codex")

    tools: dict[str, int] = {}
    prompts: list[str] = []
    tool_names: dict[str, str] = {}
    models: dict[str, None] = {}

    for obj in iter_lines(path):
        etype = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = obj.get("timestamp") or ""
        if ts:
            sess.first_ts = sess.first_ts or ts
            sess.last_ts = ts

        if etype == "session_meta":
            sess.cwd = payload.get("cwd") or sess.cwd
            sess.version = payload.get("cli_version") or sess.version
            sess.id = payload.get("session_id") or sess.id
            continue

        if etype == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                models.setdefault(model, None)
            continue

        if etype != "response_item":
            continue

        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role")
            if role in _SKIP_ROLES:
                continue
            text = _text_of(payload.get("content"))
            if not text.strip() or _is_noise(text) or _is_harness(text):
                continue
            if role == "user":
                sess.n_user += 1
                prompts.append(text.strip())
                sess.events.append(Event(
                    uuid=payload.get("id") or "", parent_uuid=None,
                    kind="prompt", ts=ts, text=text))
            else:
                sess.n_assistant += 1
                sess.events.append(Event(
                    uuid=payload.get("id") or "", parent_uuid=None,
                    kind="assistant", ts=ts, text=text))
            continue

        if ptype in ("custom_tool_call", "function_call"):
            name = payload.get("name") or "?"
            raw = payload.get("input")
            if raw is None:
                raw = payload.get("arguments")
            call_id = payload.get("call_id") or payload.get("id") or ""
            tool_names[call_id] = name
            tools[name] = tools.get(name, 0) + 1
            sess.n_tool += 1
            sess.events.append(Event(
                uuid=payload.get("id") or "", parent_uuid=None,
                kind="tool_use", ts=ts, tool=name, tool_use_id=call_id,
                title=_tool_title(name, raw),
                text=raw if isinstance(raw, str)
                     else json.dumps(raw, ensure_ascii=False, indent=2)))
            continue

        if ptype in ("custom_tool_call_output", "function_call_output"):
            call_id = payload.get("call_id") or ""
            text = _output_of(payload)
            sess.events.append(Event(
                uuid=payload.get("id") or "", parent_uuid=None,
                kind="tool_result", ts=ts, text=text,
                tool=tool_names.get(call_id, ""), tool_use_id=call_id))
            continue

        if ptype == "reasoning":
            # Codex stores reasoning encrypted; only the summary is readable,
            # and in practice it is empty.  Index it when present.
            summary = payload.get("summary")
            text = _text_of(summary) if isinstance(summary, list) else ""
            if text.strip():
                sess.n_thinking += 1
                sess.events.append(Event(
                    uuid=payload.get("id") or "", parent_uuid=None,
                    kind="thinking", ts=ts, text=text))

    sess.models = list(models)
    sess.tools = tools
    sess.prompts = prompts
    if not sess.title and prompts:
        sess.title = prompts[0].strip().splitlines()[0][:120]
    return sess


def _project_of(path: str, root: str) -> str:
    """``sessions/2026/09/17`` -> ``codex/2026-09-17``."""
    if not root:
        return "codex"
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if len(parts) >= 4 and parts[0] == "sessions":
        return "codex/" + "-".join(parts[1:4])
    return "codex"
