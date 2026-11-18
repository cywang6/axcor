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

"""Runtime 无关的 per-query MCP 提问门禁。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

import uvicorn

try:  # pragma: no cover
    from mcp.server.mcpserver.context import Context  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import Context  # noqa: TC002

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.applications import Starlette

    from workflow_controller.driver.hooks import DriverHooks

logger = logging.getLogger(__name__)

GATE_MCP_NAME = "workflow_controller_gate"
GATE_TOOL_NAME = "workflow_controller_ask"
PROGRESS_INTERVAL_SECONDS = 10.0

_TOOL_DESCRIPTION = (
    "Ask the Workflow Controller controller a question when you need a decision or "
    "clarification before proceeding. Pass one or more questions, each with "
    "a header, the question text, and a list of options (label + description). "
    "The controller (an automated reviewer, or a human) will answer; use the "
    "answer to continue."
)


async def answer_via_gate(
    hooks: DriverHooks | None,
    session_id: str,
    tool_input: dict[str, Any],
) -> str:
    """把 MCP 提问转交控制端并等待答案。"""
    if hooks is None or hooks.on_gate_question is None:
        logger.warning("gate question callback is missing")
        return ""
    return await hooks.on_gate_question(session_id, tool_input)


async def answer_via_gate_with_keepalive(
    hooks: DriverHooks,
    session_id: str,
    tool_input: dict[str, Any],
    ctx: Any,
    interval: float = PROGRESS_INTERVAL_SECONDS,
) -> str:
    """等待答案期间定期发送 MCP progress。"""

    async def send_progress() -> None:
        """持续报告长等待进度。"""
        progress = 0.0
        while True:
            await asyncio.sleep(interval)
            progress += 1
            await ctx.report_progress(progress, message="waiting for response")

    progress_task = asyncio.create_task(
        send_progress(),
        name="gate-progress-keepalive",
    )
    try:
        return await answer_via_gate(hooks, session_id, tool_input)
    finally:
        progress_task.cancel()
        with contextlib.suppress(BaseException):
            await progress_task


class _GateServer(uvicorn.Server):
    """不抢占宿主进程信号的 uvicorn 服务。"""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """保持宿主信号处理器不变。"""
        yield


class PerQueryGate:
    """绑定单次调用的进程内 MCP 门禁。"""

    def __init__(
        self,
        *,
        hooks: DriverHooks,
        session_id: str,
        require_callback: bool = False,
    ) -> None:
        """校验回调并保存单次调用上下文。"""
        if require_callback and hooks.on_gate_question is None:
            raise ValueError("PerQueryGate requires on_gate_question")
        self._hooks = hooks
        self._session_id = session_id
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port = 0
        self._app: Starlette | None = None

    @property
    def url(self) -> str:
        """返回本次调用的 MCP HTTP 地址。"""
        return f"http://127.0.0.1:{self._port}/mcp"

    async def start(self) -> None:
        """启动随机端口的 MCP 服务并等待可连接。"""
        self._app = self._build_app()
        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            log_level=None,
            timeout_graceful_shutdown=1,
        )
        server = _GateServer(config)
        self._server = server

        async def serve_gate() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise RuntimeError("gate server exited unexpectedly") from exc

        self._task = asyncio.create_task(serve_gate(), name="workflow-controller-gate")
        for _ in range(50):
            if server.started:
                break
            if self._task is not None and self._task.done():
                break
            await asyncio.sleep(0.1)
        if not server.started or not server.servers:
            await self.stop()
            raise RuntimeError("gate server failed to start")
        self._port = server.servers[0].sockets[0].getsockname()[1]
        logger.info(
            "runtime gate up for session=%s at %s",
            self._session_id[:12],
            self.url,
        )

    async def stop(self) -> None:
        """有界停止服务并清理后台任务。"""
        server, task = self._server, self._task
        self._server, self._task = None, None
        if server is not None:
            server.should_exit = True
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                logger.warning(
                    "runtime gate for session=%s ignored cancellation",
                    self._session_id[:12],
                )
        except Exception as exc:
            logger.warning(
                "runtime gate for session=%s exited: %s",
                self._session_id[:12],
                exc,
            )

    def _build_app(self) -> Starlette:
        """构建带提问闭包的 MCP 应用。"""
        try:  # pragma: no cover
            from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover
            from mcp.server.fastmcp import FastMCP

        hooks, session_id = self._hooks, self._session_id
        mcp = FastMCP(GATE_MCP_NAME)

        @mcp.tool(name=GATE_TOOL_NAME, description=_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
        async def workflow_controller_ask(
            questions: list[dict[str, Any]],
            ctx: Context,
        ) -> str:
            """等待控制端回答当前提问。"""
            return await answer_via_gate_with_keepalive(
                hooks,
                session_id,
                {"questions": questions},
                ctx,
            )

        return cast("Starlette", mcp.streamable_http_app())
