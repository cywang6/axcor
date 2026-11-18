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

"""检查点存储（agent_traceback 插件 own 的数据结构 + SQL）。

``Checkpoint`` dataclass 与 ``checkpoints`` 表的 DDL + CRUD 全部归插件所有；表物理上仍
留在 ``.workflow-controller/events.db``（与 events 同库，``event_rowid`` 双向可查、备份/迁移一致），
但核心 :class:`EventStore` 不再含 checkpoint 概念。插件经 ``PluginServices.events_db_path``
端口解析 db 路径后自己开连接操作。

``get_latest_subagent_stop_rowid`` 查的是 events 表（通用事件数据），但仅 traceback 关心
checkpoint↔event 反链，故一并归插件。核心只需在 ``delete_session`` 末尾经通用
``on_session_delete`` 钩子通知插件清自己的行。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass
class Checkpoint:
    """检查点元数据。

    一个检查点 = 一个 subagent 的 stop 时刻，绑定「事件态 + 文件态 + 对话态」三件：
    git_ref 指向快照仓的文件态 commit；event_rowid 指向 EventStore 的 SUBAGENT stop
    事件；main_part_id 指向 opencode.db 主 session 的 part 截断点。
    """

    checkpoint_id: str
    session_id: str
    iteration: int
    agent_id: str
    agent_type: str
    trigger: str
    git_ref: str | None
    event_rowid: int | None
    main_part_id: str | None
    parent_checkpoint_id: str | None
    created_at: str
    file_count: int
    byte_size: int
    status: str = "ok"
    # 内部 git 捕获结果：captured / absent / skipped_size；空串 = 旧检查点（未知）。
    # 恢复逻辑不依赖该列（以 git ls-tree 探测为准），仅作展示 / 排查用。
    inner_git: str = ""


def _opt_text(value: Any, default: str) -> str:
    """非空文本列映射：None → default，其余 str()。"""
    return default if value is None else str(value)


def _opt_ref(value: Any) -> str | None:
    """可空文本引用列映射：None → None，其余 str()。"""
    return None if value is None else str(value)


def _opt_count(value: Any, default: int) -> int:
    """数值列映射：None → default，其余 int()。"""
    return default if value is None else int(value)


def _row_to_checkpoint(row: tuple[Any, ...]) -> Checkpoint:
    """Map a checkpoints table row (ordered by the SELECT in store_* methods)."""
    return Checkpoint(
        checkpoint_id=str(row[0]),
        session_id=str(row[1]),
        iteration=_opt_count(row[2], 0),
        agent_id=_opt_text(row[3], ""),
        agent_type=_opt_text(row[4], ""),
        trigger=str(row[5]),
        git_ref=_opt_ref(row[6]),
        event_rowid=int(row[7]) if row[7] is not None else None,
        main_part_id=_opt_ref(row[8]),
        parent_checkpoint_id=_opt_ref(row[9]),
        created_at=str(row[10]),
        file_count=_opt_count(row[11], 0),
        byte_size=_opt_count(row[12], 0),
        status=_opt_text(row[13], "ok"),
        inner_git=_opt_text(row[14], ""),
    )


class CheckpointStore:
    """插件 own 的检查点表 DDL + CRUD（与 events 同库，独立连接）。

    构造时接收 db 路径并 ``CREATE TABLE IF NOT EXISTS``。``get_latest_subagent_stop_rowid``
    查的是核心 events 表（同库），故无需跨库。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._init_db()

    async def store(self, cp: Checkpoint) -> None:
        """Persist a checkpoint row. Idempotent on checkpoint_id (INSERT OR REPLACE)."""
        async with self._lock:
            self.store_sync(cp)

    def store_sync(self, cp: Checkpoint) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(checkpoint_id, session_id, iteration, agent_id, agent_type, "
                "trigger, git_ref, event_rowid, main_part_id, parent_checkpoint_id, "
                "created_at, file_count, byte_size, status, inner_git) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cp.checkpoint_id,
                    cp.session_id,
                    cp.iteration,
                    cp.agent_id,
                    cp.agent_type,
                    cp.trigger,
                    cp.git_ref,
                    cp.event_rowid,
                    cp.main_part_id,
                    cp.parent_checkpoint_id,
                    cp.created_at,
                    cp.file_count,
                    cp.byte_size,
                    cp.status,
                    cp.inner_git,
                ),
            )

    async def get(self, checkpoint_id: str) -> Checkpoint | None:
        async with self._lock:
            return self.get_sync(checkpoint_id)

    def get_sync(self, checkpoint_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checkpoint_id, session_id, iteration, agent_id, agent_type, "
                "trigger, git_ref, event_rowid, main_part_id, parent_checkpoint_id, "
                "created_at, file_count, byte_size, status, inner_git "
                "FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_checkpoint(row)

    async def list(self, session_id: str) -> list[Checkpoint]:
        async with self._lock:
            return self.list_sync(session_id)

    def list_sync(self, session_id: str) -> list[Checkpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT checkpoint_id, session_id, iteration, agent_id, agent_type, "
                "trigger, git_ref, event_rowid, main_part_id, parent_checkpoint_id, "
                "created_at, file_count, byte_size, status, inner_git "
                "FROM checkpoints WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [_row_to_checkpoint(r) for r in rows]

    def get_latest_subagent_stop_rowid_sync(
        self,
        session_id: str,
        agent_id: str,
    ) -> int | None:
        """Latest SUBAGENT_STOP event rowid for (session_id, agent_id).

        Best-effort link for checkpoints.event_rowid (双向可查). Queries the core
        ``events`` table (same db). None if not found (e.g. the bridge queue hasn't
        stored the event yet at call time, or the events table doesn't exist yet).
        """
        if not agent_id:
            return None
        with self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT rowid FROM events "
                    "WHERE session_id = ? AND event_type = 'subagent_stop' "
                    "AND json_extract(data, '$.agent_id') = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (session_id, agent_id),
                ).fetchone()
            except sqlite3.OperationalError:
                # events table not yet created (no events stored) — nothing to link.
                return None
        return int(row[0]) if row else None

    def delete_for_session(self, session_id: str) -> None:
        """Delete all checkpoints for a session (called from on_session_delete)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection with a sane busy_timeout.

        The checkpoints table lives in the same WAL db as the core ``events``
        table, so the plugin's writes can contend with the EventStore's writes.
        A per-connection ``busy_timeout`` makes SQLite wait (up to 5s) for the
        write lock instead of failing immediately with ``database is locked``
        (the per-op default is 0 → silent checkpoint loss under live contention).
        ``journal_mode=WAL`` is persistent on the db header, so per-op connections
        inherit it without re-setting the PRAGMA.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id    TEXT PRIMARY KEY,
                    session_id       TEXT NOT NULL,
                    iteration         INTEGER NOT NULL,
                    agent_id          TEXT,
                    agent_type        TEXT,
                    trigger           TEXT NOT NULL,
                    git_ref           TEXT,
                    event_rowid       INTEGER,
                    main_part_id      TEXT,
                    parent_checkpoint_id TEXT,
                    created_at        TEXT NOT NULL,
                    file_count        INTEGER DEFAULT 0,
                    byte_size         INTEGER DEFAULT 0,
                    status            TEXT DEFAULT 'ok',
                    inner_git         TEXT DEFAULT ''
                )
            """)
            # 旧库迁移：CREATE TABLE IF NOT EXISTS 不给已存在的表加列 → best-effort
            # ALTER（列已存在时忽略）。
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE checkpoints ADD COLUMN inner_git TEXT DEFAULT ''")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cp_session
                ON checkpoints (session_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cp_parent
                ON checkpoints (parent_checkpoint_id)
            """)
