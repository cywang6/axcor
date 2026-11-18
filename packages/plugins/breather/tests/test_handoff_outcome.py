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

"""交接失败的结局改判回归：超时/失败直写补发 failed 结局事件。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from workflow_controller.config.settings import AppConfig, ReviewerConfig, SessionConfig
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.session.events import Event, EventType

from ._helpers import breather_plugin

if TYPE_CHECKING:
    from workflow_controller.session.session import Session


class _BlockingLoop:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._current_session = None

    async def take_over(self, session) -> None:
        self._current_session = session
        session.loop_status = "yes"

    def configure_for_session(self, session, evaluator) -> None:
        pass

    async def run(self) -> None:
        await self._stop_event.wait()

    def is_current(self, session) -> bool:
        return self._current_session is session

    async def pause_current(self) -> None:
        self._current_session.loop_status = "no"

    def request_stop(self) -> None:
        self._stop_event.set()


def _orchestrator(tmp_path, monkeypatch) -> Orchestrator:
    """起 _BlockingLoop 化的 orchestrator（session 停止路径不并发干扰断言）。"""
    orchestrator = Orchestrator(
        AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
        default_project_dir=tmp_path,
    )
    monkeypatch.setattr(orchestrator, "_new_loop", lambda _sid: _BlockingLoop())
    return orchestrator


async def _outcome_observing_orchestrator(tmp_path, monkeypatch) -> tuple[Orchestrator, list]:
    """起 orchestrator 并挂 SESSION_OUTCOME 捕获；返回 (orchestrator, outcomes)。"""
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    outcomes: list[Event] = []

    async def capture(event: Event) -> None:
        outcomes.append(event)

    orchestrator.bus.observe_awaited(EventType.SESSION_OUTCOME, capture)
    return orchestrator, outcomes


async def _start_worker_session(orchestrator: Orchestrator, tmp_path) -> Session:
    """以标准 worker 配置（cwd 指向 tmp_path）启动会话。"""
    return await orchestrator.start_session_with_config(
        SessionConfig(
            name="worker",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="p",
        )
    )


async def test_handoff_timeout_direct_write_emits_failed_outcome(tmp_path, monkeypatch):
    orchestrator, outcomes = await _outcome_observing_orchestrator(tmp_path, monkeypatch)
    worker = await _start_worker_session(orchestrator, tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "wait_session_done",
        AsyncMock(return_value=False),
    )

    plugin = breather_plugin(orchestrator)
    result = await plugin.execute_handoff(
        worker.id,
        SessionConfig(name="handoff", role="handoff", cwd=str(tmp_path), initial_prompt="h"),
        SessionConfig(name="next", role="worker", cwd=str(tmp_path), initial_prompt="p"),
    )

    assert result["success"] is False
    # worker 的 handoff_triggered 是 stopped；handoff 会话超时直写须补发 failed
    timeout_outcomes = [e for e in outcomes if e.data["stop_reason"] == "timeout"]
    assert [e.data["kind"] for e in timeout_outcomes] == ["failed"]
    assert timeout_outcomes[0].session_id != worker.id


async def test_handoff_failure_direct_write_emits_failed_outcome(tmp_path, monkeypatch):
    orchestrator, outcomes = await _outcome_observing_orchestrator(tmp_path, monkeypatch)
    worker = await _start_worker_session(orchestrator, tmp_path)
    # wait_session_done 正常返回但 result != success：走失败分支的直写改判
    monkeypatch.setattr(
        orchestrator,
        "wait_session_done",
        AsyncMock(return_value=True),
    )

    plugin = breather_plugin(orchestrator)
    result = await plugin.execute_handoff(
        worker.id,
        SessionConfig(name="handoff", role="handoff", cwd=str(tmp_path), initial_prompt="h"),
        SessionConfig(name="next", role="worker", cwd=str(tmp_path), initial_prompt="p"),
    )

    assert result["success"] is False
    fail_outcomes = [e for e in outcomes if e.data["stop_reason"] == "error"]
    assert [e.data["kind"] for e in fail_outcomes] == ["failed"]
    assert fail_outcomes[0].session_id == result["handoff_session_id"]
