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

"""交接运行时回归：真实 Orchestrator 下 execute_handoff 的停工/落账/prompt 语义。"""

from __future__ import annotations

import asyncio

import pytest

from workflow_controller.config.settings import AppConfig, ReviewerConfig, SessionConfig
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.session.session import Session, SessionState

from ._helpers import breather_plugin


class _BlockingLoop:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._current_session = None
        self._running = False
        self.evaluator = None

    async def take_over(self, session) -> None:
        self._current_session = session
        session.loop_status = "yes"

    def configure_for_session(self, session, evaluator) -> None:
        self.evaluator = evaluator

    async def run(self) -> None:
        self._running = True
        await self._stop_event.wait()
        self._running = False

    def is_current(self, session) -> bool:
        return self._current_session is session

    def current_session_brief(self):
        session = self._current_session
        if session is None:
            return None
        return {"id": session.id, "name": session.name}

    async def pause_current(self) -> None:
        self._current_session.loop_status = "no"

    def request_stop(self) -> None:
        self._stop_event.set()


def _blocking_loop_orchestrator(tmp_path, monkeypatch) -> Orchestrator:
    """建 _BlockingLoop 化的 orchestrator（reviewer 关闭，loop 永不自行结束）。"""
    config = AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False))
    orchestrator = Orchestrator(config, default_project_dir=tmp_path)
    monkeypatch.setattr(orchestrator, "_new_loop", lambda _sid: _BlockingLoop())
    return orchestrator


async def _handoff_ready_worker(orchestrator: Orchestrator, tmp_path) -> Session:
    """启动一个 RUNNING 态的 worker（execute_handoff 测试前置）。"""
    worker = await orchestrator.start_session_with_config(
        SessionConfig(
            name="worker",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="test",
        )
    )
    worker.state = SessionState.RUNNING
    return worker


async def _raise_at_handoff_creation(plugin, worker, tmp_path, error: type[Exception]) -> None:
    """执行默认 handoff/next 配置的交接，等待在 handoff 会话创建处抛 error。"""
    with pytest.raises(error):
        await asyncio.wait_for(
            plugin.execute_handoff(
                worker.id,
                SessionConfig(name="handoff", role="handoff_agent", cwd=str(tmp_path)),
                SessionConfig(name="next", role="worker", cwd=str(tmp_path)),
            ),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_execute_handoff_marks_drained_worker_idle_without_polling(
    tmp_path,
    monkeypatch,
) -> None:
    orchestrator = _blocking_loop_orchestrator(tmp_path, monkeypatch)
    worker = await _handoff_ready_worker(orchestrator, tmp_path)

    class ReachedHandoffCreationError(Exception):
        pass

    async def capture_worker_state(_config: SessionConfig, **_kwargs):
        assert worker.state is SessionState.IDLE
        assert worker.loop_status == "no"
        assert worker.stop_reason == "handoff_triggered"
        raise ReachedHandoffCreationError

    monkeypatch.setattr(orchestrator.pool, "create", capture_worker_state)
    plugin = breather_plugin(orchestrator)
    await _raise_at_handoff_creation(plugin, worker, tmp_path, ReachedHandoffCreationError)

    assert worker.id not in getattr(orchestrator, "_loops")
    assert worker.id not in getattr(orchestrator, "_loop_tasks")


@pytest.mark.asyncio
async def test_execute_handoff_persists_worker_once_with_final_idle(
    tmp_path,
    monkeypatch,
) -> None:
    orchestrator = _blocking_loop_orchestrator(tmp_path, monkeypatch)
    worker = await _handoff_ready_worker(orchestrator, tmp_path)

    persisted: list[tuple[str, str, str | None]] = []
    real_persist = getattr(orchestrator.pool, "_persist_session")

    async def record_persist(session):
        persisted.append((session.id, session.state.value, session.stop_reason))
        await real_persist(session)

    monkeypatch.setattr(orchestrator.pool, "_persist_session", record_persist)

    class ReachedHandoffCreationError(Exception):
        pass

    async def capture_worker_state(_config: SessionConfig, **_kwargs):
        raise ReachedHandoffCreationError

    monkeypatch.setattr(orchestrator.pool, "create", capture_worker_state)
    plugin = breather_plugin(orchestrator)
    await _raise_at_handoff_creation(plugin, worker, tmp_path, ReachedHandoffCreationError)

    # worker 记录只落一次账，且是终态 IDLE/handoff_triggered——不落中间 STOPPED 态
    assert [row for row in persisted if row[0] == worker.id] == [
        (worker.id, "idle", "handoff_triggered"),
    ]


@pytest.mark.asyncio
async def test_execute_handoff_preserves_caller_prompt_without_plugin_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    config = AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False))
    orchestrator = Orchestrator(config, default_project_dir=tmp_path)
    worker = await orchestrator.pool.create(
        SessionConfig(
            name="worker",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="test",
        )
    )

    class _PrefixPlugin:
        @staticmethod
        def prompt_prefix(config: object) -> str:
            return "PLUGIN-PREFIX"

    getattr(orchestrator.plugin_registry, "_plugins").append(_PrefixPlugin())

    class ReachedHandoffCreationError(Exception):
        pass

    captured: dict[str, str] = {}

    async def capture_handoff_config(cfg: SessionConfig, **_kwargs):
        captured[cfg.name] = cfg.initial_prompt
        raise ReachedHandoffCreationError

    monkeypatch.setattr(orchestrator.pool, "create", capture_handoff_config)
    plugin = breather_plugin(orchestrator)
    with pytest.raises(ReachedHandoffCreationError):
        await asyncio.wait_for(
            plugin.execute_handoff(
                worker.id,
                SessionConfig(
                    name="handoff",
                    role="handoff_agent",
                    cwd=str(tmp_path),
                    initial_prompt="EXACT-HANDOFF",
                ),
                SessionConfig(
                    name="next",
                    role="worker",
                    cwd=str(tmp_path),
                    initial_prompt="EXACT-NEXT",
                ),
            ),
            timeout=1,
        )

    # 交接/接续 config 的 prompt 原样生效，不聚合插件 prompt_prefix
    assert captured == {"handoff": "EXACT-HANDOFF"}
