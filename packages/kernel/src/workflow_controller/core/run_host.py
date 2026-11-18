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

"""RunHost 成员执行服务面与 run 键进程辅助类型。"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import json
import logging
import os
import signal
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from workflow_controller.config.settings import LoopConfig, SessionConfig
from workflow_controller.plugin import run_driver
from workflow_controller.plugin.run_driver import (
    ArtifactRead,
    ProcessExit,
    ProcessInfo,
    ProcessOutput,
    RestartPolicy,
    RunNotification,
    SessionActivated,
    SessionSpec,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.core.runs import RunStore
    from workflow_controller.core.session_run_affiliation import SessionRunAffiliationService
    from workflow_controller.event.event_bus import EventBus

logger = logging.getLogger(__name__)

KILL_GRACE_S = 2.0  # 进程组 SIGTERM 后等自然退出的宽限，超时补 SIGKILL
STOP_DRAIN_TIMEOUT_S = 10.0  # stop 后等 run 任务自行收尾的上限，超时 cancel 兜底
_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024  # read_artifact 单文件载入上限（超限折叠为判假载荷，不载入）
_PROC_LINE_MAX_LEN = 2000  # 进程输出单行截断上限
_PROC_LINE_BUDGET = 500  # 单进程落库行数上限（超出计 dropped 不落行）
_PROC_TAIL_LINES = 50  # 退出事件携带的尾部聚合行数
_PROC_READ_CHUNK = 65536  # stdout 读块大小（读块内行聚合单事务落库）
_RESERVED_OVERRIDES = frozenset(
    {"reviewer", "reviewer_profile"}
)  # reviewer 面不进 config_overrides 透传（显式拒绝）
_HOST_INJECTED_CONFIG = frozenset(
    {"run_id", "role"}
)  # 宿主注入字段：Start 侧组装后回填，Restart 侧部分更新前剔除


class _ProcessHandle(Protocol):
    """进程句柄的消费面约定：组杀用 pid、收尾 await wait()、读 stdout 采集输出。"""

    pid: int
    stdout: asyncio.StreamReader | None

    def wait(self) -> Awaitable[int]:
        raise NotImplementedError


@dataclass
class _ProcEntry:
    """run 键进程注册表项（退出后保留 returncode 至 KillProcesses/清理）。"""

    proc: _ProcessHandle
    context: str | None = None
    returncode: int | None = None
    output_task: asyncio.Task[None] | None = None  # 输出采集任务（读 stdout 落库）


@dataclass
class _StreamStats:
    """单进程输出落库记账（行数上限/丢弃数/尾部聚合）。"""

    total: int = 0
    written: int = 0
    dropped: int = 0
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=_PROC_TAIL_LINES))


class RunHost:
    """成员执行服务面：动作的执行器；session 面直调 SO 原语。"""

    def __init__(
        self,
        orchestrator: Orchestrator,
        run_store: RunStore,
        affiliation: SessionRunAffiliationService,
        notifier: Callable[[str, RunNotification], None],
        run_root: Callable[[str], Path],
        bus: EventBus,
    ) -> None:
        self._orch = orchestrator
        self._run_store = run_store
        self._affiliation = affiliation
        self._notifier = notifier
        self._run_root = run_root
        self._bus = bus
        self._supervised: dict[
            str, set[asyncio.Task[None]]
        ] = {}  # run_id → 在途出生/重启任务集（收尾扫掠前排空）
        self._processes: dict[
            str, dict[str, _ProcEntry]
        ] = {}  # run_id → run 键进程注册表（spawn 落定后写入）
        self._spawn_tasks: dict[
            str, set[asyncio.Task[None]]
        ] = {}  # run_id → 在途受监督 spawn 任务集
        self._timers: dict[
            str, dict[str, asyncio.TimerHandle]
        ] = {}  # run_id → 在途定时器（runtime-only）
        self._proc_seq = itertools.count(1)  # proc_id 计数器（派发时分配即返）
        self._kill_marks: set[str] = set()  # 本面组杀的 proc_id——退出通知据此带 plane_killed 标记
        self._proc_exit_sent: set[str] = (
            set()
        )  # 退出通知已发的 proc_id（组杀直发与采集任务收尾的去重）
        self._dead_runs: set[str] = set()  # 已删除 run：残余输出采集不再对已删 run_id 落库

    @staticmethod
    def _reject_reserved_overrides(spec: SessionSpec) -> None:
        """派发口保留字拒绝：reviewer 面不进 config_overrides 透传（Start/Restart 相同）。

        reviewer 配置的唯一合法通道是 reviewer_profile（_build_config 解析为
        SessionReviewerConfig）；config_overrides 里的 reviewer 键是越权直配。
        """
        reserved = _RESERVED_OVERRIDES & set(spec.config_overrides or {})
        if reserved:
            raise NotImplementedError(
                f"成员不可经 config_overrides 自配 reviewer（{sorted(reserved)}）——"
                "reviewer 配置经 reviewer_profile 注入"
            )

    @staticmethod
    def _drop_task(
        registry: dict[str, set[asyncio.Task[None]]], run_id: str, task: asyncio.Task[None]
    ) -> None:
        """任务完成回调：自剔出于在途集，集空删键（注册表不随时间增长）。"""
        tasks = registry.get(run_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                registry.pop(run_id, None)

    @staticmethod
    def _unattended_loop(loop: Any) -> Any:
        """无人值守成员的问句检测默认关。

        只对显式提供的 LoopConfig 填默认：question_detect=None → False
        （显式 True/False 尊重驱动选择）；loop=None 不凭空造——组装会
        连带改变 prompt 渲染与迭代语义，驱动自配 loop 表达意图。
        """
        if isinstance(loop, LoopConfig):
            if loop.question_detect is None:
                return loop.model_copy(update={"question_detect": False})
            return loop
        if isinstance(loop, dict) and loop.get("question_detect") is None:
            return {**loop, "question_detect": False}
        return loop

    @staticmethod
    def _reviewer_from_profile(profile: str | None) -> Any:
        """reviewer 配置解析：内联 JSON（命名字册未落地，非 JSON 显式拒绝）。"""
        if profile is None:
            return None
        try:
            raw = json.loads(profile)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            raise NotImplementedError(f"reviewer_profile 暂只支持内联 JSON 配置：{profile!r}")
        from workflow_controller.config.settings import SessionReviewerConfig

        return SessionReviewerConfig(**raw)

    @staticmethod
    def _signal_group(proc: _ProcessHandle, sig: int) -> None:
        """向进程组发信号（setsid 组 id = pid；已退出则容忍）。"""
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, sig)

    @staticmethod
    async def _drain_tasks(tasks: set[asyncio.Task[None]] | None, timeout_s: float) -> None:
        """等一批后台任务落定（有界等待，不取消）。"""
        pending = [t for t in (tasks or ()) if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout_s)

    @staticmethod
    def _extract_artifact(text: str, field_name: str | None) -> dict[str, Any]:
        """产物内容提取：整文件去空白或 JSON 标量字段；失败给判假细因。"""
        if field_name is None:
            return {"exists": True, "value": text.strip()}
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            return {"exists": True, "error": f"JSON 解析失败: {e}"}
        if not isinstance(doc, dict) or field_name not in doc:
            return {"exists": True, "error": f"缺少字段: {field_name}"}
        raw = doc[field_name]
        if raw is None:
            return {"exists": True, "error": f"字段 {field_name} 为 null"}
        if isinstance(raw, (dict, list)):
            return {"exists": True, "error": f"字段 {field_name} 非标量"}
        return {"exists": True, "value": raw if isinstance(raw, str) else json.dumps(raw)}

    def start_run_session(
        self, run_id: str, spec: SessionSpec, role: str, context: str | None
    ) -> None:
        """受监督后台任务推进，派发即返回；回执（成败都发）经通知回投。"""
        self._reject_reserved_overrides(spec)
        task = asyncio.create_task(
            self._start_run_session(run_id, spec, role, context),
            name=f"run-session:{run_id}:{spec.name}",
        )
        self._supervised.setdefault(run_id, set()).add(task)
        task.add_done_callback(functools.partial(self._drop_task, self._supervised, run_id))

    def restart_run_session(
        self,
        run_id: str,
        session_id: str,
        policy: RestartPolicy,
        spec_override: SessionSpec | None = None,
        context_override: str | None = None,
    ) -> None:
        """成员重启复合原语：受监督后台任务推进，派发即返回；回执经通知回投。"""
        if spec_override is not None:
            self._reject_reserved_overrides(spec_override)
        task = asyncio.create_task(
            self._restart_run_session(run_id, session_id, policy, spec_override, context_override),
            name=f"run-restart:{run_id}:{session_id}",
        )
        self._supervised.setdefault(run_id, set()).add(task)
        task.add_done_callback(functools.partial(self._drop_task, self._supervised, run_id))

    async def drain_session_births(self, run_id: str, timeout_s: float) -> None:
        """终态/失败收尾扫掠前置：等在途出生落定（记录行随 attach 落定）。"""
        await self._drain_tasks(self._supervised.get(run_id), timeout_s)

    async def await_sessions(
        self, run_id: str, session_ids: Sequence[str], timeout_s: float | None
    ) -> None:
        """等待 session 成员终态；超时返回前保证已停该 session。"""
        for sid in session_ids:
            try:
                done = await self._orch.wait_session_done(sid, timeout=timeout_s)
            except Exception:
                done = False
            if not done:
                with contextlib.suppress(Exception):
                    await self._orch.stop_session(sid)

    async def discard_run_session(self, run_id: str, session_id: str) -> None:
        """节点收尾后释放已结束成员的内存副本，保留持久行和归属。"""
        if not await self._run_store.session_in_run(run_id, session_id):
            return
        self._orch.discard_terminal_session(session_id)

    def start_process(
        self,
        run_id: str,
        cmd: str,
        cwd: str,
        *,
        env: Mapping[str, str] | None = None,
        context: str | None = None,
        stream: bool = True,
    ) -> str:
        """起 run 键 bash 进程组：受监督后台任务推进，返回 proc_id。

        输出经 ProcessOutput 通知流式回投、退出经 ProcessExit（带终局标记）
        回投；组杀（暂停/停止/超时清理）在退出标记上记 plane_killed。
        """
        proc_id = f"p{next(self._proc_seq)}"
        task = asyncio.create_task(
            self._spawn_process(proc_id, run_id, cmd, cwd, env, context, stream),
            name=f"run-proc:{run_id}",
        )
        self._spawn_tasks.setdefault(run_id, set()).add(task)
        task.add_done_callback(functools.partial(self._drop_task, self._spawn_tasks, run_id))
        return proc_id

    def forget_run(self, run_id: str) -> None:
        """登记已删除 run：残余输出采集不再对已删 run_id 落库。"""
        self._dead_runs.add(run_id)

    def process_infos(self, run_id: str) -> list[ProcessInfo]:
        """run 名下进程的当前句柄视图（含已退出未清理项，returncode 标识）。"""
        return [
            ProcessInfo(proc_id=proc_id, context=entry.context, returncode=entry.returncode)
            for proc_id, entry in (self._processes.get(run_id) or {}).items()
        ]

    async def drain_process_spawns(self, run_id: str, timeout_s: float) -> None:
        """排空在途受监督 spawn 任务（kill 前置：注册表项在 spawn 落定后写入）。"""
        await self._drain_tasks(self._spawn_tasks.get(run_id), timeout_s)

    async def kill_processes(self, run_id: str, context: str | None = None) -> None:
        """杀 run 键进程组（SIGTERM→宽限→SIGKILL，竞态容忍；幂等）。

        入口先排空在途受监督 spawn 任务：注册表项在 spawn 落定后才写入，
        不排空则「已派发未落定」的进程不在注册表、kill 静默漏杀成孤儿。
        受害者先记组杀标记，退出通知据此带 plane_killed（驱动区分组杀
        与外部信号死亡，不凭 rc 符号推断）。
        """
        await self.drain_process_spawns(run_id, STOP_DRAIN_TIMEOUT_S)
        entries = self._processes.get(run_id)
        if not entries:
            return
        victims = [
            (proc_id, entry)
            for proc_id, entry in list(entries.items())
            if context is None or entry.context == context
        ]
        for proc_id, entry in victims:
            # 已退出的残留条目不再打标记——其退出通知早已发出，打了无人消费
            if entry.returncode is None:
                self._kill_marks.add(proc_id)
        for _, entry in victims:
            self._signal_group(entry.proc, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(e.proc.wait() for _, e in victims), return_exceptions=True),
                timeout=KILL_GRACE_S,
            )
        for _, entry in victims:
            self._signal_group(entry.proc, signal.SIGKILL)
        receipts = await asyncio.gather(
            *(e.proc.wait() for _, e in victims), return_exceptions=True
        )
        for (proc_id, entry), rc in zip(victims, receipts, strict=True):
            # 孙进程 setsid 逃逸组杀仍握 stdout 写端时读循环永不 EOF——
            # kill 回执直接发退出通知，驱动不被 stdout 排干门控（采集任务
            # 收尾经去重幂等跳过）
            if not isinstance(rc, BaseException):
                entry.returncode = rc
                self._notify_process_exit_once(run_id, proc_id, entry.context, rc)
        for proc_id, _ in victims:
            entries.pop(proc_id, None)
        if not entries:
            self._processes.pop(run_id, None)

    def arm_timer(self, run_id: str, timer_id: str, delay_s: float) -> None:
        """挂一次性定时器（runtime-only 不持久；同 id 重挂覆盖旧句柄）。"""
        self.cancel_timer(run_id, timer_id)
        loop = asyncio.get_running_loop()
        self._timers.setdefault(run_id, {})[timer_id] = loop.call_later(
            max(0.0, delay_s),
            self._fire_timer,
            run_id,
            timer_id,
        )

    def cancel_timer(self, run_id: str, timer_id: str) -> None:
        """取消定时器（不存在或已触发 = 幂等 no-op）。"""
        handle = self._timers.get(run_id, {}).pop(timer_id, None)
        if handle is not None:
            handle.cancel()

    def cancel_run_timers(self, run_id: str) -> None:
        """清 run 名下全部在途定时器（终态收尾用）。"""
        for handle in self._timers.pop(run_id, {}).values():
            handle.cancel()

    async def read_artifact(
        self, run_id: str, path: str, field_name: str | None, context: str | None
    ) -> None:
        """读 run 工作目录产物；缺文件/缺字段/非标量折叠为判假payload回投。"""
        run = await self._run_store.get(run_id)
        base = Path(run.work_dir) if run.work_dir else self._run_root(run_id) / "work"
        target = base / path
        payload: dict[str, Any] = {}
        note = ArtifactRead(context=context, path=path, field=field_name)
        # 绝对路径或 .. 逃逸 work_dir：按缺文件判假，不读工作目录外
        if not target.resolve().is_relative_to(base.resolve()) or not target.is_file():
            self._notifier(run_id, note)
            return
        try:
            if target.stat().st_size > _ARTIFACT_MAX_BYTES:
                # 产物由 run 键进程自由写入，超限不载入（守护进程单进程承载全部 run）
                payload = {"exists": True, "error": "文件过大"}
            else:
                payload = self._extract_artifact(target.read_text(encoding="utf-8"), field_name)
        except (OSError, UnicodeDecodeError) as e:
            payload = {"exists": True, "error": f"读取失败: {e}"}
        self._notifier(run_id, replace(note, **payload))

    async def emit_run_event(
        self,
        run_id: str,
        event_type: str,
        data: Mapping[str, Any],
        *,
        context: str | None = None,
    ) -> None:
        """发射 run 级事件。"""
        await self._run_event_store().store_run_event(
            run_id,
            event_type,
            dict(data or {}),
            context=context,
        )

    async def persist_driver_state(
        self, run_id: str, blob: Mapping[str, Any], *, history: bool = True
    ) -> None:
        """驱动状态落盘（最新 + 历史）。"""
        await self._run_store.save_driver_state(run_id, dict(blob))
        if history:
            await self._run_store.append_driver_snapshot(run_id, dict(blob))

    async def _start_run_session(
        self, run_id: str, spec: SessionSpec, role: str, context: str | None
    ) -> None:
        """出生任务体：SO 激活成员；失败发失败回执，成功幂等回退挂靠并补记 context。"""
        birth_kwargs: dict[str, Any] = {}
        if spec.plugin_metadata:
            # 溯源元数据随出生参数入 pool（事后直改的替代通道）
            birth_kwargs["plugin_metadata"] = dict(spec.plugin_metadata)
        try:
            session = await self._orch.start_session_with_config(
                self._build_config(run_id, spec, role),
                **birth_kwargs,
            )
        except Exception as e:
            self._notifier(
                run_id,
                SessionActivated(
                    session_id=spec.name,
                    context=context,
                    ok=False,
                    error=f"{type(e).__name__}: {e}",
                ),
            )
            return
        # 激活钩子路径已落归属行；此处幂等回退并补记 context（成员标签）
        with contextlib.suppress(Exception):
            await self._affiliation.attach(session.id, run_id, role=role, context=context)
        with contextlib.suppress(Exception):
            # 走归属服务（行 + 缓存同写）：事件打戳与结局回执都从缓存取
            # context，只写行会让驱动侧按旧 context 对不上账
            await self._affiliation.set_context(session.id, context)
        self._notifier(
            run_id,
            SessionActivated(session_id=session.id, context=context, ok=True),
        )

    async def _restart_run_session(
        self,
        run_id: str,
        session_id: str,
        policy: RestartPolicy,
        spec_override: SessionSpec | None,
        context_override: str | None,
    ) -> None:
        """重启任务体：按策略复用续跑、停旧起新或超时续跑；成败都回执。

        池缺席（成员 session 被直接删除）经 SO 原语 raise ValueError，
        由下方统一异常回执收尾，不空转假 ok。
        """
        try:
            if (
                policy is RestartPolicy.RESUME_IF_PAUSED
                and self._orch.get_session_facts(session_id) is None
            ):
                # 池缺席分支的恢复读走线程池：同步 SQLite 读不上事件循环
                await asyncio.to_thread(
                    self._orch.restore_session_from_store,
                    session_id,
                )
                if not self._orch.is_resumable(session_id):
                    raise ValueError(f"session {session_id} 无可续的持久记录")
            await self._apply_restart_policy(session_id, policy, spec_override)
            if context_override is not None:
                # 同 Start 路口径：行 + 缓存同写（RestartSession 的换代 context）
                await self._affiliation.set_context(session_id, context_override)
        except Exception as e:
            self._notifier(
                run_id,
                SessionActivated(
                    session_id=session_id,
                    context=context_override,
                    ok=False,
                    error=f"{type(e).__name__}: {e}",
                ),
            )
            return
        self._notifier(
            run_id,
            SessionActivated(session_id=session_id, context=context_override, ok=True),
        )

    async def _apply_restart_policy(
        self,
        session_id: str,
        policy: RestartPolicy,
        spec_override: SessionSpec | None,
    ) -> None:
        """按重启策略操作成员：复用续跑 / 停旧起新 / 超时续跑。"""
        if policy is RestartPolicy.CONTINUE:
            prompt = spec_override.prompt if spec_override is not None else None
            await self._orch.continue_session(session_id, prompt)
        elif policy is RestartPolicy.RESUME_IF_PAUSED and self._orch.is_resumable(session_id):
            if spec_override is not None:
                model_fields = {"model", "provider_index", "model_pinned"}
                model_overrides = {
                    k: v for k, v in spec_override.refresh_overrides().items() if k in model_fields
                }
                if model_overrides:
                    await self._orch.apply_session_overrides(session_id, model_overrides)
            await self._orch.resume_session(session_id)
        else:
            if spec_override is not None:
                # 字段派生归 SessionSpec；宿主注入键剔除是挂靠缝保护
                # （session.config.run_id 越权改写会污染归属判定），沿用 Start 侧注入值；
                # loop payload 同样注入无人值守默认
                await self._orch.apply_session_overrides(
                    session_id,
                    {
                        k: RunHost._unattended_loop(v) if k == "loop" else v
                        for k, v in spec_override.refresh_overrides().items()
                        if k not in _HOST_INJECTED_CONFIG
                    },
                )
            await self._orch.restart_session(session_id)

    def _build_config(self, run_id: str, spec: SessionSpec, role: str) -> SessionConfig:
        """成员配置组装策略：spec 派生基座 + 宿主注入（成员不自配归属）。

        字段映射归 SessionSpec.assembly_fields（信息专家）；宿主只持注入与
        保护——reviewer 经 profile 解析、run_id/role 后置回填
        （config_overrides 越权覆盖无效；reviewer 直配在派发口显式拒绝）、
        无人值守成员的问句检测默认关。
        """
        fields: dict[str, Any] = spec.assembly_fields()
        if spec.model and "model_pinned" not in fields:
            rotator = getattr(self._orch, "auth_rotator", None)
            if rotator is not None:
                fields["model_pinned"] = spec.model != (
                    rotator.get_provider(spec.provider_index).model or ""
                )
        fields["loop"] = RunHost._unattended_loop(fields["loop"])
        fields.update(
            reviewer=RunHost._reviewer_from_profile(spec.reviewer_profile),
            run_id=run_id,
            role=role,
        )
        return SessionConfig(**fields)

    async def _spawn_process(
        self,
        proc_id: str,
        run_id: str,
        cmd: str,
        cwd: str,
        env: Mapping[str, str] | None,
        context: str | None,
        stream: bool,
    ) -> None:
        """受监督 spawn 任务体：exec 落定即入注册表，输出采集转独立任务。"""
        capture = await self._automation_capture(run_id, cmd, cwd, context)
        try:
            if cwd:
                # run 工作目录是 agent 产物的家：首次使用即建（含 parents），
                # bash 与后续成员不因目录缺席而失败
                Path(cwd).mkdir(parents=True, exist_ok=True)
            merged_env = dict(os.environ)
            merged_env.update(env or {})
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                cmd,
                cwd=cwd or None,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            await self._handle_spawn_failure(run_id, proc_id, context, capture, e)
            return
        entry = _ProcEntry(proc=proc, context=context)
        self._processes.setdefault(run_id, {})[proc_id] = entry
        entry.output_task = asyncio.create_task(
            self._collect_output(run_id, proc_id, entry, stream, capture),
            name=f"run-proc-out:{run_id}:{proc_id}",
        )

    async def _handle_spawn_failure(
        self,
        run_id: str,
        proc_id: str,
        context: str | None,
        capture: Any,
        error: Exception,
    ) -> None:
        """spawn 失败收尾：自动化回执（incomplete）+ 退出事件 + ProcessExit 回执。"""
        logger.error("run %s 进程 %s 起动失败：%s", run_id, proc_id, error)
        await self._automation_outcome(capture, False, None, incomplete=True)
        stats = _StreamStats()
        await self._write_process_exit(
            run_id,
            proc_id,
            context,
            returncode=-1,
            stats=stats,
            error=f"{type(error).__name__}: {error}",
        )
        self._notifier(
            run_id,
            ProcessExit(
                proc_id=proc_id,
                context=context,
                returncode=-1,
                exit_mark="spawn_failed",
            ),
        )

    async def _collect_output(
        self,
        run_id: str,
        proc_id: str,
        entry: _ProcEntry,
        stream: bool,
        capture: Any = None,
    ) -> None:
        """输出采集任务：按读块聚合落库（行级限流/尾部聚合），进程退出即回执。"""
        stats = await self._drain_proc_output(run_id, proc_id, entry, stream)
        await self._finalize_proc_exit(run_id, proc_id, entry, capture, stats)

    async def _drain_proc_output(
        self, run_id: str, proc_id: str, entry: _ProcEntry, stream: bool
    ) -> _StreamStats:
        """读尽 stdout 并按行落库；返回流式统计（异常只记日志不中断采集）。"""
        stats = _StreamStats()
        buf = b""
        overlong = False
        reader = entry.proc.stdout
        try:
            if reader is not None:
                buf, overlong = await self._pump_proc_reader(
                    reader, run_id, proc_id, entry.context, stream, stats, buf, overlong
                )
            if buf:
                await self._ingest_lines(run_id, proc_id, entry.context, stream, stats, [buf])
        except Exception:
            logger.exception("run %s 进程 %s 输出采集异常", run_id, proc_id)
        return stats

    async def _pump_proc_reader(
        self,
        reader: asyncio.StreamReader,
        run_id: str,
        proc_id: str,
        context: str | None,
        stream: bool,
        stats: _StreamStats,
        buf: bytes,
        overlong: bool,
    ) -> tuple[bytes, bool]:
        """按读块读尽输出流并逐块落库；返回循环后的 (缓冲, 超长丢弃中)。"""
        while True:
            chunk = await reader.read(_PROC_READ_CHUNK)
            if not chunk:
                break
            buf, overlong = await self._ingest_proc_chunk(
                run_id, proc_id, context, stream, stats, chunk, buf, overlong
            )
        return buf, overlong

    async def _ingest_proc_chunk(
        self,
        run_id: str,
        proc_id: str,
        context: str | None,
        stream: bool,
        stats: _StreamStats,
        chunk: bytes,
        buf: bytes,
        overlong: bool,
    ) -> tuple[bytes, bool]:
        """单块并入行缓冲：完整行落库；超长行留前缀、余部丢弃至行尾。"""
        if overlong:
            nl = chunk.find(b"\n")
            if nl < 0:
                return buf, True
            await self._ingest_lines(run_id, proc_id, context, stream, stats, [buf])
            buf = b""
            chunk = chunk[slice(nl + 1, None)]
        buf += chunk
        if b"\n" in buf:
            *lines, buf = buf.split(b"\n")
            await self._ingest_lines(run_id, proc_id, context, stream, stats, lines)
        if len(buf) > _PROC_LINE_MAX_LEN:
            # 无换行超长行：留前缀、余部丢弃至行尾，累积缓冲有界
            return buf[:_PROC_LINE_MAX_LEN], True
        return buf, False

    async def _finalize_proc_exit(
        self,
        run_id: str,
        proc_id: str,
        entry: _ProcEntry,
        capture: Any,
        stats: _StreamStats,
    ) -> None:
        """进程退出结算：自动化回执 + 退出事件落库 + ProcessExit 回执。"""
        returncode = await entry.proc.wait()
        entry.returncode = returncode
        await self._automation_outcome(
            capture,
            returncode == 0,
            returncode,
            incomplete=returncode < 0,
        )
        await self._write_process_exit(run_id, proc_id, entry.context, returncode, stats=stats)
        self._notify_process_exit_once(run_id, proc_id, entry.context, returncode)

    def _notify_process_exit_once(
        self,
        run_id: str,
        proc_id: str,
        context: str | None,
        returncode: int,
    ) -> None:
        """进程退出通知单点（幂等）：组杀直发与采集任务收尾共用，只发一次。"""
        if proc_id in self._proc_exit_sent:
            return
        self._proc_exit_sent.add(proc_id)
        exit_mark = "plane_killed" if proc_id in self._kill_marks else None
        self._kill_marks.discard(proc_id)
        self._notifier(
            run_id,
            ProcessExit(
                proc_id=proc_id,
                context=context,
                returncode=returncode,
                exit_mark=exit_mark,
            ),
        )

    async def _ingest_lines(
        self,
        run_id: str,
        proc_id: str,
        context: str | None,
        stream: bool,
        stats: _StreamStats,
        raw_lines: Sequence[bytes],
    ) -> None:
        """一批输出行的记账与落库：限流截断、尾部聚合、单事务批量写。

        已删除 run 的残余输出不落库——无记录可落，防孤儿行复活。
        """
        if run_id in self._dead_runs:
            for raw in raw_lines:
                stats.total += 1
                stats.tail.append(raw.decode("utf-8", errors="replace")[:_PROC_LINE_MAX_LEN])
            return
        events: list[tuple[str, dict[str, Any]]] = []
        for raw in raw_lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")[:_PROC_LINE_MAX_LEN]
            stats.total += 1
            stats.tail.append(line)
            if stream:
                self._notifier(run_id, ProcessOutput(proc_id=proc_id, context=context, line=line))
                if stats.written < _PROC_LINE_BUDGET:
                    stats.written += 1
                    events.append(("process_output", {"proc_id": proc_id, "line": line}))
                else:
                    stats.dropped += 1
        if events:
            with contextlib.suppress(Exception):
                await self._run_event_store().store_run_events_batch(
                    run_id,
                    events,
                    context=context,
                )

    async def _write_process_exit(
        self,
        run_id: str,
        proc_id: str,
        context: str | None,
        returncode: int,
        *,
        stats: _StreamStats,
        error: str | None = None,
    ) -> None:
        """退出事件落库（尾部聚合随行，stream=false 的唯一输出通道）。"""
        if run_id in self._dead_runs:
            return
        data: dict[str, Any] = {
            "proc_id": proc_id,
            "returncode": returncode,
            "lines": stats.total,
            "dropped": stats.dropped,
            "tail": list(stats.tail),
        }
        if error is not None:
            data["error"] = error
        with contextlib.suppress(Exception):
            await self._run_event_store().store_run_event(
                run_id, "process_exit", data, context=context
            )

    async def _automation_capture(
        self,
        run_id: str,
        cmd: str,
        cwd: str,
        context: str | None,
    ) -> Any:
        """自动化进程的轨迹捕获上下文（无订阅者时 None，捕获失败不阻断执行）。"""
        file_changes = getattr(self._orch, "file_changes", None)
        if file_changes is None or not file_changes.has_subscribers:
            return None
        from datetime import UTC, datetime
        from uuid import uuid4

        from workflow_controller.plugin.file_changes import (
            FileChangeContext,
            tool_will_execute_event,
        )

        capture = FileChangeContext(
            capture_id=uuid4().hex,
            actor_kind="automation",
            workflow_controller_session_id=None,
            native_session_id=None,
            parent_native_session_id=None,
            runtime="run",
            model=None,
            agent_name=None,
            tool_name="Bash",
            tool_use_id=uuid4().hex,
            cwd=Path(cwd or ".").resolve(),
            tool_input={"command": cmd},
            started_at=datetime.now(tz=UTC),
            attributes={},
            run_id=run_id,
            run_context=context,
        )
        try:
            await self._bus.emit(tool_will_execute_event(capture))
        except Exception:
            logger.exception("run 进程轨迹 pre capture failed open")
            return None
        return capture

    async def _automation_outcome(
        self,
        capture: Any,
        succeeded: bool,
        returncode: int | None,
        *,
        incomplete: bool = False,
    ) -> None:
        """自动化进程的轨迹结局事件（失败开环：不阻断进程面）。"""
        if capture is None:
            return
        from datetime import UTC, datetime

        from workflow_controller.plugin.file_changes import (
            FileChangeOutcome,
            tool_execution_outcome_event,
        )

        try:
            outcome = FileChangeOutcome(
                succeeded=succeeded,
                is_error=not succeeded,
                tool_output={"returncode": returncode},
                path_hints=(),
                finished_at=datetime.now(tz=UTC),
                incomplete=incomplete,
            )
            await self._bus.emit(tool_execution_outcome_event(capture, outcome))
        except Exception:
            logger.exception("run 进程轨迹 post capture failed open")

    def _run_event_store(self) -> Any:
        """run 键事件的落库门面（单库直写）。"""
        return self._orch.event_router.default_store

    def _fire_timer(self, run_id: str, timer_id: str) -> None:
        """到期回调：出表后经通知 FIFO 回投（事件循环内同步安全）。"""
        self._timers.get(run_id, {}).pop(timer_id, None)
        # 经模块限定引用，避免与 graph engine 的同名动作导入形成整段重复
        self._notifier(run_id, run_driver.TimerFired(timer_id=timer_id))
