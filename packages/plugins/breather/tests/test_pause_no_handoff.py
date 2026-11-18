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

"""pause 不触发 handoff 的回归测试。

场景：用户 resume 一个 session 后再 pause，不应该触发 handoff。
pause 是硬杀 driver 进程，不走 stop hook 路径，因此 breather 的
on_stop_hook 和 on_iteration_boundary 都不应该被调用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from workflow_controller.config.settings import LoopConfig, SessionConfig
from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugins.breather.plugin import BreatherPlugin
from workflow_controller.session.session import Session, SessionState


def _cfg(threshold=2.0):
    return PluginConfig(
        name="breather",
        version="0.1",
        enabled=True,
        extra={
            "context_threshold_pct": threshold,
            "context_window": 1000000,
            "handoff_agent_timeout": 10,
        },
    )


def _worker(sid="w1"):
    cfg = SessionConfig(
        name="worker-1",
        role="worker",
        cwd="/tmp",
        initial_prompt="test",
        runtime="opencode",
        loop=LoopConfig(max_iterations=10),
    )
    s = Session(name="worker-1", role="worker", config=cfg)
    s.id = sid
    s.state = SessionState.RUNNING
    return s


def _make_orch():
    orch = MagicMock()
    orch.pool = MagicMock()
    setattr(orch.pool, "_persist_session", AsyncMock())
    orch.event_router.get_events_sync.return_value = []
    orch.event_router.store = AsyncMock()
    getattr(orch, "_subagents").in_flight.return_value = {}
    orch.config = MagicMock()
    return orch


def _ctx(orch, hook_server=None):
    """Build a PluginContext for install（端口闭包经生产装配函数接到 mock 上）."""
    from ._helpers import plugin_context

    orch.bus = MagicMock()
    orch.bus.observe = MagicMock()
    return plugin_context(orch, hook_server)


def test_pause_does_not_set_handoff_pending():
    """pause session 后，breather 的 _handoff_pending 保持 False。"""
    worker = _worker()
    orch = _make_orch()
    orch.pool.get.return_value = worker
    orch.pool.list_sessions.return_value = [worker]

    plugin = BreatherPlugin(_cfg(), Path("."))
    plugin.install(_ctx(orch))

    assert not getattr(plugin, "_handoff_pending")

    asyncio.run(worker.pause())

    assert worker.state == SessionState.IDLE
    assert worker.stop_reason == "user_pause"
    assert not getattr(plugin, "_handoff_pending")
    assert not getattr(plugin, "_handoff_finalizing")
    assert not getattr(plugin, "_handoff_stop_armed")
    assert not getattr(plugin, "_handoff_force_pending")


def test_pause_does_not_fire_stop_hook(monkeypatch):
    """pause 不调用 orchestrator 的 stop hook 路径。"""
    worker = _worker()
    orch = _make_orch()
    orch.pool.get.return_value = worker
    orch.pool.list_sessions.return_value = [worker]
    monkeypatch.setattr(orch, "_decide_stop", MagicMock())
    monkeypatch.setattr(orch, "_run_plugin_stop_interceptors", AsyncMock())

    plugin = BreatherPlugin(_cfg(), Path("."))
    plugin.install(_ctx(orch))
    plugin.on_stop_hook = MagicMock()

    asyncio.run(worker.pause())

    plugin.on_stop_hook.assert_not_called()
    getattr(orch, "_decide_stop").assert_not_called()
    getattr(orch, "_run_plugin_stop_interceptors").assert_not_called()


def test_resume_then_pause_no_handoff():
    """resume → pause 循环不触发 handoff（回归测试）。"""
    worker = _worker()
    orch = _make_orch()
    orch.pool.get.return_value = worker
    orch.pool.list_sessions.return_value = [worker]

    plugin = BreatherPlugin(_cfg(), Path("."))
    plugin.install(_ctx(orch))

    for _ in range(3):
        asyncio.run(worker.resume())
        assert worker.state == SessionState.RUNNING

        asyncio.run(worker.pause())
        assert worker.state == SessionState.IDLE
        assert not getattr(plugin, "_handoff_pending")
        assert not getattr(plugin, "_handoff_finalizing")


def test_stop_hook_skips_handoff_when_pause_requested(monkeypatch):
    """pause_requested=True 时，on_stop_hook 不触发 handoff。"""
    worker = _worker()
    worker.pause_requested = True

    orch = _make_orch()
    orch.pool.get.return_value = worker

    plugin = BreatherPlugin(_cfg(), Path("."))
    plugin.install(_ctx(orch))
    monkeypatch.setattr(plugin, "_handoff_pending", True)
    monkeypatch.setattr(plugin, "_pending_handoff_session_id", worker.id)

    result = plugin.on_stop_hook(worker.id, worker, "final text")

    assert result is None
    assert not getattr(plugin, "_handoff_finalizing")
