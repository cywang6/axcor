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

"""作用域账本表 scopes 的持久化读写面。

scopes 表住项目库 events.db，建表归 run 记录库的统一迁移管理，本模块只提
供读写面、不自行建表。一个存储实例绑定一棵树（根作用域标识）：树的全部
作用域行以根作用域标识锚定，主键是（根作用域标识, 作用域标识）——Graph
平面的作用域键不含 run 标识，同一库内多个 run 的同名节点靠树根区分。
行与账本行（ScopeRecord）字段一一对应，枚举列按词面还原。建区序号在行
首次落表时分配、此后不变：时间戳的精度不足以定序，读出按序号排序，重建
后的遍历序才与重建前一致。
"""

from __future__ import annotations

__all__ = ["SCOPES_DDL", "ScopeLedgerStore"]

import asyncio
import sqlite3
import threading
from typing import TYPE_CHECKING

from workflow_controller.core.workspace.scope_tree import (
    ScopeGateState,
    ScopeKind,
    ScopeRecord,
    ScopeState,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

SCOPES_DDL = """
    CREATE TABLE IF NOT EXISTS scopes (
        root_scope_id          TEXT NOT NULL,
        scope_id               TEXT NOT NULL,
        parent_scope_id        TEXT,
        scope_kind             TEXT NOT NULL,
        workspace_ref          TEXT,
        scope_base_commit      TEXT,
        integration_base_commit TEXT,
        scope_epoch            INTEGER NOT NULL,
        root_epoch             INTEGER NOT NULL,
        gate_state             TEXT NOT NULL,
        state                  TEXT NOT NULL,
        owner_ref              TEXT,
        abandon_op_id          TEXT,
        created_at             TEXT NOT NULL,
        seq                    INTEGER NOT NULL,
        PRIMARY KEY (root_scope_id, scope_id)
    )
"""

# 行与账本行一一对应的列（根作用域标识与建区序号由存储维护，不占账本行字段）
# 建区序号：树内最大值加一；首次落表时写入，同键覆盖时不变
_NEXT_SEQ_SQL = (
    "(SELECT COALESCE(MAX(seq), 0) + 1 FROM scopes WHERE root_scope_id = :root_scope_id)"
)
_COLUMN_NAMES = (
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
    "abandon_op_id",
    "created_at",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)
_VALUES = ", ".join([":root_scope_id", *(f":{name}" for name in _COLUMN_NAMES), _NEXT_SEQ_SQL])
# 同键覆盖时除建区序号外的列全部换成新值；序号只在首次落表时生效
_UPSERT_SQL = (
    f"INSERT INTO scopes (root_scope_id, {_COLUMNS}, seq) VALUES ({_VALUES})"
    " ON CONFLICT (root_scope_id, scope_id) DO UPDATE SET"
    + "".join(f" {name} = excluded.{name}," for name in _COLUMN_NAMES[1:]).rstrip(",")
)


def _row_to_record(row: sqlite3.Row) -> ScopeRecord:
    """行 → 账本行：枚举列按词面还原。"""
    return ScopeRecord(
        scope_id=row["scope_id"],
        parent_scope_id=row["parent_scope_id"],
        scope_kind=ScopeKind(row["scope_kind"]),
        owner_ref=row["owner_ref"],
        workspace_ref=row["workspace_ref"],
        scope_base_commit=row["scope_base_commit"],
        integration_base_commit=row["integration_base_commit"],
        scope_epoch=int(row["scope_epoch"]),
        root_epoch=int(row["root_epoch"]),
        gate_state=ScopeGateState(row["gate_state"]),
        state=ScopeState(row["state"]),
        created_at=row["created_at"],
        abandon_op_id=row["abandon_op_id"],
    )


def _named(record: ScopeRecord) -> dict[str, object]:
    """账本行 → 落库命名参数（键与列名一致，枚举落词面）。"""
    return {
        "scope_id": record.scope_id,
        "parent_scope_id": record.parent_scope_id,
        "scope_kind": record.scope_kind.value,
        "workspace_ref": record.workspace_ref,
        "scope_base_commit": record.scope_base_commit,
        "integration_base_commit": record.integration_base_commit,
        "scope_epoch": record.scope_epoch,
        "root_epoch": record.root_epoch,
        "gate_state": record.gate_state.value,
        "state": record.state.value,
        "owner_ref": record.owner_ref,
        "abandon_op_id": record.abandon_op_id,
        "created_at": record.created_at,
    }


class ScopeLedgerStore:
    """一棵树的作用域账本读写面（按树绑定；建表归 run 记录库统一迁移）。"""

    def __init__(self, db_path: Path, *, root_scope_id: str) -> None:
        """只记库路径与树根：连接与表核查随首次访问发生。"""
        self.db_path = db_path
        self.root_scope_id = root_scope_id
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def close(self) -> None:
        """释放复用连接（树回收与测试释放文件级占用）。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── 异步面（事件循环侧调用）──

    async def upsert(self, records: Sequence[ScopeRecord]) -> None:
        """把若干账本行写入表：同键覆盖，多行同一事务，不留半批。"""
        if records:
            await asyncio.to_thread(self._upsert_locked, records)

    async def list_for_tree(self) -> list[ScopeRecord]:
        """本树全部账本行，按建区序号排序（根最先建立，恒在前）。"""
        return await asyncio.to_thread(self._list_for_tree_locked)

    async def delete_for_tree(self) -> int:
        """删本树全部账本行（全新建树前清残留、树回收），返回删除行数。"""
        return await asyncio.to_thread(self._delete_for_tree_locked)

    # ── 同步面（锁内）──

    def _upsert_locked(self, records: Sequence[ScopeRecord]) -> None:
        """同步面：多行同一事务落表。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for record in records:
                    conn.execute(
                        _UPSERT_SQL, {"root_scope_id": self.root_scope_id, **_named(record)}
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _list_for_tree_locked(self) -> list[ScopeRecord]:
        """同步面：按建区序号读出本树全部行。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM scopes WHERE root_scope_id = ? ORDER BY seq",
                (self.root_scope_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _delete_for_tree_locked(self) -> int:
        """同步面：删本树全部行。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM scopes WHERE root_scope_id = ?", (self.root_scope_id,)
            )
            return cursor.rowcount

    def _connect(self) -> sqlite3.Connection:
        """懒开复用连接；首次访问核对账本表已在库（本模块不建表）。"""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scopes'"
            ).fetchone()
            if present is None:
                conn.close()
                raise RuntimeError(
                    f"{self.db_path}：scopes 表未建——作用域账本须经 run 记录库的统一迁移建表后使用"
                )
            self._conn = conn
        return self._conn
