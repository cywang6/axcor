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

"""Stage 3b: opencode MCP-gate — AskUserQuestion-equivalent for opencode.

Two test surfaces:
- ``Orchestrator._resolve_gate_question`` — the on_gate_question callback
  (auto = reviewer decide_auq; manual = emit + await human). Mirrors the cc
  ``_decide_pre_tool_use`` AUQ logic but returns the ANSWER string (the gate
  returns it as the MCP tool result), not a CC deny blob.
- ``workflow_controller.driver.opencode_gate`` — build_config_content (the OPENCODE_CONFIG_-
  CONTENT JSON), answer_via_gate (the tool handler), and PerQueryGate lifecycle
  (start an in-process MCP server, a real MCP client lists+calls the tool,
  stop). Session-scoped per query → zero routing.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest

from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.driver.hooks import DriverHooks
from workflow_controller.driver.opencode_gate import (
    GATE_MCP_NAME,
    GATE_TOOL_NAME,
    PerQueryGate,
    answer_via_gate,
    answer_via_gate_with_keepalive,
    build_config_content,
)

from ._hook_handler_stubs import hook_orch_test_config

if TYPE_CHECKING:
    from pathlib import Path

    from workflow_controller.config.settings import AppConfig


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    return hook_orch_test_config(tmp_path)


@pytest.fixture
async def orch(base_config: AppConfig, tmp_path: Path):
    orchestrator = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    await orchestrator.start()
    yield orchestrator
    await orchestrator.shutdown()


def _auq_input(questions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "questions": questions
        or [
            {
                "question": "Continue?",
                "header": "h",
                "options": [{"label": "Yes"}, {"label": "No"}],
            },
        ]
    }


# ── opencode_gate pure helpers ────────────────────────────────────────────


class TestBuildConfigContent:
    @staticmethod
    def test_shape() -> None:
        url = "http://127.0.0.1:8000/mcp"
        cfg = json.loads(build_config_content(url))
        entry = cfg["mcp"][GATE_MCP_NAME]
        assert entry["type"] == "remote"
        assert entry["url"] == url
        assert entry["enabled"] is True
        assert entry["oauth"] is False
        # 无显式策略时保留 gate 的既有默认值。
        assert cfg["compaction"] == {"auto": False}

    @staticmethod
    def test_auto_compaction_when_no_plugin_manages_context() -> None:
        cfg = json.loads(
            build_config_content(
                auto_compaction=True,
                base_content='{"compaction":{"prune":true}}',
            )
        )
        assert cfg["compaction"] == {"prune": True, "auto": True}


class TestAnswerViaGate:
    async def test_delegates_to_hook(self) -> None:
        async def cb(sid: str, data: dict[str, Any]) -> str:
            return f"ans:{sid}:{data['questions'][0]['header']}"

        hooks = DriverHooks(session_id="s1", on_gate_question=cb)
        out = await answer_via_gate(hooks, "s1", _auq_input())
        assert out == "ans:s1:h"

    async def test_no_hook_returns_safe_default(self) -> None:
        out = await answer_via_gate(None, "s1", _auq_input())
        assert isinstance(out, str)


class _FakeCtx:
    """Stand-in for mcp's Context: records report_progress calls."""

    def __init__(self) -> None:
        self.progress: list = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, message))


class TestGateProgressKeepalive:
    """opencode hardcodes a ~30-60s tool-call timeout; reporting progress keeps
    it alive (per opencode changelog). The gate must ping progress while blocked
    on a slow answer (manual mode = waiting for a human).
    """

    async def test_progress_sent_periodically_while_waiting(self) -> None:
        async def slow_cb(sid: str, data: dict[str, Any]) -> str:
            await asyncio.sleep(0.25)  # long relative to interval
            return "resolved"

        hooks = DriverHooks(session_id="s1", on_gate_question=slow_cb)
        ctx = _FakeCtx()
        out = await answer_via_gate_with_keepalive(hooks, "s1", _auq_input(), ctx, interval=0.05)
        assert out == "resolved"
        assert len(ctx.progress) >= 2, "progress must fire during the wait"
        assert ctx.progress[0][1] == "waiting for response"

    async def test_no_progress_when_answer_is_fast(self) -> None:
        async def fast_cb(sid: str, data: dict[str, Any]) -> str:
            return "quick"

        hooks = DriverHooks(session_id="s1", on_gate_question=fast_cb)
        ctx = _FakeCtx()
        out = await answer_via_gate_with_keepalive(hooks, "s1", _auq_input(), ctx, interval=0.05)
        assert out == "quick"
        assert ctx.progress == [], "no progress before completion"

    async def test_keepalive_stops_after_answer(self) -> None:
        async def cb(sid: str, data: dict[str, Any]) -> str:
            await asyncio.sleep(0.1)
            return "done"

        hooks = DriverHooks(session_id="s1", on_gate_question=cb)
        ctx = _FakeCtx()
        await answer_via_gate_with_keepalive(hooks, "s1", _auq_input(), ctx, interval=0.03)
        n_after = len(ctx.progress)
        await asyncio.sleep(0.1)
        assert len(ctx.progress) == n_after, "keepalive task must be cancelled"


# ── PerQueryGate lifecycle (real MCP client ↔ in-process server) ──────────


class TestPerQueryGateEndToEnd:
    async def test_client_lists_and_calls_tool(self) -> None:
        async def cb(sid: str, data: dict[str, Any]) -> str:
            return f"resolved:{sid}"

        hooks = DriverHooks(session_id="ses-xyz", on_gate_question=cb)
        gate = PerQueryGate(hooks=hooks, session_id="ses-xyz")
        await gate.start()
        try:
            async with _mcp_client(gate.url) as session:
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert GATE_TOOL_NAME in names
                result = await session.call_tool(GATE_TOOL_NAME, _auq_input())
                text = "".join(b.text for b in result.content if hasattr(b, "text"))
                assert "resolved:ses-xyz" in text
        finally:
            await gate.stop()

    async def test_url_is_localhost_and_mounted_at_mcp(self) -> None:
        gate = PerQueryGate(hooks=DriverHooks(session_id="s"), session_id="s")
        await gate.start()
        try:
            assert gate.url.startswith("http://127.0.0.1:")
            assert gate.url.rstrip("/").endswith("/mcp")
        finally:
            await gate.stop()

    async def test_uvicorn_startup_error_is_fail_closed(self, monkeypatch) -> None:
        from workflow_controller.driver import runtime_gate

        async def fail_startup(self, sockets=None) -> None:
            raise RuntimeError("startup failed")

        monkeypatch.setattr(getattr(runtime_gate, "_GateServer"), "startup", fail_startup)
        gate = PerQueryGate(hooks=DriverHooks(session_id="s"), session_id="s")

        with pytest.raises(RuntimeError, match="gate server failed to start"):
            await gate.start()

    async def test_stop_timeout_cancels_and_waits_again(self, monkeypatch) -> None:
        from workflow_controller.driver import runtime_gate

        gate = runtime_gate.PerQueryGate(hooks=DriverHooks(session_id="s"), session_id="s")
        timeouts: list[float] = []

        async def fake_wait_for(awaitable: Any, timeout: float) -> None:
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise TimeoutError
            await awaitable

        async def never_exit() -> None:
            await asyncio.Event().wait()

        class FakeServer:
            should_exit = False

        server = FakeServer()
        task = asyncio.create_task(never_exit())
        setattr(gate, "_server", server)
        setattr(gate, "_task", task)
        monkeypatch.setattr(runtime_gate.asyncio, "wait_for", fake_wait_for)

        await gate.stop()

        assert server.should_exit
        assert timeouts == [3.0, 2.0]
        assert task.cancelled()


@asynccontextmanager
async def _mcp_client(url: str):  # type: ignore[no-untyped-def]
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # streamable_http_client 的上下文返回值随 mcp 版本为 2 元或 3 元组，按位取读写流
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


class TestGateSignalHygiene:
    """嵌入式 gate 不得触碰进程级信号处理器与全局 uvicorn 日志态。

    回归背景：uvicorn ``capture_signals`` 的保存/恢复在多 server 先后启停时
    交错，会把已死 gate 的 SIGTERM 处理器装回进程——宿主 web 服务器从此
    收不到关停信号（守护进程 SIGTERM 后无限存活）。
    """

    async def test_lifecycle_preserves_signal_handlers(self) -> None:
        import signal

        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        hooks = DriverHooks(session_id="s")
        gates = [PerQueryGate(hooks=hooks, session_id=f"s{i}") for i in range(2)]
        for g in gates:
            await g.start()
        for g in gates:
            await g.stop()
        assert signal.getsignal(signal.SIGTERM) is before_term
        assert signal.getsignal(signal.SIGINT) is before_int

    async def test_start_keeps_uvicorn_access_log_level(self) -> None:
        import logging

        access = logging.getLogger("uvicorn.access")
        access.setLevel(logging.INFO)
        gate = PerQueryGate(hooks=DriverHooks(session_id="s"), session_id="s")
        await gate.start()
        try:
            assert access.level == logging.INFO
        finally:
            await gate.stop()
        access.setLevel(logging.NOTSET)
