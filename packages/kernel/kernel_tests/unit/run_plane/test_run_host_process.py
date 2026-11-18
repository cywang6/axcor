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

"""RunHost 进程面与产物探测：进程生命周期、有界输出、定时器与 read_artifact。"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from workflow_controller.core import run_host
from workflow_controller.core.run_coordinator import RunConfig, RunCoordinator, RunHost
from workflow_controller.core.runs import RunKind, RunStatus, RunStore
from workflow_controller.core.session_run_affiliation import SessionRunAffiliationService
from workflow_controller.plugin.run_driver import (
    ArmTimer,
    ArtifactRead,
    CancelTimer,
    ControlNotification,
    FinishOutcome,
    FinishRun,
    KillProcesses,
    ProcessExit,
    ProcessInfo,
    ProcessOutput,
    ReadArtifact,
    RunNotification,
    StartProcess,
    TimerFired,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── 测试替身 ──────────────────────────────────────────────────────


class RunEventRecorder:
    """run_events 落库门面替身：按 (run_id, event_type) 收集。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any], str | None]] = []

    @property
    def default_store(self) -> RunEventRecorder:
        return self

    async def store_run_event(
        self, run_id: str, event_type: str, data: dict[str, Any], *, context: str | None = None
    ) -> None:
        self.events.append((run_id, event_type, dict(data), context))

    async def store_run_events_batch(
        self, run_id: str, events: list[tuple[str, dict[str, Any]]], *, context: str | None = None
    ) -> None:
        for event_type, data in events:
            self.events.append((run_id, event_type, dict(data), context))

    def rows(self, run_id: str, event_type: str) -> list[dict[str, Any]]:
        return [data for rid, et, data, _ in self.events if rid == run_id and et == event_type]


class FakeBus:
    def __init__(self) -> None:
        self.observers: dict[str, list] = {}

    def observe(self, event_type, handler) -> None:
        self.observers.setdefault(getattr(event_type, "value", str(event_type)), []).append(handler)


class FakeSO:
    """进程测试用 SO 替身：无成员 session，仅提供路径锚与事件门面。"""

    def __init__(self, project_dir: Path) -> None:
        self.default_project_dir = project_dir
        self.event_router = RunEventRecorder()

    @staticmethod
    def get_session_facts(sid: str) -> None:
        return None

    @staticmethod
    def is_terminal(sid: str) -> bool:
        return True

    async def stop_session(self, sid: str) -> None:
        return None


class ProcDriver:
    """进程测试驱动基类：首轮动作固定，各类通知全量记录。"""

    def __init__(self, first_actions: list[Any]) -> None:
        self.first_actions = first_actions
        self.rounds = 0
        self.outputs: list[ProcessOutput] = []
        self.exits: list[ProcessExit] = []
        self.timers: list[TimerFired] = []
        self.controls: list[ControlNotification] = []
        self.world_processes: tuple[ProcessInfo, ...] = ()

    @staticmethod
    def projection():
        return {"schema_version": 1}

    @staticmethod
    def snapshot():
        return {"schema_version": 1, "pending_timers": []}

    @staticmethod
    def handle_op(op, args):
        return {}

    def decide(self, world, notifications):
        self.rounds += 1
        self._collect(notifications)
        if self.rounds == 1:
            return list(self.first_actions)
        return []

    def on_control(self, control) -> None:
        if isinstance(control, ControlNotification):
            self.controls.append(control)

    def _collect(self, notifications) -> None:
        for n in notifications:
            if isinstance(n, ProcessOutput):
                self.outputs.append(n)
            elif isinstance(n, ProcessExit):
                self.exits.append(n)
            elif isinstance(n, TimerFired):
                self.timers.append(n)
            elif isinstance(n, ControlNotification):
                self.controls.append(n)


class Harness:
    def __init__(self, tmp_path: Path, driver: ProcDriver) -> None:
        self.store = RunStore(tmp_path / "events.db")
        self.affiliation = SessionRunAffiliationService(self.store)
        self.bus = FakeBus()
        self.so = FakeSO(tmp_path)
        self.driver = driver
        factory = SimpleNamespace(
            format="proc@1",
            create=lambda run, program, params: driver,
            restore=lambda run, snapshot, sessions: driver,
        )
        self.coord = RunCoordinator(
            orchestrator=self.so,
            run_store=self.store,
            bus=self.bus,
            driver_factories={"proc@1": factory},
            affiliation=self.affiliation,
        )
        self.recorder = self.so.event_router
        self.program = tmp_path / "prog.txt"
        self.program.write_text("format: proc", encoding="utf-8")

    async def start(self, *, work_dir: str | None = None) -> str:
        run = await self.coord.start_run(
            RunConfig(format="proc@1", work_dir=work_dir), self.program
        )
        return run.run_id

    async def await_terminal(self, run_id: str, timeout_s: float = 10.0) -> str:
        async def _poll() -> str:
            while True:
                run = await self.store.get(run_id)
                if run.finished_at is not None:
                    return run.status
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(_poll(), timeout_s)

    async def await_exit_events(self, run_id: str, count: int, timeout_s: float = 8.0) -> None:
        async def _poll() -> None:
            while len(self.recorder.rows(run_id, "process_exit")) < count:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_poll(), timeout_s)


# ── 进程生命周期 ──────────────────────────────────────────────────


class TestProcessLifecycle:
    async def test_stream_output_and_exit(self, tmp_path):
        class EchoDriver(ProcDriver):
            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [StartProcess(cmd="echo hello", cwd="/tmp", context="c1")]
                self.world_processes = world.processes
                if self.exits:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        h = Harness(tmp_path, EchoDriver([]))
        run_id = await h.start()
        status = await h.await_terminal(run_id)
        driver: EchoDriver = h.driver  # type: ignore[assignment]
        assert status == "completed"
        assert driver.exits == [ProcessExit(proc_id="p1", context="c1", returncode=0)]
        assert [o.line for o in driver.outputs] == ["hello"]
        assert driver.world_processes == (ProcessInfo(proc_id="p1", context="c1", returncode=0),)
        assert h.recorder.rows(run_id, "process_output") == [{"proc_id": "p1", "line": "hello"}]
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["returncode"] == 0 and exit_row["tail"] == ["hello"]
        assert getattr(h.coord, "_host").process_infos(run_id) == []  # 终态清理后注册表空

    async def test_output_rows_bounded(self, tmp_path):
        class SeqDriver(ProcDriver):
            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [StartProcess(cmd="seq 1 3000", cwd="/tmp")]
                if self.exits:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        h = Harness(tmp_path, SeqDriver([]))
        run_id = await h.start()
        await h.await_terminal(run_id)
        assert len(h.recorder.rows(run_id, "process_output")) == 500  # 单进程落库行数上限
        assert len(h.driver.outputs) == 3000  # 流式通知不截流
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["lines"] == 3000 and exit_row["dropped"] == 2500
        assert exit_row["tail"][-1] == "3000"

    async def test_overlong_line_truncated_in_buffer(self, tmp_path):
        class BlobDriver(ProcDriver):
            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [
                        StartProcess(
                            cmd="head -c 300000 /dev/zero | tr '\\0' 'x'; printf '\\nlast\\n'",
                            cwd="/tmp",
                        )
                    ]
                if self.exits:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        h = Harness(tmp_path, BlobDriver([]))
        run_id = await h.start()
        await h.await_terminal(run_id)
        rows = h.recorder.rows(run_id, "process_output")
        assert [len(r["line"]) for r in rows] == [2000, 4]  # 超长行截断，后续行不受污染
        assert rows[0]["line"] == "x" * 2000 and rows[1]["line"] == "last"
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["lines"] == 2 and exit_row["dropped"] == 0

    async def test_stream_false_tail_only(self, tmp_path):
        class QuietDriver(ProcDriver):
            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [StartProcess(cmd="printf 'l1\nl2\nl3\n'", cwd="/tmp", stream=False)]
                if self.exits:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        h = Harness(tmp_path, QuietDriver([]))
        run_id = await h.start()
        await h.await_terminal(run_id)
        assert h.driver.outputs == []  # stream=false 不逐行通知
        assert h.recorder.rows(run_id, "process_output") == []  # 也不逐行落库
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["tail"] == ["l1", "l2", "l3"]  # 尾部聚合是唯一输出通道

    async def test_unbound_empty_run_id_skips_persistence(self, tmp_path):
        """已删除 run（forget_run）的残余输出采集不落库，通知照常回投。"""
        so = FakeSO(tmp_path)
        host = RunHost(
            so,
            None,
            None,
            lambda run_id, notification: None,
            lambda run_id: tmp_path,
            FakeBus(),
        )
        host.forget_run("dead-run")
        host.start_process(
            "dead-run",
            "echo hi",
            str(tmp_path),
            context="node=b1",
        )
        while getattr(host, "_spawn_tasks").get("dead-run") or not getattr(host, "_processes").get(
            "dead-run"
        ):
            await asyncio.sleep(0.01)
        await host.kill_processes("dead-run")
        assert so.event_router.events == []  # run_events 零残留行

    async def test_kill_escalates_to_sigkill(self, tmp_path):
        class KillDriver(ProcDriver):
            def __init__(self) -> None:
                super().__init__([])
                self.killed = False

            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [StartProcess(cmd="trap '' TERM; echo ready; sleep 30", cwd="/tmp")]
                if self.exits:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                if not self.killed and any(o.line == "ready" for o in self.outputs):
                    self.killed = True
                    return [KillProcesses()]
                return []

        h = Harness(tmp_path, KillDriver())
        run_id = await h.start()
        status = await h.await_terminal(run_id, timeout_s=15.0)
        assert status == "completed"
        assert h.driver.exits[0].returncode == -9  # 忽略 TERM 的进程被 SIGKILL 收口
        assert getattr(h.coord, "_host").process_infos(run_id) == []


# ── 终态窗口迟到进程 ──────────────────────────────────────────────


class TestTerminalWindowSpawn:
    async def test_final_batch_process_then_finish_killed(self, tmp_path):
        """末批 [StartProcess, FinishRun]：置杀前排空 spawn，进程孤儿零残留。"""
        driver = ProcDriver(
            [StartProcess(cmd="sleep 60", cwd="/tmp"), FinishRun(FinishOutcome.COMPLETED)]
        )
        h = Harness(tmp_path, driver)
        run_id = await h.start()
        status = await h.await_terminal(run_id)
        assert status == "completed"
        await h.await_exit_events(run_id, 1)  # 迟到 spawn 被置杀并留痕退出事件
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["returncode"] < 0
        assert getattr(h.coord, "_host").process_infos(run_id) == []

    async def test_stop_cleanup_round_spawn_killed(self, tmp_path):
        class CleanupDriver(ProcDriver):
            def __init__(self) -> None:
                super().__init__([])
                self.cleanup_spawned = False

            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [StartProcess(cmd="sleep 60", cwd="/tmp", context="main")]
                if self.controls and not self.cleanup_spawned:
                    self.cleanup_spawned = True
                    return [StartProcess(cmd="sleep 60", cwd="/tmp", context="cleanup")]
                return []

        h = Harness(tmp_path, CleanupDriver())
        run_id = await h.start()
        record = await h.coord.stop_run(run_id)
        assert record.status == "stopped"
        await h.await_exit_events(run_id, 2)  # 常规进程与收尾轮进程都被置杀
        assert all(row["returncode"] < 0 for row in h.recorder.rows(run_id, "process_exit"))

    async def test_stop_drains_inflight_spawn_then_kills(self, monkeypatch, tmp_path):
        """置杀时 spawn 已派发未落定（注册表无项）：排空落定后必被杀。"""
        driver = ProcDriver([StartProcess(cmd="sleep 60", cwd="/tmp")])
        h = Harness(tmp_path, driver)
        host = getattr(h.coord, "_host")
        gate = asyncio.Event()
        orig_spawn = getattr(host, "_spawn_process")

        async def gated_spawn(*args, **kwargs):
            await gate.wait()
            await orig_spawn(*args, **kwargs)

        monkeypatch.setattr(host, "_spawn_process", gated_spawn)  # type: ignore[method-assign]
        run_id = await h.start()
        for _ in range(500):
            if getattr(host, "_spawn_tasks").get(run_id):
                break
            await asyncio.sleep(0.01)
        assert getattr(host, "_spawn_tasks").get(run_id), "StartProcess 应已派发"

        async def _release() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        releaser = asyncio.get_running_loop().create_task(_release())
        record = await h.coord.stop_run(run_id)
        await releaser
        assert record.status == "stopped"
        await h.await_exit_events(run_id, 1)
        (exit_row,) = h.recorder.rows(run_id, "process_exit")
        assert exit_row["returncode"] < 0
        assert host.process_infos(run_id) == []


# ── kill 契约：内建排空 + 终局标记 ────────────────────────────────


class TestKillContract:
    @staticmethod
    def _host(tmp_path: Path) -> tuple[RunHost, list[RunNotification]]:
        so = FakeSO(tmp_path)
        notes: list[RunNotification] = []
        host = RunHost(
            so,
            None,
            None,
            lambda rid, note: notes.append(note),
            lambda rid: tmp_path,
            FakeBus(),
        )
        return host, notes

    @staticmethod
    async def _exit_note(notes: list[RunNotification], proc_id: str) -> ProcessExit:
        """等指定进程的退出通知并取回。"""
        for _ in range(500):
            exits = [n for n in notes if isinstance(n, ProcessExit) and n.proc_id == proc_id]
            if exits:
                return exits[0]
            await asyncio.sleep(0.01)
        raise AssertionError(f"进程 {proc_id} 未在时限内退出")

    @staticmethod
    async def _await_exit_with_timeout(
        notes: list[RunNotification],
        proc_id: str,
    ) -> ProcessExit:
        """带硬上界地等退出通知（孙进程场景 kill 回执可能晚于宽限窗）。"""
        for _ in range(1000):
            exits = [n for n in notes if isinstance(n, ProcessExit) and n.proc_id == proc_id]
            if exits:
                return exits[0]
            await asyncio.sleep(0.01)
        raise AssertionError(f"进程 {proc_id} 未在时限内退出")

    async def test_spawn_failure_marked(self, tmp_path, monkeypatch):
        host, notes = self._host(tmp_path)

        async def fail_exec(*args, **kwargs):
            raise OSError("nope")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)
        pid = host.start_process("r1", "true", "/tmp", context="c")
        note = await self._exit_note(notes, pid)
        assert note.returncode == -1
        assert note.exit_mark == "spawn_failed"

    async def test_plane_kill_marked(self, tmp_path):
        host, notes = self._host(tmp_path)
        pid = host.start_process("r2", "sleep 30", "/tmp", context="c")
        while not getattr(host, "_processes").get("r2"):
            await asyncio.sleep(0.01)
        kill_task = asyncio.create_task(host.kill_processes("r2"))
        note = await self._exit_note(notes, pid)
        await kill_task
        assert note.returncode < 0
        assert note.exit_mark == "plane_killed"

    async def test_normal_exit_unmarked(self, tmp_path):
        host, notes = self._host(tmp_path)
        pid = host.start_process("r3", "true", "/tmp", context="c")
        note = await self._exit_note(notes, pid)
        assert note.returncode == 0
        assert note.exit_mark is None

    async def test_kill_marks_only_inflight_entries(self, tmp_path):
        """已退出的注册表残留条目不打组杀标记：其退出通知早已发出。"""
        host, notes = self._host(tmp_path)
        dead = host.start_process("r4", "true", "/tmp", context="c")
        await self._exit_note(notes, dead)
        live = host.start_process("r4", "sleep 30", "/tmp", context="c")
        while len(getattr(host, "_processes").get("r4") or {}) < 2:
            await asyncio.sleep(0.01)
        kill_task = asyncio.create_task(host.kill_processes("r4"))
        note = await self._exit_note(notes, live)
        await kill_task
        assert note.returncode < 0
        assert note.exit_mark == "plane_killed"
        dead_notes = [n for n in notes if isinstance(n, ProcessExit) and n.proc_id == dead]
        assert len(dead_notes) == 1 and dead_notes[0].exit_mark is None

    async def test_kill_drains_inflight_spawn(self, monkeypatch, tmp_path):
        """kill 入口排空在途 spawn：已派发未落定的进程不漏杀。"""
        host, notes = self._host(tmp_path)
        gate = asyncio.Event()
        orig_spawn = getattr(host, "_spawn_process")

        async def gated_spawn(*args, **kwargs):
            await gate.wait()
            await orig_spawn(*args, **kwargs)

        monkeypatch.setattr(host, "_spawn_process", gated_spawn)  # type: ignore[method-assign]
        pid = host.start_process("r1", "sleep 30", "/tmp", context="c")
        assert getattr(host, "_spawn_tasks").get("r1"), "spawn 应已派发未落定"

        async def _release() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        releaser = asyncio.get_running_loop().create_task(_release())
        await host.kill_processes("r1")
        await releaser
        note = await self._exit_note(notes, pid)
        assert note.returncode < 0  # spawn 落定即被杀，未静默漏杀
        assert host.process_infos("r1") == []

    async def test_kill_settles_despite_escaped_stdout_holder(self, tmp_path):
        """孙进程 setsid 逃逸组杀仍握 stdout 写端：kill 回执直接发退出通知，不被读循环 EOF 门控。"""
        host, notes = self._host(tmp_path)
        pidfile = tmp_path / "escape.pid"
        holder = shlex.quote(sys.executable)
        cmd = (
            f"{holder} -c 'import os,time; os.setsid();"
            ' open("escape.pid", "w").write(str(os.getpid())); time.sleep(30)\''
            " & sleep 30"
        )
        pid = host.start_process("r7", cmd, str(tmp_path), context="c")
        while not getattr(host, "_processes").get("r7") or not pidfile.exists():
            await asyncio.sleep(0.01)
        kill_task = asyncio.create_task(host.kill_processes("r7"))
        note = await self._await_exit_with_timeout(notes, pid)
        await kill_task
        assert note.returncode < 0
        assert note.exit_mark == "plane_killed"
        assert host.process_infos("r7") == []
        os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)  # 放读循环 EOF
        for _ in range(250):
            if getattr(host, "_orch").event_router.rows("r7", "process_exit"):
                break
            await asyncio.sleep(0.02)
        (exit_row,) = getattr(host, "_orch").event_router.rows("r7", "process_exit")
        assert exit_row["returncode"] == note.returncode  # 采集任务后续收口，二次通知去重
        exit_notes = [n for n in notes if isinstance(n, ProcessExit) and n.proc_id == pid]
        assert len(exit_notes) == 1  # 组杀直发后采集收尾不再重复通知


# ── 定时器 ────────────────────────────────────────────────────────


class TestTimers:
    async def test_arm_fire_and_cancel(self, tmp_path):
        class TimerDriver(ProcDriver):
            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                if self.rounds == 1:
                    return [
                        ArmTimer(timer_id="t1", delay_s=0.05),
                        ArmTimer(timer_id="t2", delay_s=30.0),
                        CancelTimer(timer_id="t2"),
                        CancelTimer(timer_id="ghost"),  # 未知 id 幂等 no-op
                    ]
                if self.timers:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        h = Harness(tmp_path, TimerDriver([]))
        run_id = await h.start()
        status = await h.await_terminal(run_id)
        assert status == "completed"
        assert h.driver.timers == [TimerFired(timer_id="t1")]
        assert (
            getattr(getattr(h.coord, "_host"), "_timers") == {}
        )  # t2 已取消，终态清理后无残留句柄


# ── 产物探测 ──────────────────────────────────────────────────────


class TestReadArtifact:
    @staticmethod
    async def _make_host(
        tmp_path: Path,
    ) -> tuple[RunHost, list[RunNotification], RunStore]:
        store = RunStore(tmp_path / "events.db")
        affiliation = SessionRunAffiliationService(store)
        so = FakeSO(tmp_path)
        notes: list[RunNotification] = []
        host = RunHost(
            so,
            store,
            affiliation,
            lambda rid, note: notes.append(note),
            lambda rid: tmp_path / "runs" / rid,
            FakeBus(),
        )
        return host, notes, store

    async def test_payload_matrix(self, tmp_path):
        host, notes, store = await self._make_host(tmp_path)
        work = tmp_path / "wd"
        work.mkdir()
        (work / "ok.json").write_text('{"status": "pass", "count": 3}', encoding="utf-8")
        (work / "arr.json").write_text('{"items": [1]}', encoding="utf-8")
        (work / "null.json").write_text('{"v": null}', encoding="utf-8")
        (work / "plain.txt").write_text("  hello \n", encoding="utf-8")
        run = await store.create(
            "", "", status=RunStatus.RUNNING, kind=RunKind.CONTAINER, work_dir=str(work)
        )
        cases = [
            ("ok.json", "status", "pass", None),
            ("ok.json", "count", "3", None),
            ("ok.json", "missing", None, "缺少字段"),
            ("arr.json", "items", None, "非标量"),
            ("null.json", "v", None, "为 null"),
            ("plain.txt", None, "hello", None),
            ("ghost.json", None, None, None),
        ]
        for path, field, _, _ in cases:
            await host.read_artifact(run.run_id, path, field, context="ctx")
        by_key = {(n.path, n.field): n for n in notes if isinstance(n, ArtifactRead)}
        for path, field, value, error in cases:
            note = by_key[(path, field)]
            assert note.context == "ctx"
            assert note.value == value
            if error is None:
                assert note.error is None
            else:
                assert note.error is not None and error in note.error
        assert by_key[("ghost.json", None)].exists is False
        assert all(n.exists for (path, _), n in by_key.items() if path != "ghost.json")

    async def test_path_escape_rejected(self, tmp_path):
        host, notes, store = await self._make_host(tmp_path)
        work = tmp_path / "wd"
        work.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("s3cret", encoding="utf-8")
        run = await store.create(
            "", "", status=RunStatus.RUNNING, kind=RunKind.CONTAINER, work_dir=str(work)
        )
        for path in ("../secret.txt", "sub/../../secret.txt", str(secret)):
            await host.read_artifact(run.run_id, path, None, context="ctx")
        assert len(notes) == 3
        for note in notes:
            assert isinstance(note, ArtifactRead)
            assert note.exists is False and note.value is None and note.error is None

    async def test_oversized_file_folds_to_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_host, "_ARTIFACT_MAX_BYTES", 8)
        host, notes, store = await self._make_host(tmp_path)
        work = tmp_path / "wd"
        work.mkdir()
        (work / "big.json").write_text('{"status": "pass"}', encoding="utf-8")
        run = await store.create(
            "", "", status=RunStatus.RUNNING, kind=RunKind.CONTAINER, work_dir=str(work)
        )
        await host.read_artifact(run.run_id, "big.json", "status", context="ctx")
        (note,) = notes
        assert isinstance(note, ArtifactRead)
        assert note.exists and note.value is None and note.error == "文件过大"

    async def test_work_dir_defaults_to_run_root(self, tmp_path):
        host, notes, store = await self._make_host(tmp_path)
        run = await store.create("", "", status=RunStatus.RUNNING, kind=RunKind.CONTAINER)
        default_work = tmp_path / "runs" / run.run_id / "work"
        default_work.mkdir(parents=True)
        (default_work / "f.txt").write_text(" fallback ", encoding="utf-8")
        await host.read_artifact(run.run_id, "f.txt", None, None)
        (note,) = [n for n in notes if isinstance(n, ArtifactRead)]
        assert note.exists and note.value == "fallback"

    async def test_driver_roundtrip(self, tmp_path):
        class ReadDriver(ProcDriver):
            def __init__(self) -> None:
                super().__init__([])
                self.reads: list[ArtifactRead] = []

            def decide(self, world, notifications):
                self.rounds += 1
                self._collect(notifications)
                for n in notifications:
                    if isinstance(n, ArtifactRead):
                        self.reads.append(n)
                if self.rounds == 1:
                    return [ReadArtifact(path="ok.json", field="status", context="probe")]
                if self.reads:
                    return [FinishRun(FinishOutcome.COMPLETED)]
                return []

        work = tmp_path / "wd"
        work.mkdir()
        (work / "ok.json").write_text('{"status": "pass"}', encoding="utf-8")
        h = Harness(tmp_path, ReadDriver())
        run_id = await h.start(work_dir=str(work))
        status = await h.await_terminal(run_id)
        driver: ReadDriver = h.driver  # type: ignore[assignment]
        assert status == "completed"
        assert driver.reads[0].value == "pass" and driver.reads[0].exists
