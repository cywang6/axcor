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

"""Offline OpenCode transcript and file-provenance audit export."""

from __future__ import annotations

__all__ = [
    "OpenCodeAuditSource",
    "export_opencode_audit_evidence",
    "read_opencode_audit_source",
    "write_audit_bundle",
]

import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from workflow_controller.plugins.file_provenance.audit_export import build_audit_evidence
from workflow_controller.plugins.file_provenance.store import ProvenanceStore
from workflow_controller.session.events import Event, EventType

_REQUIRED_COLUMNS = {
    "session": {"id", "parent_id", "directory", "time_created", "time_updated"},
    "message": {"id", "session_id", "time_created", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}
_TASK_ID_RE = re.compile(r"<task\s+id=[\"']([^\s\"'>]+)")
_EVENT_HISTORY_WARNING = (
    "OpenCode append-only event history is excluded; transient, removed, or "
    "unpersisted states are not reconstructed."
)
_CAPTURE_CONFIG_WARNING = (
    "Standalone capture ignore and max-file-size settings were not persisted; "
    "coverage values are unknown."
)
_PUBLISHED_FILE_MODE = 0o644


@dataclass(slots=True)
class OpenCodeAuditSource:
    session: dict[str, Any]
    events: list[Event]
    session_ids: list[str]
    parent_by_session: dict[str, str | None]
    parent_tool_by_session: dict[str, str]
    warnings: list[str]


def export_opencode_audit_evidence(
    project: Path,
    root_session_id: str,
    opencode_db: Path,
) -> dict[str, Any]:
    """Build one sift-records v2 package without a running Workflow Controller service."""
    project = project.expanduser().resolve()
    opencode_db = opencode_db.expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"project is not a directory: {project}")
    source = read_opencode_audit_source(opencode_db, root_session_id, project)
    try:
        store = ProvenanceStore(project, readonly=True)
        captures = store.list_captures_for_native_sessions(source.session_ids)
        for capture in captures:
            native_session_id = str(capture.get("native_session_id") or "")
            capture["parent_native_session_id"] = source.parent_by_session.get(native_session_id)
            parent_tool_id = source.parent_tool_by_session.get(native_session_id)
            if parent_tool_id:
                capture["attributes"] = {
                    **(capture.get("attributes") or {}),
                    "parent_tool_use_id": parent_tool_id,
                }

        warnings = [*source.warnings, _EVENT_HISTORY_WARNING, _CAPTURE_CONFIG_WARNING]
        if not captures:
            warnings.append("No file-provenance captures matched the exported OpenCode sessions.")
        bundle = build_audit_evidence(
            source.session,
            source.events,
            captures,
            store.read_snapshot,
            coverage_warnings=tuple(warnings),
        )
    except sqlite3.Error as exc:
        raise ValueError(f"failed to read provenance database: {exc}") from exc
    bundle["producer"] = "workflow-controller-opencode-offline"
    bundle["coverage"].update(
        {
            "source": "opencode-db",
            "opencode_db": str(opencode_db),
            "root_session_id": root_session_id,
            "session_count": len(source.session_ids),
            "event_history_included": False,
        }
    )
    return bundle


def read_opencode_audit_source(
    db_path: Path,
    root_session_id: str,
    project: Path,
) -> OpenCodeAuditSource:
    """Read canonical session/message/part state and recursively include children."""
    if not root_session_id:
        raise ValueError("OpenCode session ID must not be empty")
    if not db_path.is_file():
        raise FileNotFoundError(f"OpenCode database does not exist: {db_path}")
    uri = f"{db_path.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot open OpenCode database: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN")
        return _read_source_rows(conn, root_session_id, project)
    except sqlite3.Error as exc:
        raise ValueError(f"failed to read OpenCode database: {exc}") from exc
    finally:
        conn.close()


def _read_source_rows(
    conn: sqlite3.Connection,
    root_session_id: str,
    project: Path,
) -> OpenCodeAuditSource:
    _validate_opencode_schema(conn)
    rows = _session_tree(conn, root_session_id)
    if not rows:
        raise KeyError(f"OpenCode session not found: {root_session_id}")
    root = rows[0]
    session_directory = Path(str(root["directory"])).expanduser().resolve()
    if session_directory != project and project not in session_directory.parents:
        raise ValueError(f"OpenCode session directory is outside project: {session_directory}")

    parent_by_session = {
        str(row["id"]): str(row["parent_id"]) if row["parent_id"] else None for row in rows
    }
    depths = _session_depths(rows, root_session_id)
    warnings: list[str] = []
    events, parent_tools, initial_prompt = _read_all_session_events(
        conn,
        rows,
        root_session_id,
        parent_by_session,
        warnings,
    )
    events.extend(_child_session_events(rows[1:], parent_by_session, parent_tools, warnings))

    session_ids = [str(row["id"]) for row in rows]
    _orphan_task_warnings(parent_tools, session_ids, warnings)
    session = _session_summary(
        root,
        rows,
        root_session_id,
        project,
        parent_by_session,
        depths,
        initial_prompt,
    )
    return OpenCodeAuditSource(
        session=session,
        events=events,
        session_ids=session_ids,
        parent_by_session=parent_by_session,
        parent_tool_by_session=parent_tools,
        warnings=warnings,
    )


def _orphan_task_warnings(
    parent_tools: dict[str, str],
    session_ids: list[str],
    warnings: list[str],
) -> None:
    included_session_ids = set(session_ids)
    for child_id in sorted(set(parent_tools) - included_session_ids):
        warnings.append(
            f"task tool references child session {child_id}, but it is not "
            "included in the parent_id session tree"
        )


def _read_all_session_events(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    root_session_id: str,
    parent_by_session: dict[str, str | None],
    warnings: list[str],
) -> tuple[list[Event], dict[str, str], str | None]:
    parent_tools: dict[str, str] = {}
    events: list[Event] = []
    initial_prompt: str | None = None
    for row in rows:
        session_id = str(row["id"])
        session_events, child_tools, prompt = _read_session_events(
            conn,
            session_id,
            parent_by_session[session_id],
            _session_agent(row),
            parent_tools.get(session_id),
            warnings,
        )
        events.extend(session_events)
        parent_tools.update(child_tools)
        if session_id == root_session_id and prompt:
            initial_prompt = prompt
    return events, parent_tools, initial_prompt


def _child_session_events(
    rows: list[sqlite3.Row],
    parent_by_session: dict[str, str | None],
    parent_tools: dict[str, str],
    warnings: list[str],
) -> list[Event]:
    events: list[Event] = []
    for row in rows:
        child_id = str(row["id"])
        parent_id = parent_by_session[child_id]
        parent_tool_id = parent_tools.get(child_id)
        scope = _scope(
            child_id,
            parent_id,
            _session_agent(row),
            parent_tool_id=parent_tool_id,
        )
        events.extend(
            [
                _event(
                    f"session:{child_id}:start",
                    child_id,
                    0,
                    _timestamp(
                        row["time_created"],
                        warnings,
                        label=f"session {child_id} creation time",
                    ),
                    EventType.SUBAGENT_START,
                    {**scope, "agent_type": scope["agent"]},
                ),
                _event(
                    f"session:{child_id}:stop",
                    child_id,
                    0,
                    _timestamp(
                        row["time_updated"],
                        warnings,
                        label=f"session {child_id} update time",
                    ),
                    EventType.SUBAGENT_STOP,
                    {**scope, "agent_type": scope["agent"]},
                ),
            ]
        )
        if not parent_tool_id:
            warnings.append(f"child session {child_id} has no matching parent task tool call")
    return events


def _session_summary(
    root: sqlite3.Row,
    rows: list[sqlite3.Row],
    root_session_id: str,
    project: Path,
    parent_by_session: dict[str, str | None],
    depths: dict[str, int],
    initial_prompt: str | None,
) -> dict[str, Any]:
    return {
        "id": root_session_id,
        "native_session_id": root_session_id,
        "runtime": "opencode",
        "project_dir": str(project),
        "directory": str(root["directory"]),
        "title": _optional_column(root, "title"),
        "version": _optional_column(root, "version"),
        "agent": _session_agent(root),
        "model": _session_model(root),
        "initial_prompt": initial_prompt,
        "native_sessions": [
            {
                "id": str(row["id"]),
                "parent_id": parent_by_session[str(row["id"])],
                "depth": depths[str(row["id"])],
                "agent": _session_agent(row),
                "model": _session_model(row),
            }
            for row in rows
        ],
    }


def write_audit_bundle(bundle: dict[str, Any], target: Path, *, force: bool = False) -> Path:
    """Publish a complete JSON bundle atomically, refusing overwrite by default."""
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(bundle, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # NamedTemporaryFile creates 0600 by default; normalize so the hardlink,
        # O_EXCL fallback and --force replace paths all publish 0o644.
        os.chmod(temporary, _PUBLISHED_FILE_MODE)
        if force:
            os.replace(temporary, target)
            temporary = None
        else:
            _publish_exclusive(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_exclusive(temporary: Path, target: Path) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise FileExistsError(f"output already exists: {target}") from exc
    except OSError:
        # Some filesystems do not support hard links. Preserve the
        # no-overwrite contract with an exclusive create fallback.
        target_fd: int | None = None
        created_target = False
        try:
            target_fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _PUBLISHED_FILE_MODE,
            )
            created_target = True
            destination = os.fdopen(target_fd, "wb")
            target_fd = None
            with destination, open(temporary, "rb") as source:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            # os.open mode is masked by umask; reset to the canonical
            # published mode so the fallback path matches hardlink/force.
            os.chmod(target, _PUBLISHED_FILE_MODE)
        except FileExistsError as exc:
            raise FileExistsError(f"output already exists: {target}") from exc
        except Exception:
            if target_fd is not None:
                os.close(target_fd)
            if created_target:
                target.unlink(missing_ok=True)
            raise


def _validate_opencode_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing_tables = set(_REQUIRED_COLUMNS) - tables
    if missing_tables:
        raise ValueError(
            f"OpenCode database is missing tables: {', '.join(sorted(missing_tables))}"
        )
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = required - columns
        if missing:
            raise ValueError(
                f"OpenCode {table} table is missing columns: {', '.join(sorted(missing))}"
            )


def _session_tree(conn: sqlite3.Connection, root_id: str) -> list[sqlite3.Row]:
    root = conn.execute("SELECT * FROM session WHERE id=?", (root_id,)).fetchone()
    if root is None:
        return []
    rows = [root]
    seen = {root_id}
    pending = [root_id]
    while pending:
        parent_id = pending.pop(0)
        children = conn.execute(
            "SELECT * FROM session WHERE parent_id=? ORDER BY time_created,id",
            (parent_id,),
        ).fetchall()
        for child in children:
            child_id = str(child["id"])
            if child_id in seen:
                raise ValueError(f"OpenCode session parent cycle detected at {child_id}")
            seen.add(child_id)
            rows.append(child)
            pending.append(child_id)
    return rows


def _session_depths(rows: list[sqlite3.Row], root_id: str) -> dict[str, int]:
    parents = {str(row["id"]): str(row["parent_id"]) if row["parent_id"] else None for row in rows}
    depths = {root_id: 0}
    for session_id in parents:
        current = session_id
        lineage: list[str] = []
        while current not in depths:
            lineage.append(current)
            parent = parents.get(current)
            if parent is None:
                raise ValueError(f"session {session_id} is detached from root {root_id}")
            current = parent
        depth = depths.get(current)
        if depth is None:
            raise ValueError(f"session {session_id} is detached from root {root_id}")
        for item in reversed(lineage):
            depth += 1
            depths[item] = depth
    return depths


def _read_session_events(
    conn: sqlite3.Connection,
    session_id: str,
    parent_session_id: str | None,
    session_agent: str | None,
    parent_tool_id: str | None,
    warnings: list[str],
) -> tuple[list[Event], dict[str, str], str | None]:
    messages = conn.execute(
        "SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created,id",
        (session_id,),
    ).fetchall()
    events: list[Event] = []
    child_tools: dict[str, str] = {}
    initial_prompt: str | None = None
    iteration = 0
    for message in messages:
        message_events, iteration, initial_prompt = _message_events(
            conn,
            message,
            session_id,
            parent_session_id,
            session_agent,
            parent_tool_id,
            iteration,
            initial_prompt,
            warnings,
            child_tools,
        )
        events.extend(message_events)
    return events, child_tools, initial_prompt


def _message_events(
    conn: sqlite3.Connection,
    message: sqlite3.Row,
    session_id: str,
    parent_session_id: str | None,
    session_agent: str | None,
    parent_tool_id: str | None,
    iteration: int,
    initial_prompt: str | None,
    warnings: list[str],
    child_tools: dict[str, str],
) -> tuple[list[Event], int, str | None]:
    message_id = str(message["id"])
    data = _json_object(message["data"], f"message {message_id}")
    role = str(data.get("role") or "unknown")
    if role == "assistant":
        iteration += 1
    agent = str(data.get("agent") or session_agent or "") or None
    parsed_parts = _message_parts(conn, message_id)
    scope = _scope(session_id, parent_session_id, agent, parent_tool_id=parent_tool_id)
    timestamp = _message_timestamp(data, message, message_id, warnings)
    has_text_part = any(
        str(part.get("type") or "") in {"text", "reasoning", "subtask"} for _, part in parsed_parts
    )
    events, initial_prompt = _message_body_events(
        data,
        role,
        message_id,
        session_id,
        iteration,
        timestamp,
        scope,
        has_text_part,
        initial_prompt,
    )
    part_events, initial_prompt = _part_events(
        parsed_parts,
        role,
        session_id,
        iteration,
        scope,
        warnings,
        child_tools,
        initial_prompt,
    )
    events.extend(part_events)
    return events, iteration, initial_prompt


def _message_parts(
    conn: sqlite3.Connection,
    message_id: str,
) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    parts = conn.execute(
        "SELECT id,time_created,data FROM part WHERE message_id=? ORDER BY time_created,id",
        (message_id,),
    ).fetchall()
    return [(part, _json_object(part["data"], f"part {part['id']}")) for part in parts]


def _message_timestamp(
    data: dict[str, Any],
    message: sqlite3.Row,
    message_id: str,
    warnings: list[str],
) -> datetime:
    return _timestamp(
        _mapping(data.get("time")).get("created"),
        warnings,
        fallback=message["time_created"],
        label=f"message {message_id} creation time",
    )


def _message_body_events(
    data: dict[str, Any],
    role: str,
    message_id: str,
    session_id: str,
    iteration: int,
    timestamp: datetime,
    scope: dict[str, Any],
    has_text_part: bool,
    initial_prompt: str | None,
) -> tuple[list[Event], str | None]:
    events: list[Event] = []
    message_content = data.get("content")
    if not has_text_part and isinstance(message_content, str) and message_content:
        events.append(
            _text_message_event(
                f"message:{message_id}",
                session_id,
                iteration,
                timestamp,
                scope,
                role,
                message_content,
            )
        )
        if role == "user" and initial_prompt is None:
            initial_prompt = message_content
    if data.get("error"):
        events.append(
            _event(
                f"message:{message_id}:error",
                session_id,
                iteration,
                timestamp,
                EventType.ERROR,
                {**scope, "error": data["error"]},
            )
        )
    return events, initial_prompt


def _part_events(
    parsed_parts: list[tuple[sqlite3.Row, dict[str, Any]]],
    role: str,
    session_id: str,
    iteration: int,
    scope: dict[str, Any],
    warnings: list[str],
    child_tools: dict[str, str],
    initial_prompt: str | None,
) -> tuple[list[Event], str | None]:
    events: list[Event] = []
    for part_row, part in parsed_parts:
        part_id, part_type, part_time = _part_preamble(part_row, part, warnings)
        text = _part_text(role, part)
        if text:
            events.append(
                _text_message_event(
                    f"part:{part_id}:message",
                    session_id,
                    iteration,
                    part_time,
                    scope,
                    role,
                    text,
                )
            )
            if role == "user" and initial_prompt is None:
                initial_prompt = text
        elif part_type == "tool":
            tool_events, call_id, child_id = _tool_part_events(
                part_row,
                part,
                session_id=session_id,
                iteration=iteration,
                part_time=part_time,
                scope=scope,
                warnings=warnings,
            )
            events.extend(tool_events)
            if child_id:
                child_tools.setdefault(child_id, call_id)
        elif part_type == "step-finish":
            events.append(
                _step_finish_event(part, part_id, session_id, iteration, part_time, scope)
            )
    return events, initial_prompt


def _part_preamble(
    part_row: sqlite3.Row,
    part: dict[str, Any],
    warnings: list[str],
) -> tuple[str, str, datetime]:
    part_id = str(part_row["id"])
    part_type = str(part.get("type") or "")
    part_time = _timestamp(
        _mapping(part.get("time")).get("start"),
        warnings,
        fallback=part_row["time_created"],
        label=f"part {part_id} start time",
    )
    return part_id, part_type, part_time


def _step_finish_event(
    part: dict[str, Any],
    part_id: str,
    session_id: str,
    iteration: int,
    part_time: datetime,
    scope: dict[str, Any],
) -> Event:
    return _event(
        f"part:{part_id}",
        session_id,
        iteration,
        part_time,
        EventType.STEP_FINISH,
        {**scope, "reason": part.get("reason"), "cost": part.get("cost")},
    )


def _tool_part_events(
    part_row: sqlite3.Row,
    part: dict[str, Any],
    *,
    session_id: str,
    iteration: int,
    part_time: datetime,
    scope: dict[str, Any],
    warnings: list[str],
) -> tuple[list[Event], str, str | None]:
    part_id = str(part_row["id"])
    state = _mapping(part.get("state"))
    call_id = str(part.get("callID") or part_id)
    tool = str(part.get("tool") or "")
    tool_scope = {**scope, "id": call_id, "name": tool}
    tool_input = part.get("input") or state.get("input") or {}
    tool_scope["input"] = tool_input if isinstance(tool_input, dict) else {"value": tool_input}
    events = [
        _event(
            f"part:{part_id}:use",
            session_id,
            iteration,
            part_time,
            EventType.TOOL_USE,
            tool_scope,
        )
    ]
    events.append(
        _tool_result_event(part_row, part, state, scope, session_id, iteration, call_id, warnings)
    )
    child_id = _child_session_id(part, state, tool)
    return events, call_id, child_id


def _tool_result_event(
    part_row: sqlite3.Row,
    part: dict[str, Any],
    state: dict[str, Any],
    scope: dict[str, Any],
    session_id: str,
    iteration: int,
    call_id: str,
    warnings: list[str],
) -> Event:
    part_id = str(part_row["id"])
    result = part.get("output") if "output" in part else state.get("output")
    status = str(state.get("status") or part.get("status") or "unknown")
    if result is None and state.get("error") is not None:
        result = state["error"]
    return _event(
        f"part:{part_id}:result",
        session_id,
        iteration,
        _timestamp(
            _mapping(state.get("time")).get("end"),
            warnings,
            fallback=part_row["time_created"],
            label=f"part {part_id} end time",
        ),
        EventType.TOOL_RESULT,
        {
            **scope,
            "tool_use_id": call_id,
            "content": result,
            "is_error": status == "error" or state.get("error") is not None,
        },
    )


def _text_message_event(
    event_id: str,
    session_id: str,
    iteration: int,
    timestamp: datetime,
    scope: dict[str, Any],
    role: str,
    text: str,
) -> Event:
    event_type = EventType.USER_MESSAGE if role == "user" else EventType.ASSISTANT_MESSAGE
    payload = {
        **scope,
        "content": [{"type": "text", "text": text}],
    }
    return _event(event_id, session_id, iteration, timestamp, event_type, payload)


def _part_text(role: str, part: dict[str, Any]) -> str:
    part_type = str(part.get("type") or "")
    text = part.get("text")
    if part_type == "text" and isinstance(text, str):
        return text.strip()
    is_reasoning_text = bool(
        role == "assistant" and part_type == "reasoning" and isinstance(text, str) and text
    )
    if is_reasoning_text:
        return f"<thinking>{text}</thinking>"
    if part_type == "subtask":
        description = str(part.get("description") or "")
        prompt = str(part.get("prompt") or "")
        return f"[SUBTASK] {description}\n{prompt}".strip()
    return ""


def _child_session_id(part: dict[str, Any], state: dict[str, Any], tool: str) -> str | None:
    metadata = _mapping(state.get("metadata"))
    value = metadata.get("sessionId")
    if value:
        return str(value)
    # Only the task tool spawns a subagent; restrict the envelope regex to it
    # so a non-task tool whose output happens to contain "<task id=...>" can't
    # be misattributed as a parent tool-use.
    if tool != "task":
        return None
    output = part.get("output") if "output" in part else state.get("output")
    match = _TASK_ID_RE.search(output) if isinstance(output, str) else None
    return match.group(1) if match else None


def _scope(
    session_id: str,
    parent_session_id: str | None,
    agent: str | None,
    *,
    parent_tool_id: str | None = None,
) -> dict[str, Any]:
    return {
        "agent": agent,
        "native_session_id": session_id,
        "parent_native_session_id": parent_session_id,
        "parent_tool_use_id": parent_tool_id,
    }


def _session_agent(row: sqlite3.Row) -> str | None:
    value = _optional_column(row, "agent")
    if not value:
        return None
    return str(value)


def _session_model(row: sqlite3.Row) -> Any:
    value = _optional_column(row, "model")
    if not value:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)


def _optional_column(row: sqlite3.Row, name: str) -> Any:
    try:
        return row[name]
    except IndexError:
        return None


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in OpenCode {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"OpenCode {label} data must be a JSON object")
    return parsed


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(
    value: Any,
    warnings: list[str],
    *,
    fallback: Any = None,
    label: str = "timestamp",
) -> datetime:
    selected = value if value is not None else fallback
    if isinstance(selected, (int, float)):
        try:
            return datetime.fromtimestamp(float(selected) / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError):
            pass
    if isinstance(selected, str) and selected:
        try:
            parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    warnings.append(
        f"{label} is missing or invalid; timestamp defaulted to 1970-01-01T00:00:00+00:00"
    )
    return datetime.fromtimestamp(0, tz=UTC)


def _event(
    event_id: str,
    session_id: str,
    iteration: int,
    timestamp: datetime,
    event_type: EventType,
    data: dict[str, Any],
) -> Event:
    return Event(event_id, session_id, iteration, timestamp, event_type, data)
