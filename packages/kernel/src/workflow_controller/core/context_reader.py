# Copyright (c) 2026 Workflow Controller Contributors
#
# Licensed under the MIT License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read main and subagent context from supported runtime records."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from workflow_controller.transcript.opencode_store import opencode_db_path


def claude_main_transcript_path(
    cwd: str, session_id: str, env: dict[str, str] | None = None
) -> Path:
    project = str(Path(cwd).resolve()).replace("/", "-")
    home = Path((env or os.environ).get("HOME") or Path.home())
    projects = home / ".claude" / "projects"
    direct = projects / project / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    return next(projects.glob(f"*/{session_id}.jsonl"), direct)


def claude_transcript_path(
    cwd: str, parent_id: str, agent_id: str, env: dict[str, str] | None = None
) -> Path:
    project = str(Path(cwd).resolve()).replace("/", "-")
    home = Path((env or os.environ).get("HOME") or Path.home())
    projects = home / ".claude" / "projects"
    direct = projects / project / parent_id / "subagents" / f"agent-{agent_id}.jsonl"
    if direct.is_file():
        return direct
    # `claude -p` can use its process cwd while `--add-dir` points elsewhere.
    for candidate in projects.glob(f"*/{parent_id}/subagents/agent-{agent_id}.jsonl"):
        return candidate
    return direct


def _input_tokens(usage: Any, *, opencode: bool = False) -> int:
    if not isinstance(usage, dict):
        return 0

    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (ValueError, TypeError):
            return 0

    if opencode:
        cache = usage.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        total = 0
        for v in (
            usage.get("input"),
            cache.get("write"),
            cache.get("read"),
        ):
            total += as_int(v)
        return total
    total = 0
    for k in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        total += as_int(usage.get(k))
    return total


def read_claude_context(path: Path) -> int:
    """Return the latest recorded assistant input, including cache tokens."""
    try:
        with path.open("rb") as transcript:
            size = transcript.seek(0, 2)
            offset = max(size - 8 * 1024 * 1024, 0)
            transcript.seek(offset)
            raw = transcript.read()
    except OSError:
        return 0
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if offset:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        value = _input_tokens(message.get("usage"))
        if value:
            return value
    return 0


def _read_opencode_context(
    session_id: str, parent_id: str | None, env: dict[str, str] | None = None
) -> int:
    path = opencode_db_path(env)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA busy_timeout=5000")
            parent = db.execute(
                "SELECT parent_id FROM session WHERE id=?", (session_id,)
            ).fetchone()
            if parent is None or (parent_id is not None and parent[0] != parent_id):
                return 0
            rows = db.execute(
                "SELECT data FROM message WHERE session_id=? ORDER BY time_created DESC LIMIT 20",
                (session_id,),
            ).fetchall()
    except sqlite3.Error:
        return 0
    for (raw,) in rows:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if message.get("role") != "assistant" or not (message.get("time") or {}).get("completed"):
            continue
        value = _input_tokens(message.get("tokens"), opencode=True)
        if value:
            return value
    return 0


def read_opencode_context(agent_id: str, parent_id: str, env: dict[str, str] | None = None) -> int:
    """Read a settled assistant message in a child session of this parent."""
    if not parent_id:
        return 0
    return _read_opencode_context(agent_id, parent_id, env)


def read_main_context(
    runtime: str, cwd: str, session_id: str, env: dict[str, str] | None = None
) -> int:
    if runtime == "opencode":
        return _read_opencode_context(session_id, None, env)
    if runtime in {"claude_sdk", "claude_cli", "claude_code"}:
        return read_claude_context(claude_main_transcript_path(cwd, session_id, env))
    return 0


def read_subagent_context(
    runtime: str, cwd: str, parent_id: str, agent_id: str, env: dict[str, str] | None = None
) -> int:
    if runtime == "opencode":
        return read_opencode_context(agent_id, parent_id, env)
    if runtime in {"claude_sdk", "claude_cli", "claude_code"}:
        return read_claude_context(claude_transcript_path(cwd, parent_id, agent_id, env))
    return 0


def _snapshot_opencode(
    session_id: str, parent_id: str | None, target: Path, env: dict[str, str] | None = None
) -> bool:
    path = opencode_db_path(env)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            parent = db.execute(
                "SELECT parent_id FROM session WHERE id=?", (session_id,)
            ).fetchone()
            if parent is None or (parent_id is not None and parent[0] != parent_id):
                return False
            rows = db.execute(
                "SELECT 'message', data, time_created FROM message WHERE session_id=? "
                "UNION ALL SELECT 'part', data, time_created FROM part WHERE session_id=? "
                "ORDER BY time_created",
                (session_id, session_id),
            ).fetchall()
        with target.open("w", encoding="utf-8") as out:
            for kind, raw, _ in rows:
                out.write(
                    json.dumps({"kind": kind, "data": json.loads(raw)}, ensure_ascii=False) + "\n"
                )
        return bool(rows)
    except (sqlite3.Error, OSError, TypeError, json.JSONDecodeError):
        return False


def snapshot_main_source(
    runtime: str, cwd: str, session_id: str, target: Path, env: dict[str, str] | None = None
) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    if runtime == "opencode":
        return _snapshot_opencode(session_id, None, target, env)
    if runtime in {"claude_sdk", "claude_cli", "claude_code"}:
        try:
            shutil.copyfile(claude_main_transcript_path(cwd, session_id, env), target)
            return True
        except OSError:
            return False
    return False


def snapshot_source(
    runtime: str,
    cwd: str,
    parent_id: str,
    agent_id: str,
    target: Path,
    env: dict[str, str] | None = None,
) -> bool:
    """Copy a child's persisted conversation into a batch-local source file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if runtime in {"claude_sdk", "claude_cli", "claude_code"}:
        path = claude_transcript_path(cwd, parent_id, agent_id, env)
        try:
            shutil.copyfile(path, target)
            return True
        except OSError:
            return False
    if runtime != "opencode":
        return False
    return _snapshot_opencode(agent_id, parent_id, target, env)
