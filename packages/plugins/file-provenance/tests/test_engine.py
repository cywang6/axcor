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

from dataclasses import replace
from datetime import datetime

import pytest

from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.engine import ProvenanceEngine, move_attributions
from workflow_controller.plugins.file_provenance.store import LineAttribution, ProvenanceStore


def _context(tmp_path, capture_id: str = "capture-1") -> FileChangeContext:
    return FileChangeContext(
        capture_id=capture_id,
        actor_kind="agent",
        workflow_controller_session_id="session-1",
        native_session_id="native-1",
        parent_native_session_id=None,
        runtime="claude_sdk",
        model="model-1",
        agent_name="worker",
        tool_name="Write",
        tool_use_id=f"tool-{capture_id}",
        cwd=tmp_path,
        tool_input={"file_path": "sample.py"},
        started_at=datetime(2026, 8, 22, 9, 0, 0),
    )


def _outcome(*, incomplete: bool = False) -> FileChangeOutcome:
    return FileChangeOutcome(
        succeeded=not incomplete,
        is_error=incomplete,
        tool_output={"status": "done"},
        path_hints=(),
        finished_at=datetime(2026, 8, 22, 9, 0, 1),
        incomplete=incomplete,
    )


def test_move_attributions_preserves_equal_lines() -> None:
    old = [
        LineAttribution("human", actor_id="human-1"),
        LineAttribution("agent", actor_id="agent-old"),
    ]
    actor = LineAttribution("agent", actor_id="agent-new")

    values, additions, deletions = move_attributions(
        ["one", "two"],
        ["zero", "one", "two changed"],
        old,
        actor,
    )

    assert [item.actor_id for item in values] == ["agent-new", "human-1", "agent-new"]
    assert (additions, deletions) == (2, 1)


def test_move_attributions_degrades_ambiguous_duplicate_lines() -> None:
    old = [
        LineAttribution("agent", actor_id="agent-1"),
        LineAttribution("human", actor_id="human-1"),
    ]
    actor = LineAttribution("agent", actor_id="agent-new", capture_id="capture-new")

    values, additions, deletions = move_attributions(
        ["same", "same"],
        ["same"],
        old,
        actor,
    )

    assert (additions, deletions) == (0, 1)
    assert values == [
        LineAttribution(
            "unknown",
            capture_id="capture-new",
            conflict=True,
        )
    ]


async def test_engine_records_snapshots_mutation_and_file_state(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("old\n")
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)

    await engine.before(context)
    assert getattr(engine, "_prepared_captures") == {context.capture_id}
    assert store.load_snapshot(context.capture_id, "before") == {"sample.py": b"old\n"}
    target.write_text("old\nnew\n")
    mutations = await engine.after(context, _outcome())

    assert [(item.path, item.additions, item.deletions) for item in mutations] == [
        ("sample.py", 1, 0)
    ]
    values = store.load_file_state("sample.py", target.read_bytes())
    assert [item.actor_kind for item in values] == ["unknown", "agent"]
    assert values[1].session_id == "session-1"
    capture = store.get_capture(context.capture_id)
    assert capture is not None
    assert capture["finished_at"] == _outcome().finished_at.isoformat()
    assert capture["mutations"][0]["after_blob"]
    assert {item["stage"] for item in capture["snapshots"]} == {"before", "after"}
    assert not getattr(engine, "_prepared_captures")


async def test_overlapping_engines_degrade_new_lines(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("base\n")
    first = ProvenanceEngine(tmp_path, ProvenanceStore(tmp_path))
    second = ProvenanceEngine(tmp_path, ProvenanceStore(tmp_path))
    first_context = _context(tmp_path, "first")
    second_context = _context(tmp_path, "second")

    await first.before(first_context)
    await second.before(second_context)
    target.write_text("base\nambiguous\n")
    await first.after(first_context, _outcome())
    await second.after(second_context, _outcome())

    values = first.store.load_file_state("sample.py", target.read_bytes())
    assert values[1].actor_kind == "unknown"
    assert values[1].conflict is True
    assert first.store.get_capture("first")["conflicted"] == 1
    assert second.store.get_capture("second")["conflicted"] == 1


async def test_incomplete_outcome_degrades_new_lines(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("base\n")
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)

    await engine.before(context)
    target.write_text("base\ninterrupted\n")
    await engine.after(context, _outcome(incomplete=True))

    values = store.load_file_state("sample.py", target.read_bytes())
    capture = store.get_capture(context.capture_id)
    assert values[1].actor_kind == "unknown"
    assert values[1].conflict is True
    assert capture["incomplete"] == 1
    assert capture["conflicted"] == 1


async def test_native_agent_id_distinguishes_subagents(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("base\n")
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    first = replace(_context(tmp_path, "first"), native_agent_id="agent-one")
    second = replace(_context(tmp_path, "second"), native_agent_id="agent-two")

    await engine.before(first)
    target.write_text("base\none\n")
    await engine.after(first, _outcome())
    await engine.before(second)
    target.write_text("base\none\ntwo\n")
    await engine.after(second, _outcome())

    values = store.load_file_state("sample.py", target.read_bytes())
    assert values[1].actor_id != values[2].actor_id
    assert values[1].native_agent_id == "agent-one"
    assert values[2].native_agent_id == "agent-two"


async def test_missing_before_snapshot_closes_capture_incomplete(tmp_path) -> None:
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)
    store.begin_capture(context)

    assert await engine.after(context, _outcome()) == []

    capture = store.get_capture(context.capture_id)
    assert capture is not None
    assert capture["incomplete"] == 1
    assert capture["conflicts"][0]["kind"] == "missing_before_snapshot"


async def test_failed_before_scan_abandons_capture(tmp_path, monkeypatch) -> None:
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)

    def fail_scan() -> dict[str, bytes]:
        raise OSError("scan failed")

    monkeypatch.setattr(engine, "scan", fail_scan)
    with pytest.raises(OSError, match="scan failed"):
        await engine.before(context)

    assert store.get_capture(context.capture_id) is None


async def test_failed_after_scan_closes_capture_incomplete(tmp_path, monkeypatch) -> None:
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)
    await engine.before(context)

    def fail_scan() -> dict[str, bytes]:
        raise OSError("scan failed")

    monkeypatch.setattr(engine, "scan", fail_scan)
    with pytest.raises(OSError, match="scan failed"):
        await engine.after(context, _outcome())

    capture = store.get_capture(context.capture_id)
    assert capture["incomplete"] == 1
    assert capture["conflicted"] == 1
    assert capture["conflicts"][0]["kind"] == "after_scan_failed"
    assert capture["conflicts"][0]["detail"] == (
        f"capture {context.capture_id} failed while scanning the worktree"
    )


async def test_failed_before_snapshot_read_reports_evidence_store_stage(
    tmp_path,
    monkeypatch,
) -> None:
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path)
    await engine.before(context)

    def fail_load_snapshot(_capture_id, _stage) -> dict[str, bytes]:
        raise OSError("snapshot read failed")

    monkeypatch.setattr(store, "load_snapshot", fail_load_snapshot)
    with pytest.raises(OSError, match="snapshot read failed"):
        await engine.after(context, _outcome())

    capture = store.get_capture(context.capture_id)
    assert capture["incomplete"] == 1
    assert capture["conflicted"] == 1
    assert capture["conflicts"][0]["kind"] == "before_snapshot_read_failed"
    assert capture["conflicts"][0]["detail"] == (
        f"capture {context.capture_id} failed while reading the before snapshot "
        "from the evidence store"
    )


async def test_unique_rename_preserves_attribution(tmp_path) -> None:
    original = tmp_path / "old.py"
    original.write_text("generated\n")
    store = ProvenanceStore(tmp_path)
    seeded = LineAttribution("agent", actor_id="original-agent")
    store.save_file_state("old.py", original.read_bytes(), [seeded], None)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path, "rename")

    await engine.before(context)
    original.rename(tmp_path / "new.py")
    await engine.after(context, _outcome())

    content = (tmp_path / "new.py").read_bytes()
    assert store.load_file_state("new.py", content) == [seeded]


async def test_ambiguous_identical_rename_is_conflicted(tmp_path) -> None:
    for name in ("first.py", "second.py"):
        (tmp_path / name).write_text("same\n")
    store = ProvenanceStore(tmp_path)
    engine = ProvenanceEngine(tmp_path, store)
    context = _context(tmp_path, "rename")

    await engine.before(context)
    (tmp_path / "first.py").unlink()
    (tmp_path / "second.py").rename(tmp_path / "new.py")
    mutations = await engine.after(context, _outcome())

    mutation = next(item for item in mutations if item.path == "new.py")
    values = store.load_file_state("new.py", (tmp_path / "new.py").read_bytes())
    assert mutation.conflict is True
    assert values[0].actor_kind == "unknown"


def test_scan_excludes_internal_ignored_symlink_and_large_files(tmp_path) -> None:
    (tmp_path / ".workflow-controller").mkdir()
    (tmp_path / ".workflow-controller" / "internal").write_text("ignored")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "dependency").write_text("ignored")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dependency").write_text("ignored")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact").write_text("ignored")
    (tmp_path / "large.txt").write_text("12345")
    (tmp_path / "tracked.txt").write_text("yes")
    (tmp_path / "linked.txt").symlink_to(tmp_path / "tracked.txt")
    engine = ProvenanceEngine(
        tmp_path,
        ProvenanceStore(tmp_path),
        ignore=["vendor"],
        max_file_bytes=4,
    )

    assert engine.scan() == {"tracked.txt": b"yes"}


def test_scan_prunes_ignored_directories_before_descending(tmp_path, monkeypatch) -> None:
    engine = ProvenanceEngine(tmp_path, ProvenanceStore(tmp_path), ignore=["vendor"])

    def walk(_root):
        dirnames = [".git", ".workflow-controller", ".venv", "build", "vendor", "included"]
        yield tmp_path, dirnames, []
        assert dirnames == ["included"]

    monkeypatch.setattr("workflow_controller.plugins.file_provenance.engine.os.walk", walk)

    assert engine.scan() == {}
