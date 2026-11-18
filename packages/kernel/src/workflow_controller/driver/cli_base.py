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

"""CLI 子进程驱动共用底座：统一 query 入口、进程生命周期与 stdout/stderr 泵。

四个运行时驱动（claude_cli / claude_sdk / codex_cli / opencode）的 ``query``
关键字参数面完全一致，签名只在 ``Driver`` 协议与本底类各存一份；子类实现
``_query_events(prompt, opts)``，经 ``QueryOptions`` 解包拿到各参数。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import weakref
from typing import TYPE_CHECKING, NamedTuple

from workflow_controller.driver.protocol import Driver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from workflow_controller.driver.hooks import DriverHooks
    from workflow_controller.session.events import Event

logger = logging.getLogger(__name__)

_STDOUT_CHUNK_SIZE = 65536


class QueryOptions(NamedTuple):
    """``Driver.query`` 的关键字参数包（基类打包，子类 `_query_events` 解包）。"""

    iteration: int = 1
    session_id: str = ""
    model: str = ""
    cwd: str | None = None
    max_turns: int | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    permission_mode: str = ""
    env: dict[str, str] | None = None
    hooks: DriverHooks | None = None
    system_prompt: str = ""
    agent_name: str = ""


def kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """击杀子进程及其整个进程组（幂等，可重复调用；三个 CLI 驱动共用）。"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def error_end_events(
    error: str,
    *,
    session_id: str,
    iteration: int,
    end_reason: str,
) -> list[Event]:
    """ERROR（可观测错误）+ ITERATION_END（query 边界收尾）事件对（驱动共用）。"""
    from workflow_controller.session.events import Event, EventType

    return [
        Event.create(
            session_id=session_id,
            iteration=iteration,
            event_type=EventType.ERROR,
            data={"error": error},
        ),
        Event.create(
            session_id=session_id,
            iteration=iteration,
            event_type=EventType.ITERATION_END,
            data={"reason": end_reason},
        ),
    ]


def spawn_failure_events(
    exc: OSError,
    *,
    args: list[str],
    default_command: str,
    command_prefix: tuple[str, ...] | list[str],
    log_label: str,
    session_id: str,
    iteration: int,
    end_reason: str,
) -> list[Event]:
    """子进程 spawn 失败的统一善后：ERROR + ITERATION_END 事件对（各 CLI 驱动共用）。"""
    command = args[0] if args else default_command
    logger.error("Failed to spawn %s: %s (command=%s)", log_label, exc, command)
    error = f"failed to execute {command}: {exc}"
    if isinstance(exc, FileNotFoundError):
        error = f"command not found: {command}"
        if command in command_prefix:
            error += f"; install {command} and add it to PATH, or disable the {command} wrapper"
    return error_end_events(
        error, session_id=session_id, iteration=iteration, end_reason=end_reason
    )


async def iter_proc_line_batches(proc: asyncio.subprocess.Process) -> AsyncIterator[list[str]]:
    """按已读 chunk 批量切行产出（解码去空白，跳过空行；EOF 尾行单独一批）。

    不用 StreamReader.readline：其 64KB 行上限会被大工具结果的单行 JSON
    击穿（LimitOverrunError）。批间是消费方检查 stop 的位置——已读入
    chunk 的行完整产出，stop 置位不丢已缓冲的行。
    """
    if proc.stdout is None:
        raise RuntimeError("CLI 子进程 stdout 未创建")
    buf = b""
    while True:
        try:
            chunk = await proc.stdout.read(_STDOUT_CHUNK_SIZE)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        lines: list[str] = []
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
        if lines:
            yield lines
    if buf.strip():
        yield [buf.decode("utf-8", errors="replace").strip()]


async def drain_proc_stderr(
    proc: asyncio.subprocess.Process,
    handle_line: Callable[[str], None],
) -> None:
    """泵干 CLI 子进程 stderr：非空行交 handle_line（日志级别与 tail 收集由调用侧决定）。"""
    try:
        if proc.stderr is None:
            raise RuntimeError("CLI 子进程 stderr 未创建")
        async for line_bytes in proc.stderr:
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                handle_line(line)
    except Exception:
        logger.debug("stderr drain ended", exc_info=True)


class CLIDriverBase(Driver):
    """CLI 子进程驱动的公共底座（设计背景见模块 docstring）。"""

    def __init__(self, *, permission_mode: str = "bypassPermissions") -> None:
        super().__init__(permission_mode=permission_mode)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._finalizer: weakref.finalize[..., CLIDriverBase] | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def query(
        self,
        prompt: str,
        *,
        agent_name: str = "",
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
        disallowed_tools: list[str] | None = None,
        env: dict[str, str] | None = None,
        hooks: DriverHooks | None = None,
        iteration: int = 1,
        max_turns: int | None = None,
        model: str = "",
        permission_mode: str = "",
        session_id: str = "",
        system_prompt: str = "",
    ) -> AsyncIterator[Event]:
        """统一 query 入口：打包 ``QueryOptions`` 交给子类 ``_query_events``。"""
        self._stop_event.clear()
        opts = QueryOptions(
            agent_name=agent_name,
            allowed_tools=allowed_tools,
            cwd=cwd,
            disallowed_tools=disallowed_tools,
            env=env,
            hooks=hooks,
            iteration=iteration,
            max_turns=max_turns,
            model=model,
            permission_mode=permission_mode,
            session_id=session_id,
            system_prompt=system_prompt,
        )
        return self._query_events(prompt, opts)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            kill_proc_group(self._proc)

    async def kill(self) -> None:
        if self._proc is not None:
            kill_proc_group(self._proc)

    def _arm_cleanup(self, proc: asyncio.subprocess.Process) -> None:
        """Kill this subprocess tree if the driver is GC'd or the interpreter
        exits, per-instance (no shared module state). weakref.finalize also
        fires on interpreter shutdown, replacing the old atexit handler.
        """
        self._finalizer = weakref.finalize(self, kill_proc_group, proc)

    def _disarm_cleanup(self) -> None:
        """Cancel the finalizer after a clean teardown (proc already killed)."""
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None

    def _query_events(self, prompt: str, opts: QueryOptions) -> AsyncIterator[Event]:
        """子类实现：执行一次查询并产出事件流。"""
        raise NotImplementedError

    async def _spawn_proc(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        cwd: str | None = None,
    ) -> asyncio.subprocess.Process:
        """起 CLI 子进程：独立进程组（setsid），stdin/stdout/stderr 三路管道。"""
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            preexec_fn=os.setsid,
        )
