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

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta

from workflow_controller.plugins.file_provenance.audit_export import build_audit_evidence
from workflow_controller.session.events import Event, EventType


def _capture(capture_id: str, tool_id: str, *, conflict: bool = False) -> dict:
    content_before = b"old\n"
    content_after = b"new\n"
    return {
        "capture_id": capture_id,
        "session_id": "session-1",
        "actor_kind": "agent",
        "native_session_id": "native-1",
        "parent_native_session_id": None,
        "runtime": "opencode",
        "model": "test",
        "agent_name": "developer",
        "native_agent_id": "agent-1",
        "tool_name": "Edit",
        "tool_use_id": tool_id,
        "cwd": "/tmp/project",
        "started_at": "2026-08-29T10:00:00",
        "finished_at": "2026-08-29T10:00:01",
        "succeeded": True,
        "is_error": False,
        "incomplete": False,
        "conflicted": conflict,
        "tool_input": {"file_path": "src/a.py"},
        "tool_output": "ok",
        "attributes": {},
        "head_before": "head",
        "head_after": "head",
        "committed_sha": None,
        "mutations": [
            {
                "path": "src/a.py",
                "before_blob": hashlib.sha256(content_before).hexdigest(),
                "after_blob": hashlib.sha256(content_after).hexdigest(),
                "additions": 1,
                "deletions": 1,
                "conflict": conflict,
            }
        ],
        "conflicts": ([{"kind": "overlap", "detail": "x"}] if conflict else []),
    }


def test_export_joins_tool_capture_and_embeds_content_addressed_blobs() -> None:
    started = datetime(2026, 8, 29, 10, 0, 0)
    use = Event.create(
        "session-1",
        2,
        EventType.TOOL_USE,
        {
            "id": "tool-1",
            "name": "Edit",
            "input": {"file_path": "src/a.py"},
        },
    )
    use.timestamp = started
    result = Event.create(
        "session-1",
        2,
        EventType.TOOL_RESULT,
        {
            "tool_use_id": "tool-1",
            "content": "ok",
            "is_error": False,
        },
    )
    result.timestamp = started + timedelta(seconds=1)
    snapshots = {("before", "src/a.py"): b"old\n", ("after", "src/a.py"): b"new\n"}

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode", "initial_prompt": "change it"},
        [result, use],
        [_capture("capture-1", "tool-1")],
        lambda _capture_id, stage, path: snapshots.get((stage, path)),
        ignore=(".git",),
        max_file_bytes=1024,
    )

    assert bundle["format"] == "sift-records" and bundle["version"] == 2
    assert bundle["captures"]["capture-1"]["quality"] == "observed"
    call = next(record["tool_calls"][0] for record in bundle["records"] if record.get("tool_calls"))
    assert call["capture_id"] == "capture-1"
    assert call["result"] == "ok"
    assert len(bundle["blobs"]) == 2
    assert {value["content"] for value in bundle["blobs"].values()} == {"old\n", "new\n"}
    assert bundle["coverage"]["max_file_bytes"] == 1024


def test_export_preserves_binary_and_ambiguous_captures() -> None:
    first = _capture("capture-1", "duplicate")
    second = _capture("capture-2", "duplicate")
    binary = b"\xff\x00"
    digest = hashlib.sha256(binary).hexdigest()
    first["mutations"][0].update(before_blob=None, after_blob=digest)
    second["mutations"] = []
    use = Event.create(
        "session-1",
        1,
        EventType.TOOL_USE,
        {
            "id": "duplicate",
            "name": "Write",
            "input": {},
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "claude_cli"},
        [use],
        [first, second],
        lambda capture_id, stage, path: binary if capture_id == "capture-1" else None,
    )

    assert bundle["captures"]["capture-1"]["quality"] == "ambiguous"
    assert bundle["captures"]["capture-2"]["quality"] == "ambiguous"
    assert bundle["blobs"][digest]["encoding"] == "base64"
    assert base64.b64decode(bundle["blobs"][digest]["content"]) == binary
    assert any("multiple captures" in warning for warning in bundle["warnings"])
    assert sum(bool(record.get("tool_calls")) for record in bundle["records"]) == 3


def test_cross_session_duplicate_tool_ids_remain_ambiguous() -> None:
    first = _capture("capture-1", "duplicate")
    first["mutations"] = []
    second = _capture("capture-2", "duplicate")
    second["native_session_id"] = "native-2"
    second["mutations"] = []
    use = Event.create(
        "session-1",
        1,
        EventType.TOOL_USE,
        {
            "id": "duplicate",
            "name": "Edit",
            "input": {},
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [use],
        [first, second],
        lambda _capture_id, _stage, _path: b"new\n",
    )

    assert bundle["captures"]["capture-1"]["quality"] == "ambiguous"
    assert bundle["captures"]["capture-2"]["quality"] == "ambiguous"


def test_captures_with_empty_tool_use_id_are_not_lumped_as_ambiguous() -> None:
    first = _capture("capture-1", "")
    first["mutations"] = []
    second = _capture("capture-2", "")
    second["native_session_id"] = "native-2"
    second["mutations"] = []

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [],
        [first, second],
        lambda _capture_id, _stage, _path: b"new\n",
    )

    assert bundle["captures"]["capture-1"]["quality"] == "observed"
    assert bundle["captures"]["capture-2"]["quality"] == "observed"
    assert not any("quality=ambiguous" in warning for warning in bundle["warnings"])


def test_explicit_capture_id_wins_over_duplicate_tool_use_id() -> None:
    first = _capture("capture-1", "duplicate")
    second = _capture("capture-2", "duplicate")
    second["mutations"] = []
    use = Event.create(
        "session-1",
        1,
        EventType.TOOL_USE,
        {
            "id": "duplicate",
            "capture_id": "capture-1",
            "name": "Edit",
            "input": {},
        },
    )
    snapshots = {("before", "src/a.py"): b"old\n", ("after", "src/a.py"): b"new\n"}

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [use],
        [first, second],
        lambda capture_id, stage, path: (
            snapshots.get((stage, path)) if capture_id == "capture-1" else None
        ),
    )

    call = next(
        record["tool_calls"][0] for record in bundle["records"] if record.get("event_id") == use.id
    )
    assert call["capture_id"] == "capture-1"
    assert bundle["captures"]["capture-1"]["quality"] == "observed"
    assert bundle["captures"]["capture-2"]["quality"] == "ambiguous"


def test_export_extracts_user_message_text_blocks() -> None:
    event = Event.create(
        "session-1",
        1,
        EventType.USER_MESSAGE,
        {
            "content": [
                {"type": "text", "text": "改一下配置"},
                {"type": "tool_result", "content": "ignored"},
                {"type": "text", "text": "并运行测试"},
            ],
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [event],
        [],
        lambda _capture_id, _stage, _path: None,
    )

    assert bundle["records"] == [
        {
            "event_id": event.id,
            "session_id": "session-1",
            "role": "user",
            "text": "改一下配置\n并运行测试",
            "iteration": 1,
            "timestamp": event.timestamp.isoformat(),
        }
    ]


def test_missing_snapshot_is_warning_and_effect_blob_is_empty() -> None:
    capture = _capture("capture-1", "tool-1")
    use = Event.create(
        "session-1",
        1,
        EventType.TOOL_USE,
        {
            "id": "tool-1",
            "name": "Edit",
            "input": {},
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [use],
        [capture],
        lambda _capture_id, stage, _path: b"new\n" if stage == "after" else None,
    )

    effect = bundle["captures"]["capture-1"]["effects"][0]
    assert bundle["captures"]["capture-1"]["quality"] == "incomplete"
    assert effect["before_blob"] is None
    assert effect["after_blob"] is not None
    assert any("missing before snapshot" in warning for warning in bundle["warnings"])


def test_subagent_control_record_does_not_mislabel_agent_as_parent_tool() -> None:
    event = Event.create(
        "session-1",
        1,
        EventType.SUBAGENT_START,
        {
            "agent_id": "agent-1",
            "agent_type": "researcher",
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [event],
        [],
        lambda _capture_id, _stage, _path: None,
    )

    assert "parent_tool_use_id" not in bundle["records"][0]


def test_control_record_keeps_scope_out_of_display_text() -> None:
    event = Event.create(
        "session-1",
        1,
        EventType.STEP_FINISH,
        {
            "reason": "stop",
            "cost": 1.25,
            "agent": "build",
            "parent_tool_use_id": "call-task",
            "native_session_id": "child",
            "parent_native_session_id": "root",
        },
    )

    bundle = build_audit_evidence(
        {"id": "session-1", "runtime": "opencode"},
        [event],
        [],
        lambda _capture_id, _stage, _path: None,
    )

    record = bundle["records"][0]
    assert record["text"] == "[step_finish] {'reason': 'stop', 'cost': 1.25}"
    assert record["agent"] == "build"
    assert record["parent_tool_use_id"] == "call-task"
    assert record["native_session_id"] == "child"
    assert record["parent_native_session_id"] == "root"


def test_same_tool_id_in_different_native_sessions_remains_ambiguous() -> None:
    first = _capture("capture-1", "same-call")
    first["mutations"] = []
    second = _capture("capture-2", "same-call")
    second["native_session_id"] = "native-2"
    second["mutations"] = []
    first_use = Event.create(
        "native-1",
        1,
        EventType.TOOL_USE,
        {
            "id": "same-call",
            "name": "Write",
            "input": {},
            "native_session_id": "native-1",
        },
    )
    second_use = Event.create(
        "native-2",
        1,
        EventType.TOOL_USE,
        {
            "id": "same-call",
            "name": "Write",
            "input": {},
            "native_session_id": "native-2",
        },
    )

    bundle = build_audit_evidence(
        {"id": "native-1", "runtime": "opencode"},
        [first_use, second_use],
        [first, second],
        lambda _capture_id, _stage, _path: None,
    )

    calls = {
        record["native_session_id"]: record["tool_calls"][0]["capture_id"]
        for record in bundle["records"]
        if record.get("tool_calls")
    }
    assert calls == {"native-1": "capture-1", "native-2": "capture-2"}
    assert bundle["captures"]["capture-1"]["quality"] == "ambiguous"
    assert bundle["captures"]["capture-2"]["quality"] == "ambiguous"
