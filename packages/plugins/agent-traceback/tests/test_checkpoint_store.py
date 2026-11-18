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

"""CheckpointStore 单元测试（设计 agent-traceback §5.2）。

检查点数据结构 + DDL + CRUD 全归插件 own（核心 EventStore 不再含 checkpoint 概念）。
表物理上仍留 ``.workflow-controller/events.db``（与 events 同库，event_rowid 双向可查）；本测试
直接用 :class:`CheckpointStore` 操作。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from workflow_controller.plugins.agent_traceback.checkpoint_store import Checkpoint, CheckpointStore
from workflow_controller.session.events import Event, EventType

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def cp_store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(tmp_path / "test.db")


def _make_cp(checkpoint_id: str = "cp_001", session_id: str = "s1") -> Checkpoint:
    return Checkpoint(
        checkpoint_id=checkpoint_id,
        session_id=session_id,
        iteration=2,
        agent_id="ses_sub1",
        agent_type="developer",
        trigger="subagent_stop",
        git_ref="abc1234",
        event_rowid=42,
        main_part_id="prt_9",
        parent_checkpoint_id=None,
        created_at="2026-07-13T00:00:00",
        file_count=5,
        byte_size=1024,
        status="ok",
    )


@pytest.mark.asyncio
async def test_store_and_get_checkpoint(cp_store: CheckpointStore) -> None:
    cp = _make_cp()
    await cp_store.store(cp)
    got = await cp_store.get("cp_001")
    assert got is not None
    assert got.checkpoint_id == "cp_001"
    assert got.session_id == "s1"
    assert got.iteration == 2
    assert got.agent_id == "ses_sub1"
    assert got.git_ref == "abc1234"
    assert got.event_rowid == 42
    assert got.main_part_id == "prt_9"
    assert got.file_count == 5
    assert got.byte_size == 1024


@pytest.mark.asyncio
async def test_get_checkpoint_missing_returns_none(cp_store: CheckpointStore) -> None:
    assert await cp_store.get("cp_zzz") is None


def _seq_cp(
    session_id: str,
    n: int,
    agent_id: str,
    git_ref: str,
    event_rowid: int,
    created_at: str,
    *,
    parent: str | None = None,
) -> Checkpoint:
    """按序号构造检查点（id/iteration/file_count/byte_size 随 n 递增）。"""
    return Checkpoint(
        checkpoint_id=f"cp_{session_id}_{n:03d}",
        session_id=session_id,
        iteration=n,
        agent_id=agent_id,
        agent_type="t",
        trigger="subagent_stop",
        git_ref=git_ref,
        event_rowid=event_rowid,
        main_part_id=None,
        parent_checkpoint_id=parent,
        created_at=created_at,
        file_count=n,
        byte_size=10 * n,
    )


@pytest.mark.asyncio
async def test_list_checkpoints_ordered_by_created_at(cp_store: CheckpointStore) -> None:
    # checkpoint_id is the PRIMARY KEY → must be globally unique, so per-session
    # sequences are prefixed with the session id.
    cp1 = _seq_cp("s1", 1, "a", "r1", 1, "2026-07-13T01:00:00")
    cp2 = _seq_cp("s1", 2, "b", "r2", 2, "2026-07-13T02:00:00", parent="cp_s1_001")
    cp3 = _seq_cp("s2", 1, "c", "r3", 3, "2026-07-13T03:00:00")
    await cp_store.store(cp2)
    await cp_store.store(cp1)
    await cp_store.store(cp3)

    s1_cps = await cp_store.list("s1")
    assert [c.checkpoint_id for c in s1_cps] == ["cp_s1_001", "cp_s1_002"]
    assert s1_cps[1].parent_checkpoint_id == "cp_s1_001"
    s2_cps = await cp_store.list("s2")
    assert [c.checkpoint_id for c in s2_cps] == ["cp_s2_001"]


@pytest.mark.asyncio
async def test_store_checkpoint_replaces_on_same_id(cp_store: CheckpointStore) -> None:
    await cp_store.store(_make_cp("cp_uniq_001"))
    cp_updated = Checkpoint(
        checkpoint_id="cp_uniq_001",
        session_id="s1",
        iteration=9,
        agent_id="z",
        agent_type="architect",
        trigger="subagent_stop",
        git_ref="r2",
        event_rowid=99,
        main_part_id=None,
        parent_checkpoint_id=None,
        created_at="2026-07-14T00:00:00",
        file_count=7,
        byte_size=2048,
    )
    await cp_store.store(cp_updated)
    got = await cp_store.get("cp_uniq_001")
    assert got is not None and got.git_ref == "r2" and got.iteration == 9


@pytest.mark.asyncio
async def test_delete_for_session_clears_only_that_session(
    cp_store: CheckpointStore,
) -> None:
    """delete_for_session 清该 session 的 checkpoints 行，其他 session 不受影响。"""
    await cp_store.store(_make_cp("cp_sA_001", "s_a"))
    await cp_store.store(
        Checkpoint(
            checkpoint_id="cp_sB_001",
            session_id="s_b",
            iteration=1,
            agent_id="",
            agent_type="",
            trigger="subagent_stop",
            git_ref="r",
            event_rowid=None,
            main_part_id=None,
            parent_checkpoint_id=None,
            created_at="2026-07-13T00:00:00",
            file_count=0,
            byte_size=0,
        )
    )
    cp_store.delete_for_session("s_a")
    assert await cp_store.get("cp_sA_001") is None
    # other session's checkpoint untouched
    assert await cp_store.get("cp_sB_001") is not None


@pytest.mark.asyncio
async def test_get_latest_subagent_stop_rowid(tmp_path: Path) -> None:
    """Best-effort rowid link: store SUBAGENT_STOP events, query latest rowid.

    The events table is core (owned by EventStore); CheckpointStore queries it
    over the same db. Seed events via EventStore, then query via CheckpointStore.
    """
    from workflow_controller.store.event_store import EventStore

    db_path = tmp_path / "test.db"
    event_store = EventStore(db_path)
    # Mix start/progress/stop so only SUBAGENT_STOP rows are matched.
    seeds = [
        (EventType.SUBAGENT_START, "ses_a"),
        (EventType.SUBAGENT_PROGRESS, "ses_a"),
        (EventType.SUBAGENT_STOP, "ses_a"),
        (EventType.SUBAGENT_STOP, "ses_b"),
        (EventType.SUBAGENT_STOP, "ses_a"),
    ]
    for et, agent in seeds:
        await event_store.store(
            Event.create(
                session_id="s1",
                iteration=0,
                event_type=et,
                data={"agent_id": agent, "agent_type": "t"},
            )
        )
    store = CheckpointStore(db_path)
    # ses_a: two stop events; the latest one's rowid should be returned.
    rowid = store.get_latest_subagent_stop_rowid_sync("s1", "ses_a")
    assert rowid is not None
    # Verify it points at a subagent_stop event for ses_a
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT session_id, event_type, data FROM events WHERE rowid = ?",
            (rowid,),
        ).fetchone()
    assert row[0] == "s1"
    assert row[1] == "subagent_stop"
    data = json.loads(row[2])
    assert data["agent_id"] == "ses_a"


def test_get_latest_subagent_stop_rowid_missing(cp_store: CheckpointStore) -> None:
    """No events table / no matching stop event → None (best-effort, no raise)."""
    assert cp_store.get_latest_subagent_stop_rowid_sync("s1", "ses_zzz") is None
    assert cp_store.get_latest_subagent_stop_rowid_sync("s1", "") is None


def test_checkpoints_table_created(cp_store: CheckpointStore) -> None:
    """checkpoints table + indexes exist after init."""
    with sqlite3.connect(str(cp_store.db_path)) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "checkpoints" in tables


@pytest.mark.asyncio
async def test_inner_git_column_roundtrip(cp_store: CheckpointStore) -> None:
    """inner_git 新列读写往返（captured / skipped_size / 空串默认）。"""
    await cp_store.store(_make_cp("cp_ig_001"))
    await cp_store.get("cp_ig_001")
    cp = _make_cp("cp_ig_002")
    cp.inner_git = "captured"
    await cp_store.store(cp)
    got = await cp_store.get("cp_ig_002")
    assert got is not None and got.inner_git == "captured"
    # 默认空串（旧检查点未知）
    cp2 = _make_cp("cp_ig_003")
    await cp_store.store(cp2)
    got2 = await cp_store.get("cp_ig_003")
    assert got2 is not None and got2.inner_git == ""
    # list 路径同样带出
    cps = await cp_store.list("s1")
    ig = {c.checkpoint_id: c.inner_git for c in cps}
    assert ig["cp_ig_002"] == "captured" and ig["cp_ig_003"] == ""


def test_inner_git_column_migrated_from_old_schema(tmp_path: Path) -> None:
    """旧库（无 inner_git 列）→ CheckpointStore 初始化自动 ALTER 迁移，幂等。"""
    db_path = tmp_path / "old.db"
    # 旧版 checkpoints 建表列（与现 schema 一致但缺 status 之外的 inner_git 列）
    legacy_columns = (
        "checkpoint_id TEXT PRIMARY KEY",
        "session_id TEXT NOT NULL",
        "iteration INTEGER NOT NULL",
        "agent_id TEXT",
        "agent_type TEXT",
        "trigger TEXT NOT NULL",
        "git_ref TEXT",
        "event_rowid INTEGER",
        "main_part_id TEXT",
        "parent_checkpoint_id TEXT",
        "created_at TEXT NOT NULL",
        "file_count INTEGER DEFAULT 0",
        "byte_size INTEGER DEFAULT 0",
        "status TEXT DEFAULT 'ok'",
    )
    legacy_ddl = "CREATE TABLE checkpoints (\n    " + ",\n    ".join(legacy_columns) + "\n)"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(legacy_ddl)
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, session_id, iteration, trigger, "
            "created_at) VALUES ('cp_old_001', 's1', 1, 'subagent_stop', '2026-01-01')",
        )
    # 初始化触发 best-effort ALTER（两次构造验证幂等）
    CheckpointStore(db_path)
    CheckpointStore(db_path)
    store = CheckpointStore(db_path)
    # 旧行读出 inner_git 为空串默认
    got = store.get_sync("cp_old_001")
    assert got is not None and got.inner_git == ""
    # 新列可写
    got.inner_git = "skipped_size"
    store.store_sync(got)
    assert store.get_sync("cp_old_001").inner_git == "skipped_size"
