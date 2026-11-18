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

import json
import sqlite3
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from workflow_controller.__main__ import cli
from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.offline_export import (
    export_opencode_audit_evidence,
    read_opencode_audit_source,
    write_audit_bundle,
)
from workflow_controller.plugins.file_provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path


_TASK_PART_DATA = json.dumps(
    {
        "type": "tool",
        "callID": "call-task",
        "tool": "task",
        "state": {
            "status": "completed",
            "input": {"prompt": "inspect"},
            "output": "done",
            "metadata": {"sessionId": "child"},
            "time": {"start": 2000, "end": 7000},
        },
    }
)

_EDIT_PART_DATA = json.dumps(
    {
        "type": "tool",
        "callID": "call-edit",
        "tool": "edit",
        "state": {
            "status": "completed",
            "input": {"file_path": "src/a.py"},
            "output": "ok",
            "time": {"start": 5000, "end": 6000},
        },
    }
)


def _opencode_db(path: Path, project: Path) -> Path:
    db_path = path / "opencode.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL,
                title TEXT, version TEXT, agent TEXT, model TEXT,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, data TEXT NOT NULL
            );
        """)
        _insert_sessions(conn, project)
        _insert_messages(conn)
        _insert_root_parts(conn)
        _insert_child_parts(conn)
    return db_path


def _insert_sessions(conn: sqlite3.Connection, project: Path) -> None:
    """session 表两行：root（build）与其子会话 child（explore）。"""
    conn.executemany(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "root",
                None,
                str(project),
                "Root",
                "1.18.23",
                "build",
                json.dumps({"providerID": "test", "modelID": "model"}),
                1000,
                9000,
            ),
            (
                "child",
                "root",
                str(project),
                "Child",
                "1.18.23",
                "explore",
                None,
                3000,
                8000,
            ),
        ],
    )


def _insert_messages(conn: sqlite3.Connection) -> None:
    """message 表：root 的三条 user 与 root/child 各一条 assistant。"""
    conn.executemany(
        "INSERT INTO message VALUES (?,?,?,?)",
        [
            ("m-user", "root", 1000, json.dumps({"role": "user", "time": {"created": 1000}})),
            (
                "m-user-followup",
                "root",
                1500,
                json.dumps({"role": "user", "time": {"created": 1500}}),
            ),
            (
                "m-user-legacy",
                "root",
                1750,
                json.dumps({"role": "user", "content": "legacy follow-up"}),
            ),
            ("m-root", "root", 2000, json.dumps({"role": "assistant", "agent": "build"})),
            ("m-child", "child", 4000, json.dumps({"role": "assistant", "agent": "explore"})),
        ],
    )


def _insert_root_parts(conn: sqlite3.Connection) -> None:
    """root 会话的 part 行：user 提问、追问与 task 子会话调用。"""
    conn.executemany(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        [
            (
                "p-user",
                "m-user",
                "root",
                1000,
                json.dumps({"type": "text", "text": "change it"}),
            ),
            (
                "p-user-followup",
                "m-user-followup",
                "root",
                1500,
                json.dumps({"type": "text", "text": "follow up"}),
            ),
            (
                "p-task",
                "m-root",
                "root",
                2000,
                _TASK_PART_DATA,
            ),
        ],
    )


def _insert_child_parts(conn: sqlite3.Connection) -> None:
    """child 会话的 part 行：reasoning、text、edit 工具与 step-finish。"""
    conn.executemany(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        [
            (
                "p-reason",
                "m-child",
                "child",
                4000,
                json.dumps({"type": "reasoning", "text": "inspect first"}),
            ),
            (
                "p-text",
                "m-child",
                "child",
                4500,
                json.dumps({"type": "text", "text": "editing now"}),
            ),
            (
                "p-edit",
                "m-child",
                "child",
                5000,
                _EDIT_PART_DATA,
            ),
            (
                "p-finish",
                "m-child",
                "child",
                6500,
                json.dumps({"type": "step-finish", "reason": "stop"}),
            ),
        ],
    )


def _capture(project: Path) -> None:
    store = ProvenanceStore(project)
    context = FileChangeContext(
        capture_id="capture-edit",
        actor_kind="agent",
        workflow_controller_session_id=None,
        native_session_id="child",
        parent_native_session_id=None,
        runtime="opencode",
        model="test/model",
        agent_name="explore",
        tool_name="edit",
        tool_use_id="call-edit",
        cwd=project,
        tool_input={"file_path": "src/a.py"},
        started_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        attributes={"source": "opencode-plugin"},
        native_agent_id="explore",
    )
    before = b"old\n"
    after = b"new\n"
    store.begin_capture(context)
    store.record_snapshot("capture-edit", "before", {"src/a.py": before})
    store.record_snapshot("capture-edit", "after", {"src/a.py": after})
    store.end_capture_window("capture-edit")
    store.record_mutation(
        "capture-edit",
        "src/a.py",
        before=before,
        after=after,
        additions=1,
        deletions=1,
    )
    store.finish_capture(
        context,
        FileChangeOutcome(
            succeeded=True,
            is_error=False,
            tool_output="ok",
            path_hints=(),
            finished_at=datetime(2026, 8, 29, 10, 0, 1, tzinfo=UTC),
        ),
    )


def test_offline_export_recurses_children_and_joins_native_capture(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    _capture(project)

    bundle = export_opencode_audit_evidence(project, "root", db_path)

    assert bundle["producer"] == "workflow-controller-opencode-offline"
    assert bundle["coverage"]["session_count"] == 2
    assert bundle["coverage"]["event_history_included"] is False
    assert [item["id"] for item in bundle["session"]["native_sessions"]] == ["root", "child"]
    user_records = [item for item in bundle["records"] if item.get("role") == "user"]
    assert [item["text"] for item in user_records] == [
        "change it",
        "follow up",
        "legacy follow-up",
    ]
    assert user_records[1]["event_id"] == "part:p-user-followup:message"
    assert user_records[1]["native_session_id"] == "root"
    assert any(
        "<thinking>inspect first</thinking>" in item.get("text", "") for item in bundle["records"]
    )
    child_records = [item for item in bundle["records"] if item.get("native_session_id") == "child"]
    assert all(item.get("parent_tool_use_id") == "call-task" for item in child_records)
    assert next(
        i for i, item in enumerate(child_records) if "inspect first" in item.get("text", "")
    ) < next(
        i
        for i, item in enumerate(child_records)
        if item.get("tool_calls") and item["tool_calls"][0]["id"] == "call-edit"
    )
    edit = next(
        item
        for item in bundle["records"]
        if item.get("tool_calls") and item["tool_calls"][0]["id"] == "call-edit"
    )
    assert edit["tool_calls"][0]["capture_id"] == "capture-edit"
    assert edit["native_session_id"] == "child"
    assert edit["parent_native_session_id"] == "root"
    assert edit["parent_tool_use_id"] == "call-task"
    assert bundle["captures"]["capture-edit"]["quality"] == "observed"
    assert {value["content"] for value in bundle["blobs"].values()} == {"old\n", "new\n"}


def test_read_source_rejects_unknown_session_and_project_mismatch(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    db_path = _opencode_db(tmp_path, project)

    with pytest.raises(KeyError, match="session not found"):
        read_opencode_audit_source(db_path, "missing", project)
    with pytest.raises(ValueError, match="outside project"):
        read_opencode_audit_source(db_path, "root", other)


def _invoke_export_cli(project: Path, session: str, db_path: Path, output: Path):
    """以 CLI 导出审计证据（click 测试 runner）。"""
    return CliRunner().invoke(
        cli,
        [
            "provenance",
            "export-audit-evidence",
            "--project",
            str(project),
            "--session",
            session,
            "--opencode-db",
            str(db_path),
            "--output",
            str(output),
        ],
    )


def test_read_source_rejects_incompatible_schema_and_invalid_json(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    bad_schema = tmp_path / "schema.db"
    with sqlite3.connect(bad_schema) as conn:
        conn.execute("CREATE TABLE session (id TEXT)")
    with pytest.raises(ValueError, match="missing tables"):
        read_opencode_audit_source(bad_schema, "root", project)

    db_path = _opencode_db(tmp_path, project)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE message SET data='not-json' WHERE id='m-user'")
    with pytest.raises(ValueError, match="invalid JSON"):
        read_opencode_audit_source(db_path, "root", project)


def test_readonly_store_requires_existing_database_and_cli_writes_atomically(tmp_path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ProvenanceStore(missing, readonly=True)
    assert not (missing / ".workflow-controller").exists()

    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    _capture(project)
    output = tmp_path / "evidence.json"
    result = _invoke_export_cli(project, "root", db_path, output)
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["format"] == "sift-records"

    refused = _invoke_export_cli(project, "root", db_path, output)
    assert refused.exit_code != 0
    assert "output already exists" in refused.output

    bundle = {"format": "replacement"}
    write_audit_bundle(bundle, output, force=True)
    assert json.loads(output.read_text()) == bundle


def test_offline_export_rejects_corrupted_provenance_blob(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    _capture(project)
    store = ProvenanceStore(project, readonly=True)
    capture = store.list_captures_for_native_sessions(["child"])[0]
    digest = capture["mutations"][0]["after_blob"]
    blob_path = store.blobs / digest[:2] / digest[2:]
    blob_path.write_bytes(b"corrupted")

    with pytest.raises(ValueError, match="integrity check"):
        export_opencode_audit_evidence(project, "root", db_path)


def test_offline_export_wraps_corrupted_provenance_database(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    provenance_dir = project / ".workflow-controller" / "file-provenance"
    provenance_dir.mkdir(parents=True)
    (provenance_dir / "provenance.db").write_bytes(b"not a sqlite database")

    with pytest.raises(ValueError, match="failed to read provenance database"):
        export_opencode_audit_evidence(project, "root", db_path)


def test_write_bundle_falls_back_without_hard_links(tmp_path) -> None:
    output = tmp_path / "evidence.json"
    with patch(
        "workflow_controller.plugins.file_provenance.offline_export.os.link",
        side_effect=OSError("unsupported"),
    ):
        write_audit_bundle({"format": "sift-records"}, output)

    assert json.loads(output.read_text()) == {"format": "sift-records"}


def test_write_bundle_fallback_still_refuses_existing_target(tmp_path) -> None:
    output = tmp_path / "evidence.json"
    output.write_text("existing")
    with (
        patch(
            "workflow_controller.plugins.file_provenance.offline_export.os.link",
            side_effect=OSError("unsupported"),
        ),
        pytest.raises(FileExistsError, match="output already exists"),
    ):
        write_audit_bundle({"format": "sift-records"}, output)


def test_offline_export_warns_for_unincluded_metadata_child(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE part SET data=? WHERE id='p-task'",
            (
                json.dumps(
                    {
                        "type": "tool",
                        "callID": "call-task",
                        "tool": "task",
                        "state": {
                            "status": "completed",
                            "input": {"prompt": "inspect"},
                            "output": "done",
                            "metadata": {"sessionId": "missing-child"},
                        },
                    }
                ),
            ),
        )

    source = read_opencode_audit_source(db_path, "root", project)

    assert any("missing-child" in warning for warning in source.warnings)


def test_cli_formats_key_error_without_repr_quotes(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    result = _invoke_export_cli(project, "missing", db_path, tmp_path / "output.json")

    assert result.exit_code != 0
    assert "Error: OpenCode session not found: missing" in result.output
    assert "Error: 'OpenCode session not found: missing'" not in result.output


def test_write_bundle_publishes_consistent_file_mode(tmp_path) -> None:
    hardlink_output = tmp_path / "hardlink.json"
    write_audit_bundle({"format": "sift-records"}, hardlink_output)
    assert stat.S_IMODE(hardlink_output.stat().st_mode) == 0o644

    fallback_output = tmp_path / "fallback.json"
    with patch(
        "workflow_controller.plugins.file_provenance.offline_export.os.link",
        side_effect=OSError("unsupported"),
    ):
        write_audit_bundle({"format": "sift-records"}, fallback_output)
    assert stat.S_IMODE(fallback_output.stat().st_mode) == 0o644

    force_output = tmp_path / "force.json"
    force_output.write_text("stale")
    write_audit_bundle({"format": "sift-records"}, force_output, force=True)
    assert stat.S_IMODE(force_output.stat().st_mode) == 0o644


def test_child_session_regex_fallback_only_applies_to_task_tool() -> None:
    from workflow_controller.plugins.file_provenance.offline_export import _child_session_id

    envelope = '<task id="ses_child" state="completed"><task_result>ok</task_result></task>'
    assert _child_session_id({"output": envelope}, {}, "task") == "ses_child"
    # A non-task tool whose output happens to contain the envelope must not be
    # misattributed as a parent tool-use.
    assert _child_session_id({"output": envelope}, {}, "read") is None
    assert _child_session_id({"output": "log line\ntask_id: ses_wrong"}, {}, "task") is None
    # Metadata always wins, regardless of tool name.
    assert _child_session_id({}, {"metadata": {"sessionId": "ses_meta"}}, "read") == "ses_meta"


def test_offline_export_ignores_task_envelope_in_non_task_tool_output(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    envelope = '<task id="ses_phantom" state="completed"><task_result>ok</task_result></task>'
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO part VALUES ('p-read','m-root','root',2100,?)",
            (
                json.dumps(
                    {
                        "type": "tool",
                        "callID": "call-read",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"path": "README.md"},
                            "output": envelope,
                            "time": {"start": 2050, "end": 2100},
                        },
                    }
                ),
            ),
        )

    source = read_opencode_audit_source(db_path, "root", project)

    # The read tool must not be recorded as a parent tool-use for ses_phantom.
    assert "ses_phantom" not in source.parent_tool_by_session
    assert not any("ses_phantom" in warning for warning in source.warnings)


def test_offline_export_warns_when_timestamp_defaults_to_epoch(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = _opencode_db(tmp_path, project)
    _capture(project)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE part SET data=? WHERE id='p-finish'",
            (
                json.dumps(
                    {
                        "type": "step-finish",
                        "reason": "stop",
                        "time": {"start": {"invalid": True}},
                    }
                ),
            ),
        )

    bundle = export_opencode_audit_evidence(project, "root", db_path)

    assert any(
        "part p-finish start time is missing or invalid" in warning
        for warning in bundle["coverage"]["warnings"]
    )
