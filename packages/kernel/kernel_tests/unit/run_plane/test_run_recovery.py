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

"""boot 崩溃恢复：步序列、步跳过、补铸、ghost reviewer 重建与关停收尾。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from workflow_controller.config.settings import SessionConfig, SessionReviewerConfig
from workflow_controller.core import run_coordinator
from workflow_controller.core.run_coordinator import RunConfig, RunCoordinator
from workflow_controller.core.runs import RunKind, RunStatus, RunStore
from workflow_controller.core.session_run_affiliation import SessionRunAffiliationService
from workflow_controller.plugin.run_driver import (
    ControlKind,
    FinishOutcome,
    FinishRun,
    SessionOutcomeNotification,
    SessionSpec,
    StartProcess,
    StartSession,
)

# StubBus 走别名导入（as 独立成块），打断与 test_group_control 导入块的 10 行重复窗口
from ._run_plane_stubs import (
    PoolFactsMixin,
    StubPool,
    StubSession,
)
from ._run_plane_stubs import (
    StubBus as _StubBus,
)
from ._run_plane_stubs import (
    member_outcome_payload as _member_outcome_payload,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── run 平面测试替身（同库重装配用）──────────────────────────────


class RecoverySO(PoolFactsMixin):
    """SO 替身：出生即归属落账；名含 slow 的成员不落终态（模拟崩溃时在跑）。

    sid 全局唯一：同库重装配后旧归属行仍在，重发同 id 会触发归属冲突。
    """

    _sid_seq = 0

    def __init__(self, affiliation, bus, project_dir) -> None:
        self.pool = StubPool()
        self.affiliation = affiliation
        self.bus = bus
        self.default_project_dir = project_dir
        self.stopped: list[str] = []

    @staticmethod
    def is_restarting(sid: str) -> bool:
        return False

    async def start_session_with_config(self, cfg):
        setattr(RecoverySO, "_sid_seq", getattr(RecoverySO, "_sid_seq") + 1)
        sid = f"rs{getattr(RecoverySO, '_sid_seq')}"
        session = StubSession(sid, cfg.name)
        self.pool.sessions[sid] = session
        await self.affiliation.attach(sid, cfg.run_id, role=cfg.role)
        if "slow" not in cfg.name:
            asyncio.get_running_loop().create_task(self._conclude(session))
        return session

    async def stop_session(self, sid: str) -> None:
        self.stopped.append(sid)

    async def ensure_reviewer_snapshot(self, sid: str) -> None:
        pass

    def is_terminal(self, sid: str) -> bool:
        facts = self.get_session_facts(sid)
        return facts is None or facts.loop_status == "no"

    async def _conclude(self, session: StubSession) -> None:
        await asyncio.sleep(0.02)
        session.state = "idle"
        session.loop_status = "no"
        session.stop_reason = "reviewer_complete"
        await self.bus.emit_outcome(
            session.id,
            _member_outcome_payload(session.id, "completed", session.stop_reason, digest="done"),
        )


class ResumingDriver:
    """恢复用玩具驱动：首轮无通知时出生一个成员，见到成员结局即收尾。"""

    def __init__(self, member_name: str) -> None:
        self.member_name = member_name
        self.rounds = 0
        self.started_member = False
        self.controls: list[Any] = []

    def decide(self, world, notifications):
        self.rounds += 1
        if any(isinstance(n, SessionOutcomeNotification) for n in notifications):
            return [FinishRun(FinishOutcome.COMPLETED)]
        if not self.started_member and world.in_flight == 0:
            self.started_member = True
            return [
                StartSession(
                    spec=SessionSpec(name=self.member_name, prompt="p", cwd="/tmp"),
                    context="a",
                )
            ]
        return []

    def on_control(self, control) -> None:
        self.controls.append(control)

    def snapshot(self):
        return {"schema_version": 1, "rounds": self.rounds, "pending_timers": []}


class ResumingFactory:
    """create 出生慢成员（崩溃前在跑）；restore 换快速结束的成员并记录恢复入参。"""

    format = "fake@1"

    def __init__(self) -> None:
        self.driver: ResumingDriver | None = None
        self.restore_logs: list[tuple[str, dict, list[str]]] = []

    def create(self, run, program_path, params):
        self.driver = ResumingDriver("slow-n1")
        return self.driver

    def restore(self, run, snapshot, sessions):
        self.driver = ResumingDriver("n1")
        self.driver.rounds = snapshot.get("rounds", 0)
        self.restore_logs.append((run.run_id, snapshot, [s.session_id for s in sessions]))
        return self.driver


class StuckDriver:
    """decide 首轮即阻塞在跨线程 Event 上：run 任务卡死，迫使关停走代办路径。

    stuck_s 后自行返回（默认 30s ≈ 永久卡死）——线程「慢而未死」与
    「永久卡死」两种形态共用一个替身。
    """

    def __init__(self, stuck_s: float = 30.0) -> None:
        self.rounds = 0
        self.stuck_s = stuck_s
        self.release = threading.Event()
        self.finished = threading.Event()

    @staticmethod
    def on_control(control) -> None:
        pass

    def decide(self, world, notifications):
        self.rounds += 1
        self.release.wait(timeout=self.stuck_s)
        self.finished.set()
        return []

    def snapshot(self):
        return {"schema_version": 1, "rounds": self.rounds, "pending_timers": []}


class StuckFactory:
    format = "fake@1"

    def __init__(self, stuck_s: float = 30.0) -> None:
        self.stuck_s = stuck_s
        self.driver: StuckDriver | None = None

    def create(self, run, program_path, params):
        self.driver = StuckDriver(self.stuck_s)
        return self.driver

    def restore(self, run, snapshot, sessions):
        return self.create(run, None, None)


class CooperatingStopDriver:
    """关停协作驱动：受理 stopped 后在下一轮 decide 内决定 FinishRun。

    运行环约定允许此形态（终态控制受理不剥夺驱动的决定权）；快照携带
    finished 标记，用于断言最终判定是否落盘。
    """

    def __init__(self) -> None:
        self.rounds = 0
        self.stopped_seen = False
        self.finished = False

    def decide(self, world, notifications):
        self.rounds += 1
        if self.stopped_seen:
            self.finished = True
            return [FinishRun(FinishOutcome.COMPLETED)]
        return []

    def on_control(self, control) -> None:
        if control.kind == ControlKind.STOPPED:
            self.stopped_seen = True
        return []

    def snapshot(self):
        return {"schema_version": 1, "rounds": self.rounds, "finished": self.finished}


class CooperatingStopFactory:
    format = "fake@1"

    def __init__(self) -> None:
        self.driver: CooperatingStopDriver | None = None

    def create(self, run, program_path, params):
        self.driver = CooperatingStopDriver()
        return self.driver

    def restore(self, run, snapshot, sessions):
        self.driver = CooperatingStopDriver()
        self.driver.rounds = snapshot.get("rounds", 0)
        self.driver.finished = snapshot.get("finished", False)
        return self.driver


class SleeperDriver:
    """关停进程面测试驱动：首轮派发常驻 run 键进程，永不自行收尾。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.controls: list[Any] = []

    def decide(self, world, notifications):
        self.rounds += 1
        if self.rounds == 1:
            return [StartProcess(cmd="sleep 60", cwd="/tmp", context="main")]
        return []

    def on_control(self, control) -> None:
        self.controls.append(control)

    def snapshot(self):
        return {"schema_version": 1, "rounds": self.rounds, "pending_timers": []}


class SleeperFactory:
    format = "fake@1"

    def __init__(self) -> None:
        self.driver: SleeperDriver | None = None

    def create(self, run, program_path, params):
        self.driver = SleeperDriver()
        return self.driver

    def restore(self, run, snapshot, sessions):
        return self.create(run, None, None)


class RecoveryEnv:
    """同库可重装配的 run 平面环境（模拟守护进程重启）。"""

    def __init__(self, tmp_path: Path, factory: ResumingFactory) -> None:
        self.factory = factory
        self.store = RunStore(tmp_path / "events.db")
        self.affiliation = SessionRunAffiliationService(self.store)
        self.bus = _StubBus()
        self.so = RecoverySO(self.affiliation, self.bus, tmp_path)
        self.coord = RunCoordinator(
            orchestrator=self.so,
            run_store=self.store,
            bus=self.bus,
            driver_factories={factory.format: factory},
            affiliation=self.affiliation,
        )
        self.program = tmp_path / f"prog-{time.monotonic_ns()}.txt"
        self.program.write_text("format: fake", encoding="utf-8")

    async def start_run(self) -> str:
        run = await self.coord.start_run(RunConfig(format=self.factory.format), self.program)
        return run.run_id

    async def recover(self, *, skip=()) -> None:
        """重启后的 boot 恢复序：归属缓存重建先于 run 恢复。"""
        await self.affiliation.rebuild()
        await self.coord.recover_at_boot(skip=skip)


async def _until(check, message: str, timeout_s: float = 5.0) -> None:
    """轮询等待异步条件成立（超时判定失败）。"""
    deadline = time.monotonic() + timeout_s
    while not await check():
        if time.monotonic() > deadline:
            pytest.fail(message)
        await asyncio.sleep(0.02)


# ── 崩溃恢复主路径 ───────────────────────────────────────────────


class TestCrashRecovery:
    async def test_crash_recovery_restores_driver_and_completes(self, tmp_path):
        # kill -9 模拟：慢成员在跑时丢弃环境（无 stop 收尾）→ 同库重装配 →
        # boot 恢复按快照重建驱动、续跑新成员至完成
        crashed = RecoveryEnv(tmp_path, ResumingFactory())
        run_id = await crashed.start_run()
        await _until(
            lambda: _run_armed(crashed, run_id),
            "崩溃前成员与快照未落定",
        )
        crashed_sid = (await crashed.store.list_session_ids(run_id))[0]

        rebooted = RecoveryEnv(tmp_path, ResumingFactory())
        await rebooted.recover()

        restored_run_id, blob, members = rebooted.factory.restore_logs[0]
        assert restored_run_id == run_id and members == [crashed_sid]
        assert blob["rounds"] >= 1  # 快照携带崩溃前轮次
        await _until(
            lambda: _finished(rebooted, run_id),
            "恢复后 run 未完成",
        )
        assert (await rebooted.store.get(run_id)).status == "completed"

    async def test_skip_resume_restores_without_spawning(self, tmp_path):
        # 步跳过：restore 执行、resume 跳过 → run 留非终态、零运行时拉起
        crashed = RecoveryEnv(tmp_path, ResumingFactory())
        run_id = await crashed.start_run()
        await _until(lambda: _run_armed(crashed, run_id), "崩溃前成员与快照未落定")

        rebooted = RecoveryEnv(tmp_path, ResumingFactory())
        await rebooted.recover(skip={"resume"})

        assert rebooted.factory.restore_logs  # 驱动重建已执行
        assert (await rebooted.store.get(run_id)).status == "running"
        assert (await rebooted.coord.run_state(run_id))["metrics"] is None

    async def test_skip_classify_then_reentry_completes(self, tmp_path):
        # 步跳过 + 幂等重入：classify 跳过时 pending 原样保留，重入后再判定
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run = await env.store.create("p", "fake@1", status=RunStatus.PENDING)

        await env.coord.recover_at_boot(skip={"classify"})
        assert (await env.store.get(run.run_id)).status == "pending"

        await env.coord.recover_at_boot()
        rec = await env.store.get(run.run_id)
        assert rec.status == "failed" and rec.error == "crash_during_start"


async def _finished(env: RecoveryEnv, run_id: str) -> bool:
    """run 是否已落终态账。"""
    return (await env.store.get(run_id)).finished_at is not None


async def _run_armed(env: RecoveryEnv, run_id: str) -> bool:
    """崩溃前就绪判定：成员归属行在账、驱动快照已落盘。"""
    return bool(await env.store.list_session_ids(run_id)) and (
        env.store.load_driver_state_sync(run_id) is not None
    )


# ── 无驱动 run 的 boot 状态重算 ────────────────────────────────


class TestDriverlessRecovery:
    async def test_boot_recalcs_implicit_run_projection(self, tmp_path):
        # 崩溃时隐式 run 账面停在 running；重启后 boot 恢复要看到它
        # （扫描不过滤 implicit），按 ghost 名册事实重算投影——
        # 成员不在新名册 → paused(projection)，不留幽灵 running
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run = await env.store.create("p", "", kind=RunKind.IMPLICIT)
        await env.affiliation.attach("ghost-sid", run.run_id)

        await env.coord.recover_at_boot()

        rec = await env.store.get(run.run_id)
        assert rec.status == "paused" and rec.pause_reason == "projection"

    async def test_skip_restore_skips_driverless_recalc(self, tmp_path):
        # 无驱动重算随 restore 步名跳过：排障时 boot 不改账面
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run = await env.store.create("p", "", kind=RunKind.IMPLICIT)
        await env.affiliation.attach("ghost-sid", run.run_id)

        await env.coord.recover_at_boot(skip={"restore"})

        rec = await env.store.get(run.run_id)
        assert rec.status == "running" and rec.pause_reason is None

    async def test_boot_recalc_closes_container_with_inflight_ghost_member(
        self,
        tmp_path,
    ):
        # 崩溃时容器成员在跑：重启后 ghost 还原为 IDLE，loop_status 沿用旧值
        # 'yes'（进程已死、不会再产生状态事件）——boot 重算按「无活 runtime」
        # 收尾容器，不留幽灵 running
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run = await env.store.create("p", "", kind=RunKind.CONTAINER)
        env.so.pool.sessions["ghost-c1"] = StubSession(
            "ghost-c1", "c1", state="idle", loop_status="yes"
        )
        await env.affiliation.attach("ghost-c1", run.run_id)

        await env.coord.recover_at_boot()

        assert (await env.store.get(run.run_id)).status == "completed"

    async def test_boot_recalc_keeps_container_open_for_resting_member(
        self,
        tmp_path,
    ):
        # 用户暂停断点（idle+user_pause）可恢复：boot 重算不收尾容器，
        # 与 is_terminal 的暂停豁免语义相同
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run = await env.store.create("p", "", kind=RunKind.CONTAINER)
        env.so.pool.sessions["ghost-c2"] = StubSession(
            "ghost-c2",
            "c2",
            state="idle",
            stop_reason="user_pause",
            loop_status="no",
        )
        await env.affiliation.attach("ghost-c2", run.run_id)

        await env.coord.recover_at_boot()

        assert (await env.store.get(run.run_id)).status == "running"


# ── 补铸与 ghost reviewer 重建（真实编排器）──────────────────────


def _boot_orch(tmp_path: Path):
    from workflow_controller.config.settings import AppConfig, HookServerConfig, ReviewerConfig
    from workflow_controller.core.orchestrator import Orchestrator

    cfg = AppConfig(
        project_dir=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )
    return Orchestrator(cfg, default_project_dir=tmp_path)


def _fake_activate(orch, monkeypatch):
    """Mock _activate_session，但保留显式归属经激活缝挂靠的契约。"""

    async def fake(session, *, prepare=None):
        if session.config.run_id is not None and getattr(orch, "_post_attach_hook") is not None:
            await getattr(orch, "_post_attach_hook")(session)

    monkeypatch.setattr(orch, "_activate_session", fake)


async def _ghost_reviewer_session(tmp_path, monkeypatch):
    """造带 reviewer 配置的成员 session（重启后快照缺位的 ghost 形态）。"""
    orch = _boot_orch(tmp_path)
    _fake_activate(orch, monkeypatch)
    return await orch.start_session_with_config(
        SessionConfig(
            name="n",
            cwd=str(tmp_path),
            reviewer=SessionReviewerConfig(model="some-model"),
        )
    )


async def _recover_with_bare_coordinator(rebooted) -> None:
    """空驱动工厂的协调器上执行 boot 恢复（reviewer 重建用例公共收尾）。"""
    coordinator = RunCoordinator(
        orchestrator=rebooted,
        run_store=rebooted.run_store,
        bus=rebooted.bus,
        driver_factories={},
        affiliation=rebooted.affiliation,
    )
    await coordinator.recover_at_boot()


class TestBootAffiliation:
    async def test_detached_member_mints_implicit_not_reviving_original(
        self,
        tmp_path,
        monkeypatch,
    ):
        # 脱离后的重启落点：归属行已删、旁证已清 → 补铸新隐式 run，
        # 原 run 存活也不补 attach（脱离跨重启不可逆）
        orch = _boot_orch(tmp_path)
        _fake_activate(orch, monkeypatch)
        run = await orch.run_store.create("p", "dag-yaml@1")
        session = await orch.start_session_with_config(
            SessionConfig(name="n", cwd=str(tmp_path), run_id=run.run_id)
        )
        db = tmp_path / ".workflow-controller" / "events.db"
        with sqlite3.connect(db) as conn:  # 脱离已完成的残留态
            conn.execute("DELETE FROM run_sessions WHERE session_id = ?", (session.id,))
            conn.execute(
                "UPDATE sessions SET config_json = json_remove(config_json, '$.run_id')"
                " WHERE id = ?",
                (session.id,),
            )

        rebooted = _boot_orch(tmp_path)
        rebooted.pool.load_sessions()
        await rebooted.affiliation.recover_at_boot(rebooted.pool)

        aff = rebooted.affiliation.resolve(session.id)
        assert aff is not None and aff.run_id != run.run_id
        assert (await rebooted.run_store.get(aff.run_id)).kind == "implicit"
        assert await rebooted.run_store.list_session_ids(run.run_id) == []


class TestGhostReviewerRebuild:
    async def test_boot_rebuilds_reviewer_snapshot_for_ghost(self, tmp_path, monkeypatch):
        # 进程重启后 reviewer 快照缺位（stop-hook 判定被静默放行的恢复期漏洞）；
        # boot 恢复的名册成员对账按持久化 profile 重建
        session = await _ghost_reviewer_session(tmp_path, monkeypatch)

        rebooted = _boot_orch(tmp_path)
        rebooted.pool.load_sessions()
        assert rebooted.reviewer_for(session.id) is None  # 重建前快照仍缺位（复现恢复洞）

        await _recover_with_bare_coordinator(rebooted)
        assert rebooted.reviewer_for(session.id) is not None

    async def test_reviewer_rebuild_survives_missing_runs_table(self, tmp_path, monkeypatch):
        # 旧库升级形态：events.db 在而 runs 族表缺失（尚未铸过 run）——
        # 成员对账先于账面守卫，reviewer 重建不被一并跳过；恢复仍不造库
        session = await _ghost_reviewer_session(tmp_path, monkeypatch)
        db = tmp_path / ".workflow-controller" / "events.db"
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE runs")

        rebooted = _boot_orch(tmp_path)
        rebooted.pool.load_sessions()
        assert rebooted.reviewer_for(session.id) is None

        await _recover_with_bare_coordinator(rebooted)
        assert rebooted.reviewer_for(session.id) is not None
        assert not await rebooted.run_store.has_runs_table()


# ── 关停收尾 ─────────────────────────────────────────────────────


class TestShutdown:
    async def test_shutdown_collects_run_and_keeps_non_terminal(self, tmp_path):
        env = RecoveryEnv(tmp_path, ResumingFactory())
        run_id = await env.start_run()
        await _until(lambda: _run_armed(env, run_id), "关停前成员与快照未落定")

        await env.coord.shutdown()
        await asyncio.sleep(0)  # 让任务退出回调清注册表

        run = await env.store.get(run_id)
        assert run.status == "running" and run.finished_at is None  # 账面保持非终态
        assert any(
            c.kind == ControlKind.STOPPED for c in env.factory.driver.controls
        )  # stopped 进驱动收尾轮
        assert env.so.stopped  # 成员经终态序列停止
        assert (await env.coord.run_state(run_id))["metrics"] is None  # 运行时已收尾

    async def test_shutdown_kills_run_key_processes(self, tmp_path):
        # 关停不遗漏 run 键进程：run 任务收尾退出后，终态序列的 kill_processes
        # 是注册表进程组唯一的杀进程点（setsid 组不随父进程退出），漏杀即
        # 孤儿进程 + 下次 boot 恢复续跑双写
        env = RecoveryEnv(tmp_path, SleeperFactory())
        run_id = await env.start_run()

        async def _p1_registered() -> bool:
            return bool(
                (getattr(getattr(env.coord, "_host"), "_processes").get(run_id) or {}).get("p1")
            )

        await _until(_p1_registered, "关停前 run 键进程未落注册表")
        pid = getattr(getattr(env.coord, "_host"), "_processes")[run_id]["p1"].proc.pid

        await env.coord.shutdown()

        assert not getattr(getattr(env.coord, "_host"), "_processes").get(
            run_id
        )  # 注册表已随杀进程回收
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # OS 级确认：进程确已死亡
        run = await env.store.get(run_id)
        assert run.status == "running" and run.finished_at is None  # 账面非终态

    async def test_shutdown_persists_final_snapshot_on_finish_after_stop(
        self,
        tmp_path,
    ):
        # 终态控制已受理 + 驱动裁决 FinishRun：循环直接退出、不走收尾轮的边界
        # 快照——此路仍须落盘最终驱动状态，否则关停优雅分支整体丢快照，
        # 下次 boot 只能拿陈旧快照续跑
        env = RecoveryEnv(tmp_path, CooperatingStopFactory())
        run_id = await env.start_run()

        async def snapshot_on_disk() -> bool:
            return env.store.load_driver_state_sync(run_id) is not None

        await _until(snapshot_on_disk, "边界快照未落盘")
        assert env.factory.driver.finished is False

        await env.coord.shutdown()
        await asyncio.sleep(0)  # 让任务退出回调清注册表

        blob = env.store.load_driver_state_sync(run_id)
        assert blob is not None and blob["finished"] is True  # 最终裁决已落盘
        run = await env.store.get(run_id)
        assert run.status == "running" and run.finished_at is None  # 终态优先权在控制侧

    async def test_orchestrator_shutdown_runs_registered_steps(self, tmp_path):
        orch = _boot_orch(tmp_path)
        called: list[str] = []

        async def step() -> None:
            called.append("run-plane")

        orch.register_shutdown_step(step)
        await orch.shutdown()
        assert called == ["run-plane"]  # 先于池停止的关停步骤位

    async def test_shutdown_step_failure_does_not_block_rest(self, tmp_path):
        # 单个关停步骤抛异常只留痕：后续步骤与关停序照常执行
        orch = _boot_orch(tmp_path)
        called: list[str] = []

        async def boom() -> None:
            raise RuntimeError("step blew up")

        async def after() -> None:
            called.append("after")

        orch.register_shutdown_step(boom)
        orch.register_shutdown_step(after)
        await orch.shutdown()
        assert called == ["after"]

    async def test_shutdown_surrogate_snapshot_waits_for_thread_exit(
        self,
        tmp_path,
        monkeypatch,
    ):
        # 慢而未死的驱动线程：代办快照要等线程退出后才执行——task.cancel
        # 杀不死执行中的 decide 线程，线程退出（而非任务退出）才是单写者；
        # 快照干净落盘，关停有界返回
        monkeypatch.setattr(run_coordinator, "STOP_DRAIN_TIMEOUT_S", 0.25)
        env = RecoveryEnv(tmp_path, StuckFactory(stuck_s=0.6))
        run_id = await env.start_run()

        async def in_decide() -> bool:
            return env.factory.driver.rounds >= 1

        await _until(in_decide, "驱动未进入 decide")
        assert env.store.load_driver_state_sync(run_id) is None  # 无边界快照

        persist_witness: dict[str, bool] = {}
        orig_persist = getattr(env.coord, "_host").persist_driver_state

        async def persist_after_thread_exit(rid: str, blob, **kw):
            task = getattr(env.coord, "_tasks").get(rid)
            persist_witness["task_done"] = task is None or task.done()
            # decide 线程已返回，不只是 run 任务已死
            persist_witness["thread_done"] = env.factory.driver.finished.is_set()
            return await orig_persist(rid, blob, **kw)

        getattr(env.coord, "_host").persist_driver_state = persist_after_thread_exit

        t0 = time.monotonic()
        await env.coord.shutdown()
        assert time.monotonic() - t0 < 5.0

        blob = env.store.load_driver_state_sync(run_id)
        assert blob is not None and blob["rounds"] == 1  # 代办快照已落盘
        assert persist_witness.get("task_done") is True
        assert persist_witness.get("thread_done") is True  # 线程退出后才持久化

    async def test_shutdown_surrogate_snapshot_skipped_while_thread_stuck(
        self,
        tmp_path,
        monkeypatch,
    ):
        # 永久卡死线程：在途驱动线程等不到退出时放弃代办快照——撕裂内存不
        # 作为恢复源落盘（无快照的 boot 恢复落确定性失败）；关停仍有界返回，
        # 被弃线程事后可退出
        monkeypatch.setattr(run_coordinator, "STOP_DRAIN_TIMEOUT_S", 0.25)
        monkeypatch.setattr(run_coordinator, "SURROGATE_THREAD_DRAIN_S", 0.25)
        env = RecoveryEnv(tmp_path, StuckFactory())
        run_id = await env.start_run()

        async def in_decide() -> bool:
            return env.factory.driver.rounds >= 1

        await _until(in_decide, "驱动未进入 decide")

        t0 = time.monotonic()
        await env.coord.shutdown()
        assert time.monotonic() - t0 < 5.0  # 弃快照不挂关停

        assert env.store.load_driver_state_sync(run_id) is None  # 不落撕裂快照

        env.factory.driver.release.set()
        assert env.factory.driver.finished.wait(timeout=5)  # 被弃线程可退出

    async def test_shutdown_tolerates_third_party_task_cancel(
        self,
        tmp_path,
        monkeypatch,
    ):
        # 排空窗内 run 任务被并发路径取消（stop_run 超时兜底 / delete_run
        # 链）：CancelledError 不得从 shutdown 向外抛出、打断后续关停序——
        # 按意外退场留痕，终态序列照常执行
        monkeypatch.setattr(run_coordinator, "STOP_DRAIN_TIMEOUT_S", 0.5)
        env = RecoveryEnv(tmp_path, StuckFactory())
        run_id = await env.start_run()

        async def in_decide() -> bool:
            return env.factory.driver.rounds >= 1

        await _until(in_decide, "驱动未进入 decide")
        task = getattr(env.coord, "_tasks")[run_id]

        # STOPPED 投递即取消 run 任务：cancel 恰落在排空 await 之前，模拟
        # 并发 stop_run 超时兜底对同一任务的取消
        orig_notify = getattr(env.coord, "_notify")

        def notify_then_cancel(rid, notification):
            orig_notify(rid, notification)
            if notification.kind == ControlKind.STOPPED:
                getattr(env.coord, "_tasks")[rid].cancel()

        monkeypatch.setattr(env.coord, "_notify", notify_then_cancel)

        seq_runs: list[str] = []
        orig_seq = getattr(env.coord, "_terminal").sequence

        async def seq_spy(rid, **kw):
            seq_runs.append(rid)
            return await orig_seq(rid, **kw)

        getattr(env.coord, "_terminal").sequence = seq_spy

        await env.coord.shutdown()  # 不抛 CancelledError

        assert task.cancelled()
        assert run_id in seq_runs  # 终态序列照常执行
        assert (await env.store.get(run_id)).status == "running"  # 账面仍非终态

        env.factory.driver.release.set()
        assert env.factory.driver.finished.wait(timeout=5)

    async def test_shutdown_tolerates_cancel_absorbing_task(
        self,
        tmp_path,
        monkeypatch,
    ):
        # 取消被收尾路径吸收的 run 任务（shield 段 / except CancelledError
        # 清理的现实形态）：等不到任务退出即放弃代办快照，关停有界返回、
        # 终态序列照常执行——await task 不得无界挂死整条关停序
        monkeypatch.setattr(run_coordinator, "STOP_DRAIN_TIMEOUT_S", 0.25)
        monkeypatch.setattr(run_coordinator, "TASK_CANCEL_EXIT_TIMEOUT_S", 0.25)
        env = RecoveryEnv(tmp_path, StuckFactory())
        orig_loop = getattr(env.coord, "_run_loop")

        async def absorbing_loop(rt):
            try:
                await orig_loop(rt)
            except asyncio.CancelledError:
                await asyncio.sleep(30)  # 吸收取消：任务不退出

        monkeypatch.setattr(env.coord, "_run_loop", absorbing_loop)  # _spawn 取实例属性，拉起即生效
        run_id = await env.start_run()

        async def in_decide() -> bool:
            return env.factory.driver.rounds >= 1

        await _until(in_decide, "驱动未进入 decide")
        task = getattr(env.coord, "_tasks")[run_id]

        seq_runs: list[str] = []
        orig_seq = getattr(env.coord, "_terminal").sequence

        async def seq_spy(rid, **kw):
            seq_runs.append(rid)
            return await orig_seq(rid, **kw)

        getattr(env.coord, "_terminal").sequence = seq_spy

        t0 = time.monotonic()
        await env.coord.shutdown()
        assert time.monotonic() - t0 < 5.0  # 等不到退出不挂关停

        assert not task.done()  # 僵尸任务确未退出（验证测试形态成立）
        assert (
            env.store.load_driver_state_sync(run_id) is None
        )  # 任务未退出即无单写者，不落代办快照
        assert run_id in seq_runs  # 终态序列照常执行
        assert (await env.store.get(run_id)).status == "running"  # 账面仍非终态

        task.cancel()  # 清场：僵尸任务二次取消即退出
        with contextlib.suppress(BaseException):
            await task
        env.factory.driver.release.set()
        assert env.factory.driver.finished.wait(timeout=5)
