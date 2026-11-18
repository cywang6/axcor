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

"""Build a portable sift-records v2 bundle from session and file evidence."""

from __future__ import annotations

__all__ = ["build_audit_evidence"]

import base64
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from workflow_controller.session.events import EventType

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal


_MESSAGE_TYPES = {
    EventType.ASSISTANT_MESSAGE,
    EventType.USER_MESSAGE,
}
_CONTROL_TYPES = {
    EventType.ITERATION_START,
    EventType.ITERATION_END,
    EventType.STEP_FINISH,
    EventType.ERROR,
    EventType.SUBAGENT_START,
    EventType.SUBAGENT_PROGRESS,
    EventType.SUBAGENT_STOP,
}

_CAPTURE_ENTRY_KEYS = (
    "capture_id",
    "session_id",
    "actor_kind",
    "native_session_id",
    "parent_native_session_id",
    "runtime",
    "model",
    "agent_name",
    "native_agent_id",
    "tool_name",
    "tool_use_id",
    "cwd",
    "started_at",
    "finished_at",
    "succeeded",
    "is_error",
    "incomplete",
    "conflicted",
    "tool_input",
    "tool_output",
    "attributes",
    "head_before",
    "head_after",
    "committed_sha",
)


class _CaptureIndexes(NamedTuple):
    by_tool: dict[str, list[dict[str, Any]]]
    by_scope: dict[tuple[str, str], list[dict[str, Any]]]
    by_id: dict[str, dict[str, Any]]
    ambiguous: set[str]


def build_audit_evidence(
    session: dict[str, Any],
    events: list[Any],
    captures: list[dict[str, Any]],
    read_snapshot: Callable[[str, Literal["before", "after"], str], bytes | None],
    *,
    ignore: tuple[str, ...] = (),
    max_file_bytes: int = 0,
    coverage_warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    warnings: list[str] = []
    blobs: dict[str, dict[str, Any]] = {}
    indexes = _capture_indexes(captures, events)
    capture_values: dict[str, dict[str, Any]] = {}
    for capture in captures:
        capture_id = str(capture["capture_id"])
        quality = _quality(capture, capture_id in indexes.ambiguous)
        effects, missing_snapshot = _capture_effects(
            capture_id,
            capture,
            read_snapshot,
            blobs,
            warnings,
        )
        if missing_snapshot and quality == "observed":
            quality = "incomplete"
        capture_values[capture_id] = _capture_entry(capture, quality, effects)
        if quality != "observed":
            warnings.append(f"capture {capture_id} quality={quality}")

    records, linked, has_user = _session_records(events, indexes, warnings)

    initial_prompt = session.get("initial_prompt")
    if initial_prompt and not has_user:
        records.insert(0, {"role": "user", "text": str(initial_prompt), "iteration": 0})

    records.extend(_unlinked_capture_records(captures, linked, warnings))
    records.sort(
        key=lambda value: (str(value.get("timestamp") or ""), str(value.get("event_id") or ""))
    )
    return _bundle(
        session, records, capture_values, blobs, ignore, max_file_bytes, coverage_warnings, warnings
    )


def _capture_indexes(captures: list[dict[str, Any]], events: list[Any]) -> _CaptureIndexes:
    captures_by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    captures_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for capture in captures:
        tool_id = str(capture.get("tool_use_id") or "")
        native_session_id = str(capture.get("native_session_id") or "")
        captures_by_tool[tool_id].append(capture)
        captures_by_scope[(native_session_id, tool_id)].append(capture)
    # A tool-use ID is not globally unique across OpenCode sessions. Keep the
    # conservative global ambiguity marker even when matching can use scope.
    # Captures with an empty tool_use_id share no real id, so exclude them
    # from the global ambiguity bucket instead of lumping them together.
    ambiguous: set[str] = set()
    for tool_id, values in captures_by_tool.items():
        if not (tool_id and len(values) > 1):
            continue
        for capture in values:
            ambiguous.add(str(capture["capture_id"]))
    explicitly_linked = {
        str(event.data.get("capture_id"))
        for event in events
        if event.event_type == EventType.TOOL_USE and event.data.get("capture_id")
    }
    captures_by_id = {str(capture["capture_id"]): capture for capture in captures}
    return _CaptureIndexes(
        by_tool=captures_by_tool,
        by_scope=captures_by_scope,
        by_id=captures_by_id,
        ambiguous=ambiguous - explicitly_linked,
    )


def _capture_effects(
    capture_id: str,
    capture: dict[str, Any],
    read_snapshot: Callable[[str, Literal["before", "after"], str], bytes | None],
    blobs: dict[str, dict[str, Any]],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    effects: list[dict[str, Any]] = []
    missing_snapshot = False
    for mutation in capture.get("mutations") or []:
        path = str(mutation["path"])
        before_hash = mutation.get("before_blob")
        after_hash = mutation.get("after_blob")
        effect_blobs: dict[str, str | None] = {"before": before_hash, "after": after_hash}
        for stage, digest in (("before", before_hash), ("after", after_hash)):
            if not digest or digest in blobs:
                continue
            content = read_snapshot(capture_id, cast("Literal['before', 'after']", stage), path)
            if content is None:
                warnings.append(f"missing {stage} snapshot for {capture_id}:{path}")
                missing_snapshot = True
                effect_blobs[stage] = None
                continue
            blobs[str(digest)] = _blob(str(digest), content)
        effects.append(
            {
                "path": path,
                "before_blob": effect_blobs["before"],
                "after_blob": effect_blobs["after"],
                "additions": int(mutation.get("additions") or 0),
                "deletions": int(mutation.get("deletions") or 0),
                "conflict": bool(mutation.get("conflict")),
                "evidence_ref": f"capture:{capture_id}:{path}",
            }
        )
    return effects, missing_snapshot


def _capture_entry(
    capture: dict[str, Any],
    quality: str,
    effects: list[dict[str, Any]],
) -> dict[str, Any]:
    entry = {key: capture.get(key) for key in _CAPTURE_ENTRY_KEYS}
    entry.update(
        {
            "quality": quality,
            "effects": effects,
            "conflicts": capture.get("conflicts") or [],
        }
    )
    return entry


def _session_records(
    events: list[Any],
    indexes: _CaptureIndexes,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], set[str], bool]:
    result_events = {
        (event.session_id, str(event.data.get("tool_use_id"))): event
        for event in events
        if event.event_type == EventType.TOOL_RESULT and event.data.get("tool_use_id")
    }
    records: list[dict[str, Any]] = []
    linked: set[str] = set()
    has_user = False
    for event in sorted(events, key=lambda item: (item.timestamp, item.id)):
        if event.event_type in _MESSAGE_TYPES:
            record = _message_record(event)
            if record:
                has_user = has_user or record["role"] == "user"
                records.append(record)
            continue
        if event.event_type == EventType.TOOL_USE:
            tool_id, matched, candidates = _matched_tool_capture(event, indexes)
            matched_capture_id: str | None = str(matched["capture_id"]) if matched else None
            if matched_capture_id:
                linked.add(matched_capture_id)
            result = result_events.get((event.session_id, tool_id))
            call = _tool_call(event, tool_id, matched_capture_id, result)
            records.append(_tool_use_record(event, call, matched))
            if len(candidates) > 1:
                warnings.append(f"tool_use_id {tool_id} matched multiple captures")
            continue
        if event.event_type in _CONTROL_TYPES:
            records.append(_control_record(event))
    return records, linked, has_user


def _matched_tool_capture(
    event: Any,
    indexes: _CaptureIndexes,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    tool_id = str(event.data.get("id") or "")
    explicit_capture = indexes.by_id.get(str(event.data.get("capture_id") or ""))
    native_session_id = str(event.data.get("native_session_id") or "")
    candidates = (
        [explicit_capture]
        if explicit_capture is not None
        else (
            indexes.by_scope.get((native_session_id, tool_id))
            if native_session_id
            else indexes.by_tool.get(tool_id)
        )
        or []
    )
    matched = candidates[0] if len(candidates) == 1 else None
    return tool_id, matched, candidates


def _tool_call(event: Any, tool_id: str, capture_id: str | None, result: Any) -> dict[str, Any]:
    return {
        "id": tool_id,
        "name": event.data.get("name") or "",
        "input": event.data.get("input") or {},
        "result": result.data.get("content") if result else None,
        "is_error": bool(result.data.get("is_error")) if result else None,
        "timestamp": event.timestamp.isoformat(),
        "capture_id": capture_id,
    }


def _tool_use_record(
    event: Any,
    call: dict[str, Any],
    matched: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "session_id": event.session_id,
        "role": "assistant",
        "agent": (matched.get("agent_name") if matched else event.data.get("agent")),
        "parent_tool_use_id": (
            _parent(matched) if matched else event.data.get("parent_tool_use_id")
        ),
        "native_session_id": (
            matched.get("native_session_id") if matched else event.data.get("native_session_id")
        ),
        "parent_native_session_id": (
            matched.get("parent_native_session_id")
            if matched
            else event.data.get("parent_native_session_id")
        ),
        "iteration": event.iteration,
        "timestamp": event.timestamp.isoformat(),
        "tool_calls": [call],
    }


def _unlinked_capture_records(
    captures: list[dict[str, Any]],
    linked: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for capture in captures:
        capture_id = str(capture["capture_id"])
        if capture_id in linked:
            continue
        records.append(
            {
                "event_id": capture_id,
                "session_id": capture.get("session_id"),
                "role": "assistant",
                "agent": capture.get("agent_name"),
                "parent_tool_use_id": _parent(capture),
                "native_session_id": capture.get("native_session_id"),
                "parent_native_session_id": capture.get("parent_native_session_id"),
                "iteration": 0,
                "timestamp": capture.get("started_at"),
                "tool_calls": [
                    {
                        "id": capture.get("tool_use_id"),
                        "name": capture.get("tool_name"),
                        "input": capture.get("tool_input") or {},
                        "result": capture.get("tool_output"),
                        "is_error": capture.get("is_error"),
                        "timestamp": capture.get("started_at"),
                        "capture_id": capture_id,
                    }
                ],
                "source": "file_provenance",
            }
        )
        warnings.append(f"capture {capture_id} had no unique session tool event")
    return records


def _bundle(
    session: dict[str, Any],
    records: list[dict[str, Any]],
    captures: dict[str, dict[str, Any]],
    blobs: dict[str, dict[str, Any]],
    ignore: tuple[str, ...],
    max_file_bytes: int,
    coverage_warnings: tuple[str, ...],
    warnings: list[str],
) -> dict[str, Any]:
    runtime = str(session.get("runtime") or "")
    runtime_warnings = list(coverage_warnings)
    if runtime.startswith("claude"):
        runtime_warnings.append(
            "Claude sub-agent read-only tool details may be unavailable; mutating captures remain included."
        )
    return {
        "format": "sift-records",
        "version": 2,
        "producer": "workflow-controller-file-provenance",
        "exported_at": datetime.now(UTC).isoformat(),
        "session": session,
        "records": records,
        "captures": captures,
        "blobs": blobs,
        "coverage": {
            "ignore": list(ignore),
            "max_file_bytes": max_file_bytes,
            "warnings": runtime_warnings,
        },
        "warnings": list(dict.fromkeys([*warnings, *runtime_warnings])),
    }


def _quality(capture: dict[str, Any], ambiguous: bool) -> str:
    if ambiguous:
        return "ambiguous"
    if capture.get("incomplete") or not capture.get("finished_at"):
        return "incomplete"
    crossed_commit = (
        capture.get("head_before")
        and capture.get("head_after")
        and capture.get("head_before") != capture.get("head_after")
    )
    has_conflict = (
        capture.get("conflicted")
        or capture.get("conflicts")
        or crossed_commit
        or any(item.get("conflict") for item in capture.get("mutations") or [])
    )
    if has_conflict:
        return "conflicted"
    return "observed"


def _blob(digest: str, content: bytes) -> dict[str, Any]:
    import hashlib

    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError(f"snapshot digest mismatch: {digest}")
    try:
        return {"encoding": "utf-8", "size": len(content), "content": content.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "size": len(content),
            "content": base64.b64encode(content).decode("ascii"),
        }


def _message_record(event: Any) -> dict[str, Any] | None:
    data = event.data
    if event.event_type == EventType.ASSISTANT_MESSAGE:
        content = data.get("content") or []
        text = "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if not text:
            return None
        role = "assistant"
    else:
        content = data.get("content") or []
        if isinstance(content, list):
            text = "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        else:
            text = str(data.get("prompt") or data.get("text") or content or "").strip()
        if not text:
            return None
        role = "user"
    record = {
        "event_id": event.id,
        "session_id": event.session_id,
        "role": role,
        "text": text,
        "iteration": event.iteration,
        "timestamp": event.timestamp.isoformat(),
    }
    for key in (
        "agent",
        "parent_tool_use_id",
        "native_session_id",
        "parent_native_session_id",
    ):
        value = data.get(key)
        if value is not None:
            record[key] = value
    return record


def _control_record(event: Any) -> dict[str, Any]:
    data = event.data
    label = event.event_type.value
    text = f"[{label}]"
    if event.event_type in {EventType.SUBAGENT_START, EventType.SUBAGENT_STOP}:
        text += f" agent={data.get('agent_type') or data.get('agent_id') or ''}"
        if data.get("content"):
            text += f" {data['content']}"
    else:
        scope_keys = {
            "agent",
            "agent_id",
            "agent_type",
            "parent_tool_use_id",
            "native_session_id",
            "parent_native_session_id",
        }
        display_data = {key: value for key, value in data.items() if key not in scope_keys}
        if display_data:
            text += f" {display_data}"
    record = {
        "event_id": event.id,
        "session_id": event.session_id,
        "role": "assistant",
        "text": text,
        "iteration": event.iteration,
        "timestamp": event.timestamp.isoformat(),
    }
    optional = {
        "agent": data.get("agent_type") or data.get("agent") or data.get("agent_id"),
        "parent_tool_use_id": data.get("parent_tool_use_id"),
        "native_session_id": data.get("native_session_id"),
        "parent_native_session_id": data.get("parent_native_session_id"),
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    return record


def _parent(capture: dict[str, Any]) -> str | None:
    attributes = capture.get("attributes") or {}
    return str(attributes.get("parent_tool_use_id") or "") or None
