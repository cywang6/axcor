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

"""run 级联删除：单事务原子性、可重复删除、产物保留与归属收敛。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from workflow_controller.core.run_coordinator import RunConfig, RunCoordinator
from workflow_controller.core.runs import RunStore
from workflow_controller.core.session_run_affiliation import SessionRunAffiliationService
from workflow_controller.plugin.run_driver import SessionSpec, StartSession
from workflow_controller.session.events import Event, EventType
from workflow_controller.store.event_store import EventStore

# ── 测试替身 ──────────────────────────────────────────────────────


@dataclass
class FakeSession:
    id: str
    name: str
    state: str = "running"
    stop_reason: str | None = None
    loop_status: str = "yes"
    config: Any = None
    created_at: datetime = field(default_factory=datetime.now)


class FakePool:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}

    def get(self, sid: str) -> FakeSession | None:
        return self.sessions.get(sid)

    def remove(self, sid: str) -> bool:
        return self.sessions.pop(sid, None) is not None

    def list_sessions(self) -> list[FakeSession]:
        return list(self.sessions.values())

    async def persist(self, session: Any) -> None:
        pass


class FakeBus:
    def __init__(self) -> None:
        self.observers: dict[str, list] = {}
        self.ephemeral: list[Event] = []

    def observe(self, event_type, handler) -> None:
        self.observers.setdefault(getattr(event_type, "value", str(event_type)), []).append(handler)

    async def emit_ephemeral(self, event: Event) -> None:
        self.ephemeral.append(event)


class FakeSO:
    """Session Orchestrator 替身：成员出生即落归属行；停止/驱逐记录在案。"""

    def __init__(self, affiliation, project_dir, event_store) -> None:
        self.pool = FakePool()
        self.affiliation = affiliation
        self.default_project_dir = project_dir
        self.event_router = SimpleNamespace(default_store=event_store)
        self.started: list = []
        self.stopped: list[str] = []
        self.discarded: list[str] = []

    async def start_session_with_config(self, cfg):
        s = FakeSession(f"s{len(self.started) + 1}", cfg.name)
        self.pool.sessions[s.id] = s
        self.started.append(cfg)
        await self.affiliation.attach(s.id, cfg.run_id, role=cfg.role)
        return s

    async def stop_session(self, sid: str) -> None:
        self.stopped.append(sid)

    def discard_deleted_session(self, sid: str) -> None:
        self.discarded.append(sid)
        self.pool.remove(sid)

    def get_session_facts(self, sid: str):
        s = self.pool.get(sid)
        if s is None:
            return None
        return SimpleNamespace(state=s.state, stop_reason=s.stop_reason, loop_status=s.loop_status)

    def is_terminal(self, sid: str) -> bool:
        facts = self.get_session_facts(sid)
        return facts is None or facts.loop_status == "no"


class SlowDriver:
    """起一个常驻成员、永不收口的驱动。"""

    @staticmethod
    def decide(world, notifications):
        if not world.sessions and world.in_flight == 0:
            return [
                StartSession(
                    spec=SessionSpec(name="slow", prompt="p", cwd="/tmp"), context="node=a"
                )
            ]
        return []

    @staticmethod
    def on_control(control) -> None:
        pass

    @staticmethod
    def projection():
        return {"schema_version": 1}

    @staticmethod
    def snapshot():
        return {"schema_version": 1, "state": {}, "pending_timers": []}


class SlowFactory:
    format = "fake@1"

    @staticmethod
    def create(run, program_path, params):
        return SlowDriver()

    @staticmethod
    def restore(run, snapshot, sessions):
        return SlowDriver()


class Harness:
    def __init__(self, tmp_path) -> None:
        db = tmp_path / "events.db"
        self.project_dir = tmp_path
        self.store = RunStore(db)
        self.event_store = EventStore(db)
        self.affiliation = SessionRunAffiliationService(self.store)
        self.bus = FakeBus()
        self.so = FakeSO(self.affiliation, tmp_path, self.event_store)
        self.coord = RunCoordinator(
            orchestrator=self.so,
            run_store=self.store,
            bus=self.bus,
            driver_factories={"fake@1": SlowFactory()},
            affiliation=self.affiliation,
        )
        self.program = tmp_path / "prog.yaml"
        self.program.write_text("format: fake@1\n", encoding="utf-8")

    async def start_workflow(self, **config_kwargs: Any) -> str:
        run = await self.coord.start_run(RunConfig(format="fake@1", **config_kwargs), self.program)
        return run.run_id

    async def create_container(self) -> str:
        return (await self.coord.create_run("box")).run_id

    async def seed_member(self, run_id: str, sid: str) -> None:
        """落一个在册成员的全套数据：归属行、名册行、事件与评测。"""
        await self.affiliation.attach(sid, run_id)
        await self.seed_roster(sid, run_id)
        await self.seed_member_data(sid, run_id)
        self.so.pool.sessions[sid] = FakeSession(sid, sid)

    async def seed_roster(self, sid: str, run_id: str | None) -> None:
        now = datetime.now(tz=UTC).isoformat()
        await self.event_store.upsert_roster(
            {
                "id": sid,
                "name": sid,
                "role": "executor",
                "state": "idle",
                "loop_status": "no",
                "config_json": json.dumps({"name": sid, "run_id": run_id}),
                "created_at": now,
                "last_activity": now,
            }
        )

    async def seed_member_data(self, sid: str, run_id: str | None) -> None:
        event = Event.create(
            session_id=sid,
            iteration=0,
            event_type=EventType.USER_MESSAGE,
            data={"q": 1},
        )
        event.run_id = run_id
        await self.event_store.store(event)
        await self.event_store.store_evaluation(
            sid,
            1,
            SimpleNamespace(
                is_complete=False,
                progress=0.5,
                summary="s",
                feedback=None,
                artifacts={},
            ),
        )
        await self.event_store.store_loop_result(sid, 1, "max_iterations")

    async def eventually(self, pred, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            if pred():
                return
            if time.monotonic() >= deadline:
                raise AssertionError("条件等待超时")
            await asyncio.sleep(0.01)

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.store.db_path)
        conn.row_factory = sqlite3.Row
        out = conn.execute(sql, params).fetchall()
        conn.close()
        return out

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table}" + (f" WHERE {where}" if where else "")
        return int(self.rows(sql, params)[0]["n"])


class _ConnThatFailsOn:
    """连接包装：命中给定 SQL 片段的语句抛错（事务内崩溃注入）。"""

    def __init__(self, conn: sqlite3.Connection, fragment: str) -> None:
        self._conn = conn
        self._fragment = fragment

    def __enter__(self):
        return self

    @staticmethod
    def __exit__(*exc):
        return False

    def execute(self, sql: str, params: tuple = ()):
        if self._fragment in sql:
            raise sqlite3.OperationalError("injected crash")
        return self._conn.execute(sql, params)


# ── 全链路：停成员、级联、驱逐与广播 ──────────────────────────────


class TestWorkflowDelete:
    async def test_live_run_delete_stops_member_and_cascades(self, tmp_path):
        h = Harness(tmp_path)
        run_id = await h.start_workflow()
        await h.eventually(lambda: h.count("run_sessions", "run_id = ?", (run_id,)) > 0)
        sid = (await h.store.list_session_ids(run_id))[0]
        await h.seed_roster(sid, run_id)
        await h.event_store.store_run_event(run_id, "process_output", {"line": "x"})
        await h.store.save_driver_state(run_id, {"schema_version": 1})
        await h.store.append_driver_snapshot(run_id, {"schema_version": 1})

        await h.coord.delete_run(run_id)

        assert h.so.stopped == [sid]  # 成员先停再删账
        assert sid not in h.so.pool.sessions  # pool 行在事务提交后驱逐
        assert h.affiliation.resolve(sid) is None
        assert h.count("runs", "run_id = ?", (run_id,)) == 0
        assert h.count("run_sessions", "run_id = ?", (run_id,)) == 0
        assert h.count("events", "session_id = ?", (sid,)) == 0  # 打戳成员事件随删
        assert h.count("run_events", "run_id = ?", (run_id,)) == 0
        assert h.count("run_driver_state", "run_id = ?", (run_id,)) == 0
        assert h.count("run_driver_snapshots", "run_id = ?", (run_id,)) == 0
        assert h.count("sessions", "id = ?", (sid,)) == 0  # 成员名册行随删
        assert [e.event_type for e in h.bus.ephemeral] == ["session_delete"]
        sd = h.bus.ephemeral[0]  # 驱逐前给成员补发清理通知（不落库）
        assert sd.session_id == sid and sd.data["session_id"] == sid

    async def test_run_root_removed_work_dir_untouched(self, tmp_path):
        h = Harness(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        (work / "artifact.txt").write_text("产物", encoding="utf-8")
        run_id = await h.start_workflow(work_dir=str(work))
        root = tmp_path / ".workflow-controller" / "runs" / run_id
        (root / "export").mkdir(parents=True, exist_ok=True)
        (root / "export" / "envelope.json").write_text("{}", encoding="utf-8")
        assert root.exists()

        await h.coord.delete_run(run_id)

        assert not root.exists()  # run 根整体删除（program/export）
        assert (work / "artifact.txt").read_text(encoding="utf-8") == "产物"  # work_dir 零触碰


# ── pool 纯内存驱逐 ────────────────────────────────────────────────


class TestPoolDiscard:
    @staticmethod
    def test_discard_is_memory_only():
        """discard 只清内存 pool 行，不动持久层——持久数据归删除事务清零。"""
        from workflow_controller.session.manager import SessionPool

        pool = SessionPool()
        touched: list[str] = []

        class Router:
            @staticmethod
            def delete_session(sid: str) -> None:
                touched.append(sid)

        pool.set_event_router(Router())
        getattr(pool, "_sessions")["s1"] = SimpleNamespace(id="s1")
        assert pool.discard("s1") is True
        assert pool.get("s1") is None and touched == []
        assert pool.discard("s1") is False  # 已不在 pool


# ── 事务与幂等 ────────────────────────────────────────────────────


class TestTransactionAndIdempotency:
    async def test_mid_transaction_crash_rolls_back_and_retry_converges(
        self, monkeypatch, tmp_path
    ):
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")

        real = getattr(h.store, "_connect")()
        monkeypatch.setattr(h.store, "_conn", _ConnThatFailsOn(real, "DELETE FROM run_events"))
        with pytest.raises(sqlite3.OperationalError):
            await h.coord.delete_run(run_id)
        monkeypatch.setattr(h.store, "_conn", real)

        # 账面完好：事务内崩溃不留半删态
        assert h.count("runs", "run_id = ?", (run_id,)) == 1
        assert h.count("run_sessions", "run_id = ?", (run_id,)) == 1
        assert h.count("events", "session_id = 's1'") == 1
        assert h.count("sessions", "id = 's1'") == 1

        await h.coord.delete_run(run_id)  # 重删收敛
        assert h.count("runs", "run_id = ?", (run_id,)) == 0
        assert h.count("events", "session_id = 's1'") == 0

    async def test_rerun_is_idempotent(self, tmp_path):
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")

        await h.coord.delete_run(run_id)
        await h.coord.delete_run(run_id)  # 重复删除：不报错、不重复广播

        assert [e.event_type for e in h.bus.ephemeral] == ["session_delete"]
        assert h.so.discarded == ["s1"]

    async def test_post_commit_crash_leaves_orphan_dir_only(self, tmp_path, monkeypatch):
        h = Harness(tmp_path)
        run_id = await h.start_workflow()
        root = tmp_path / ".workflow-controller" / "runs" / run_id
        assert root.exists()

        # 提交点后、物理删除前崩溃：rmtree 未执行
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
        await h.coord.delete_run(run_id)
        assert h.count("runs", "run_id = ?", (run_id,)) == 0  # 账面无残留
        assert root.exists()  # 至多遗留孤儿目录
        monkeypatch.undo()

        await h.coord.delete_run(run_id)  # 重试收敛：补物理删除
        assert not root.exists()


# ── 成员数据寻址 ──────────────────────────────────────────────────


class TestMemberDataAddressing:
    async def test_member_events_and_evaluations_deleted(self, tmp_path):
        """现任成员的事件/评测/循环结果随 run 删（跨库缺口回归）。"""
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")
        assert h.count("events", "run_id = ?", (run_id,)) == 1

        await h.coord.delete_run(run_id)

        assert h.count("events", "session_id = 's1'") == 0
        assert h.count("evaluations", "session_id = 's1'") == 0
        assert h.count("loop_results", "session_id = 's1'") == 0

    async def test_member_handoff_relations_deleted_by_either_endpoint(self, tmp_path):
        """成员的 handoff 行两端任一指向成员即随事务删；无关行与已脱离成员的行保留。"""
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")
        await h.seed_member(run_id, "s2")
        await _seed_detached_member(h, run_id, "s-gone")
        await h.event_store.add_handoff_relation("s1", "s2")  # 双端均为现任成员
        await h.event_store.add_handoff_relation("s1", "s-out1")  # 出端成员、入端外人
        await h.event_store.add_handoff_relation("s-out2", "s2")  # 入端成员
        await h.event_store.add_handoff_relation("s-out1", "s-out2")  # 全外人
        await h.event_store.add_handoff_relation("s-gone", "s-out3")  # 已脱离成员（随 session 走）

        await h.coord.delete_run(run_id)

        for sid in ("s1", "s2"):
            assert (
                h.count(
                    "handoff_relations",
                    "from_session_id = ? OR to_session_id = ?",
                    (sid, sid),
                )
                == 0
            )
        assert h.count("handoff_relations") == 2  # 全外人 + 已脱离成员的行保留
        assert h.count("handoff_relations", "from_session_id = 's-gone'") == 1

    async def test_early_unstamped_member_rows_deleted(self, tmp_path):
        """激活前未打 run_id 戳的早期事件按 session 定位随 run 删，不留孤儿行。"""
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")
        early = Event.create(
            session_id="s1",
            iteration=0,
            event_type=EventType.SESSION_ACTIVATED,
            data={},
        )
        assert early.run_id is None
        await h.event_store.store(early)

        await h.coord.delete_run(run_id)

        assert h.count("events", "session_id = 's1'") == 0
        assert h.count("evaluations", "session_id = 's1'") == 0
        assert h.count("loop_results", "session_id = 's1'") == 0


# ── 名册与已脱离成员 ──────────────────────────────────────────────


async def _seed_detached_member(h: Harness, run_id: str, sid: str) -> None:
    """构造已脱离成员：成员期数据与名册行在、归属行不在。"""
    await h.affiliation.attach(sid, run_id)
    await h.seed_roster(sid, run_id)
    await h.seed_member_data(sid, run_id)
    await h.store.discard_affiliation(sid)
    h.affiliation.evict(sid)


class TestRosterAndDetachedMembers:
    async def test_member_roster_deleted_no_dangling_pointer(self, tmp_path):
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await h.seed_member(run_id, "s1")

        await h.coord.delete_run(run_id)

        assert h.count("sessions", "id = 's1'") == 0
        danglings = [
            r["id"]
            for r in h.rows("SELECT id, config_json FROM sessions")
            if json.loads(r["config_json"]).get("run_id") == run_id
        ]
        assert danglings == []

    async def test_detached_member_backfills_implicit_without_fk_crash(self, tmp_path):
        """已脱离成员的名册行仍指向被删 run：重启恢复不报外键错，改铸新的隐式 run。"""
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await _seed_detached_member(h, run_id, "s-gone")

        await h.coord.delete_run(run_id)
        assert h.count("sessions", "id = 's-gone'") == 1  # 名册行随 session 走

        cfg = SimpleNamespace(run_id=run_id, role="executor")
        h.so.pool.sessions["s-gone"] = FakeSession("s-gone", "s-gone", config=cfg)
        await h.affiliation.recover_at_boot(h.so.pool)

        new_run = h.affiliation.resolve("s-gone")
        assert new_run is not None and new_run.run_id != run_id
        assert h.count("runs", "run_id = ?", (new_run.run_id,)) == 1
        assert h.count("run_sessions", "session_id = 's-gone'") == 1

    async def test_detached_member_data_survives_with_residual_stamp(self, tmp_path):
        """已脱离成员的数据保留不追删；残留的 run_id 戳只是历史痕迹。"""
        h = Harness(tmp_path)
        run_id = await h.create_container()
        await _seed_detached_member(h, run_id, "s-gone")
        other_run = await h.create_container()
        await h.seed_member(other_run, "s-cur")

        await h.coord.delete_run(run_id)

        assert h.count("events", "session_id = 's-gone'") == 1  # 随 session 走
        assert h.count("events", "run_id = ?", (run_id,)) == 1  # 残戳保留、不追删
        assert h.count("evaluations", "session_id = 's-gone'") == 1
        assert h.count("sessions", "id = 's-gone'") == 1

        await h.coord.delete_run(other_run)  # 现任成员照常随 run 删
        assert h.count("events", "session_id = 's-cur'") == 0
        assert h.count("sessions", "id = 's-cur'") == 0
        assert h.count("runs", "run_id = ?", (other_run,)) == 0
