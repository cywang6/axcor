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

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime

import pytest

import workflow_controller.plugins.file_provenance.store as store_module
from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.store import LineAttribution, ProvenanceStore


def _context(tmp_path, capture_id: str) -> FileChangeContext:
    args = [
        capture_id,
        "agent",
        "session-1",
        "native-1",
        None,
        "claude_sdk",
        "test-model",
        "worker",
        "Write",
        f"tool-{capture_id}",
        tmp_path,
        {"file_path": "src/example.py"},
        datetime(2026, 8, 21, 9, 0, 0),
        {"prompt": "update example"},
        "agent-1",
    ]
    return FileChangeContext(*args)


def _outcome(*, incomplete: bool = False) -> FileChangeOutcome:
    args = [
        not incomplete,
        incomplete,
        {"status": "done"},
        (),
        datetime(2026, 8, 21, 9, 0, 1),
        incomplete,
    ]
    return FileChangeOutcome(*args)


def test_store_initializes_versioned_wal_database(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)

    assert store.root == tmp_path / ".workflow-controller" / "file-provenance"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    expected = {"captures", "active_captures", "capture_files", "file_state"}
    expected.update({"mutations", "conflicts", "commit_lines", "commits", "state"})
    assert expected <= tables


def test_store_migrates_schema_one_file_snapshots(tmp_path) -> None:
    root = tmp_path / ".workflow-controller" / "file-provenance"
    root.mkdir(parents=True)
    db_path = root / "provenance.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE captures (
                capture_id TEXT PRIMARY KEY,
                head_before TEXT NOT NULL DEFAULT '',
                head_after TEXT
            );
            CREATE TABLE capture_files (
                capture_id TEXT NOT NULL, stage TEXT NOT NULL,
                path TEXT NOT NULL, content_blob TEXT NOT NULL,
                PRIMARY KEY (capture_id, stage, path)
            );
            PRAGMA user_version=1;
        """)

    ProvenanceStore(tmp_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(capture_files)")}
        capture_columns = {row[1] for row in conn.execute("PRAGMA table_info(captures)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "attributions" in columns
        assert "committed_sha" in capture_columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_state'"
        ).fetchone()


def test_blob_storage_deduplicates_and_checks_integrity(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    content = b"print('evidence')\n"
    store.begin_capture(context)
    store.record_snapshot("capture-1", "before", {"src/example.py": content})
    store.record_snapshot("capture-1", "after", {"src/example.py": content})
    capture = store.get_capture("capture-1")
    digest = capture["snapshots"][0]["content_blob"]

    assert digest == hashlib.sha256(content).hexdigest()
    assert {item["content_blob"] for item in capture["snapshots"]} == {digest}
    assert store.read_snapshot("capture-1", "before", "src/example.py") == content

    blob_path = store.blobs / digest[:2] / digest[2:]
    blob_path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity check"):
        store.read_snapshot("capture-1", "before", "src/example.py")

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE capture_files SET content_blob='../provenance.db'")
    with pytest.raises(ValueError, match="blob digest"):
        store.read_snapshot("capture-1", "before", "src/example.py")


def test_list_captures_for_session_isolated_and_without_snapshots(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    first = _context(tmp_path, "capture-1")
    second = _context(tmp_path, "capture-2")
    second = FileChangeContext(
        second.capture_id,
        second.actor_kind,
        "session-2",
        second.native_session_id,
        second.parent_native_session_id,
        second.runtime,
        second.model,
        second.agent_name,
        second.tool_name,
        second.tool_use_id,
        second.cwd,
        second.tool_input,
        second.started_at,
        second.attributes,
        second.native_agent_id,
    )
    store.begin_capture(first)
    store.record_snapshot("capture-1", "before", {"src/example.py": b"old\n"})
    store.end_capture_window("capture-1")
    store.record_mutation(
        "capture-1",
        "src/example.py",
        before=b"old\n",
        after=b"new\n",
        additions=1,
        deletions=1,
    )
    store.finish_capture(first, _outcome())
    store.begin_capture(second)

    rows = store.list_captures_for_session("session-1")
    assert [row["capture_id"] for row in rows] == ["capture-1"]
    assert rows[0]["mutations"][0]["path"] == "src/example.py"
    assert "snapshots" not in rows[0]


def test_existing_blob_integrity_is_verified_once_per_store(tmp_path, monkeypatch) -> None:
    content = b"print('evidence')\n"
    first_store = ProvenanceStore(tmp_path)
    first_store.begin_capture(_context(tmp_path, "capture-1"))
    first_store.record_snapshot("capture-1", "before", {"src/example.py": content})
    checks = 0
    real_digest = getattr(store_module, "_file_digest")

    def tracked_digest(path):
        nonlocal checks
        checks += 1
        return real_digest(path)

    monkeypatch.setattr(store_module, "_file_digest", tracked_digest)
    store = ProvenanceStore(tmp_path)
    store.begin_capture(_context(tmp_path, "capture-2"))
    store.record_snapshot("capture-2", "before", {"src/example.py": content})
    store.record_snapshot("capture-2", "after", {"src/example.py": content})

    assert checks == 1


def test_blob_storage_fsyncs_parent_directory(tmp_path, monkeypatch) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    directory_fsyncs = []
    real_fsync = os.fsync

    def track_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)
    store.begin_capture(context)
    store.record_snapshot("capture-1", "before", {"src/example.py": b"evidence\n"})

    assert len(directory_fsyncs) == 1


def test_record_snapshot_batches_state_and_compacts_unknown(tmp_path, monkeypatch) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    known = b"known\n"
    unknown = b"first\nsecond\n"
    store.begin_capture(context)
    store.save_file_state(
        "known.py",
        known,
        [LineAttribution("agent", actor_id="seeded")],
        None,
    )
    connections = 0
    state_queries = 0
    connect = getattr(store, "_connect")

    @contextlib.contextmanager
    def tracked_connect():
        nonlocal connections, state_queries
        connections += 1
        with connect() as conn:

            def track_query(statement):
                nonlocal state_queries
                if "SELECT path,content_blob,attributions FROM file_state" in statement:
                    state_queries += 1

            conn.set_trace_callback(track_query)
            yield conn

    monkeypatch.setattr(store, "_connect", tracked_connect)
    files = {f"unknown-{index}.py": b"" for index in range(901)}
    files.update({"known.py": known, "unknown.py": unknown})
    store.record_snapshot(
        context.capture_id,
        "before",
        files,
    )

    assert connections == 1
    assert state_queries == 2
    with sqlite3.connect(store.db_path) as conn:
        rows = dict(conn.execute("SELECT path,attributions FROM capture_files"))
    assert json.loads(rows["known.py"])[0]["actor_id"] == "seeded"
    assert rows["unknown.py"] == "[]"

    snapshots = {
        item["path"]: item["attributions"]
        for item in store.get_capture(context.capture_id)["snapshots"]
    }
    assert snapshots["known.py"][0]["actor_id"] == "seeded"
    assert [item["actor_kind"] for item in snapshots["unknown.py"]] == [
        "unknown",
        "unknown",
    ]


def test_capture_lifecycle_persists_snapshots_and_mutation(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    before = b"old\n"
    after = b"old\nnew\n"

    assert store.begin_capture(context, head_before="parent") is False
    store.record_snapshot("capture-1", "before", {"src/example.py": before})
    store.record_snapshot("capture-1", "after", {"src/example.py": after})
    store.record_mutation(
        "capture-1",
        "src/example.py",
        before=before,
        after=after,
        additions=1,
        deletions=0,
    )
    assert not store.end_capture_window(context.capture_id)
    assert not store.finish_capture(context, _outcome(), head_after="parent")
    assert not store.finish_capture(
        context,
        _outcome(incomplete=True),
        head_after="different",
    )

    assert store.read_snapshot("capture-1", "before", "src/example.py") == before
    assert store.read_snapshot("capture-1", "after", "src/example.py") == after
    assert store.load_snapshot("capture-1", "before") == {"src/example.py": before}
    value = store.get_capture("capture-1")
    assert value is not None
    assert value["tool_input"] == {"file_path": "src/example.py"}
    assert value["tool_output"] == {"status": "done"}
    assert value["succeeded"] is True
    assert value["is_error"] is False
    assert value["incomplete"] is False
    assert value["conflicted"] is False
    assert value["head_before"] == "parent"
    assert value["head_after"] == "parent"
    assert value["mutations"][0]["additions"] == 1
    assert value["mutations"][0]["conflict"] is False
    assert len(value["snapshots"]) == 2


def test_overlapping_captures_mark_both_windows_conflicted(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    first = _context(tmp_path, "capture-1")
    second = _context(tmp_path, "capture-2")

    assert store.begin_capture(first, head_before="same") is False
    assert store.begin_capture(second, head_before="same") is True
    assert store.end_capture_window(first.capture_id) is True
    assert store.end_capture_window(second.capture_id) is True
    assert store.finish_capture(first, _outcome(), head_after="same") is True
    assert store.finish_capture(second, _outcome(), head_after="same") is True

    assert store.get_capture("capture-1")["conflicted"] is True
    assert store.get_capture("capture-2")["conflicted"] is True
    assert store.get_capture("capture-1")["conflicts"][0]["kind"] == "overlapping_capture"
    assert store.get_capture("capture-2")["conflicts"][0]["kind"] == "overlapping_capture"


def test_crossing_a_commit_records_conflict(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    store.begin_capture(context, head_before="before")

    assert not store.end_capture_window(context.capture_id)
    assert store.finish_capture(context, _outcome(), head_after="after")
    conflict = store.get_capture("capture-1")["conflicts"][0]
    assert conflict["kind"] == "capture_crossed_commit"
    assert conflict["detail"] == "capture capture-1 crossed a commit boundary"


def test_stale_window_is_removed_before_overlap_detection(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    stale = _context(tmp_path, "stale")
    current = _context(tmp_path, "current")
    store.begin_capture(stale)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE active_captures SET owner_pid=-1 WHERE capture_id='stale'")

    assert store.begin_capture(current) is False
    stale_value = store.get_capture("stale")
    assert stale_value["conflicted"] is True
    assert stale_value["incomplete"] is True
    assert stale_value["finished_at"] is not None
    assert stale_value["conflicts"][0]["kind"] == "stale_active_capture"


@pytest.mark.parametrize(
    "path",
    ["", ".", "/absolute", "../outside", "src/../outside", "src\\file.py", "src//file.py"],
)
def test_snapshot_paths_must_be_canonical_and_relative(tmp_path, path) -> None:
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-1")
    store.begin_capture(context)

    with pytest.raises(ValueError, match="worktree-relative path"):
        store.record_snapshot("capture-1", "before", {path: b"content"})


def _seed_committed_agent_lines(tmp_path) -> tuple:
    """铺已完成 capture 与定稿 commit（src/example.py 的 unknown/agent 两行）。"""
    target = tmp_path / "src" / "example.py"
    target.parent.mkdir()
    content = b"unknown\nagent\n"
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    store = ProvenanceStore(tmp_path)
    context = _context(tmp_path, "capture-query")
    agent = LineAttribution(
        "agent",
        actor_id="s_0123456789abcd::t_0123456789abcd",
        capture_id=context.capture_id,
        session_id=context.workflow_controller_session_id,
        native_agent_id=context.native_agent_id,
    )
    attributions = [LineAttribution("unknown"), agent]
    store.begin_capture(context)
    store.save_file_state("src/example.py", content, attributions, context.capture_id)
    store.record_snapshot(context.capture_id, "after", {"src/example.py": content})
    store.record_mutation(
        context.capture_id,
        "src/example.py",
        before=b"unknown\n",
        after=content,
        additions=1,
        deletions=0,
    )
    store.end_capture_window(context.capture_id)
    store.finish_capture(context, _outcome())
    store.record_commit_lines("commit-1", "src/example.py", ["unknown", "agent"], attributions)
    store.finalize_commit("commit-1", "", 1, 1, 1)
    return target, digest, store, context


def test_queries_human_claim_and_session_redaction(tmp_path) -> None:
    target, digest, store, context = _seed_committed_agent_lines(tmp_path)

    assert store.list_files() == ["src/example.py"]
    assert store.list_files("commit-1") == ["src/example.py"]
    assert store.current_content_hash("src/example.py") == digest
    assert store.blame("src/example.py")[1]["actor_kind"] == "agent"
    assert store.blame("src/example.py", "commit-1")[1]["content"] == "agent"
    assert store.get_commit_metrics("commit-1")["ai_lines"] == 1
    assert store.list_commits()[0]["commit_sha"] == "commit-1"

    assert (
        store.claim_human(
            "src/example.py",
            1,
            2,
            "Test User <test@example.com>",
            digest,
        )
        == 1
    )
    blame = store.blame("src/example.py")
    assert blame[0]["actor_kind"] == "human"
    assert blame[1]["actor_kind"] == "agent"
    snapshot = store.get_capture_file(context.capture_id, "after", "src/example.py")
    assert snapshot is not None
    assert snapshot["attributions"][0]["actor_kind"] == "human"

    store.redact_session("session-1")
    capture = store.get_capture(context.capture_id)
    assert capture is not None
    assert capture["session_id"] is None
    assert capture["native_session_id"] is None
    assert capture["tool_input"] == {}
    assert store.blame("src/example.py")[1]["session_id"] is None
    assert store.blame("src/example.py", "commit-1")[1]["session_id"] is None

    target.write_text("changed\n")
    with pytest.raises(ValueError, match="working tree file changed"):
        store.claim_human(
            "src/example.py",
            1,
            1,
            "Test User <test@example.com>",
            digest,
        )
