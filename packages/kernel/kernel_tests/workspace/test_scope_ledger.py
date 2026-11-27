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

"""作用域账本表测试：统一迁移建表、行随管理器动作落表、按树隔离与建区序。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workflow_controller.core.runs import SCHEMA_VERSION, RunStore
from workflow_controller.core.workspace.actions import ScopeIntegrated
from workflow_controller.core.workspace.scope_ledger import ScopeLedgerStore
from workflow_controller.core.workspace.scope_tree import (
    ScopeKind,
    ScopeState,
    ScopeTree,
)

from ._support import FakeOrchestrator, at_rest, build_workspace

_ROOT_SCOPE_ID = "run-1-root"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


class TestSchemaMigration:
    async def test_migration_creates_scopes_table_and_stamps_version(self, db_path: Path) -> None:
        await RunStore(db_path).list_runs()  # 首次访问触发迁移（构造零落盘）

        with sqlite3.connect(db_path) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "scopes" in tables
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            info = conn.execute("PRAGMA table_info(scopes)").fetchall()
            assert [r[1] for r in info] == [
                "root_scope_id",
                "scope_id",
                "parent_scope_id",
                "scope_kind",
                "workspace_ref",
                "scope_base_commit",
                "integration_base_commit",
                "scope_epoch",
                "root_epoch",
                "gate_state",
                "state",
                "owner_ref",
                "created_at",
                "seq",
            ]
            assert [r[1] for r in info if r[5]] == ["root_scope_id", "scope_id"]

    async def test_previous_version_db_upgraded_in_one_step(self, db_path: Path) -> None:
        await RunStore(db_path).list_runs()
        with sqlite3.connect(db_path) as conn:  # 降回上一档形状：删新表、盖旧档
            conn.execute("DROP TABLE scopes")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

        await RunStore(db_path).list_runs()  # 重开一次性升档
        ledger = ScopeLedgerStore(db_path, root_scope_id=_ROOT_SCOPE_ID)
        tree = ScopeTree(store=ledger)
        await tree.create_root_scope(scope_id=_ROOT_SCOPE_ID)

        reopened = RunStore(db_path)  # 重放幂等：档位与行不漂
        await reopened.list_runs()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            count = conn.execute("SELECT count(*) FROM scopes").fetchone()[0]
        assert count == 1

    async def test_ledger_requires_migrated_table(self, tmp_path: Path) -> None:
        bare = ScopeLedgerStore(tmp_path / "empty" / "events.db", root_scope_id="r")  # 未经统一迁移
        with pytest.raises(RuntimeError, match="scopes 表未建"):
            await bare.list_for_tree()
        bare.close()


class TestLedgerRows:
    async def test_rows_follow_manager_actions(self, tmp_path: Path) -> None:
        """建区、集成、废弃、释放与恢复之后，表里的行与内存账本一致（闸门状态列按稳态看）。"""
        db_path = tmp_path / "events.db"
        await RunStore(db_path).list_runs()
        ledger = ScopeLedgerStore(db_path, root_scope_id=_ROOT_SCOPE_ID)
        _root, _repository, manager = build_workspace(tmp_path, scope_tree=ScopeTree(store=ledger))
        orchestrator = FakeOrchestrator(manager)
        await orchestrator.bootstrap_root()
        await orchestrator.prepare("node-a")
        ws_b = Path((await orchestrator.prepare("node-b")).workspace_path)
        (ws_b / "design" / "b.md").write_text("from-b\n", encoding="utf-8")
        assert isinstance(await orchestrator.integrate("node-b"), ScopeIntegrated)
        await manager.release_scope("node-b")
        await orchestrator.abandon("node-a", 1)
        await orchestrator.prepare("node-p")
        await orchestrator.prepare(
            "node-p-c1", parent_scope_id="node-p", scope_kind=ScopeKind.SUBAGENT
        )
        parent = await manager.get_scope("node-p")
        assert parent is not None
        await manager.restore_scope(
            "node-p",
            restore_commit=parent.scope_base_commit or "",
            expected_scope_epoch=1,
            client_op_id="restore-1",
        )

        # 新连接读表（对应重启后的新进程）
        rows = await ScopeLedgerStore(db_path, root_scope_id=_ROOT_SCOPE_ID).list_for_tree()
        assert {row.scope_id: at_rest(row) for row in rows} == {
            record.scope_id: record for record in await manager.list_scopes()
        }
        assert [row.scope_id for row in rows] == [
            _ROOT_SCOPE_ID,
            "node-a",
            "node-b",
            "node-p",
            "node-p-c1",
        ]  # 按建区序存放
        states = {row.scope_id: (row.state, row.scope_epoch) for row in rows}
        assert states["node-a"] == (ScopeState.DISCARDED, 2)
        assert states["node-b"] == (ScopeState.RELEASED, 1)
        assert states["node-p"] == (ScopeState.ACTIVE, 2)
        assert states["node-p-c1"] == (ScopeState.DISCARDED, 2)

    async def test_trees_are_isolated_by_root_scope(self, tmp_path: Path) -> None:
        """同一库内多棵树的同名作用域靠根作用域区分；建区序号树内各自计数。"""
        db_path = tmp_path / "events.db"
        await RunStore(db_path).list_runs()
        ledgers = {
            root: ScopeLedgerStore(db_path, root_scope_id=root)
            for root in ("run-a-root", "run-b-root")
        }
        trees = {root: ScopeTree(store=ledger) for root, ledger in ledgers.items()}
        for root, tree in trees.items():
            await tree.create_root_scope(scope_id=root)
            for key in ("node-x", "node-y"):
                await tree.create_child_scope(
                    scope_id=key,
                    parent_scope_id=root,
                    scope_kind=ScopeKind.MAIN_WRITER,
                    integration_base_commit="c1",
                )
                await tree.activate_scope(
                    key, workspace_ref=f"/ws/{key}|scope/{key}", scope_base_commit="c2"
                )
        tree_a = trees["run-a-root"]
        await tree_a.abandon_scope("node-x", expected_scope_epoch=1)
        await tree_a.begin_attempt(
            "node-x",
            workspace_ref="/ws|scope/node-x-e2",
            scope_base_commit="c9",
            integration_base_commit="c1",
        )
        await tree_a.activate_scope(
            "node-x", workspace_ref="/ws|scope/node-x-e2", scope_base_commit="c9"
        )

        # 新连接读表（对应重启后的新进程）
        reader_a = ScopeLedgerStore(db_path, root_scope_id="run-a-root")
        reader_b = ScopeLedgerStore(db_path, root_scope_id="run-b-root")
        rows_a = await reader_a.list_for_tree()
        rows_b = await reader_b.list_for_tree()
        # 同键换代重建不改变序号：遍历序仍是根最先、子作用域按建区序
        assert [row.scope_id for row in rows_a] == ["run-a-root", "node-x", "node-y"]
        assert [row.scope_id for row in rows_b] == ["run-b-root", "node-x", "node-y"]
        assert rows_a == await tree_a.list_all_scopes()
        assert rows_b == await trees["run-b-root"].list_all_scopes()
        assert rows_a[1].scope_epoch == 2 and rows_b[1].scope_epoch == 1

        assert await reader_a.delete_for_tree() == 3
        assert await reader_a.list_for_tree() == []
        assert len(await reader_b.list_for_tree()) == 3
