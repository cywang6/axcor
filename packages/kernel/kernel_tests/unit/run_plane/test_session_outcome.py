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

"""全路径终态事件（session_outcome）与激活回执（session_activated）断言。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.config.settings import (
    AppConfig,
    LoopConfig,
    ReviewerConfig,
    SessionConfig,
)
from workflow_controller.core.evaluator import EvaluationResult
from workflow_controller.core.loop import SingleLoopEngine
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.plugin.base import IterationGateResult
from workflow_controller.session.events import Event, EventType, outcome_kind_for_reason
from workflow_controller.session.session import Session, SessionState


class _OutcomeRecorder:
    """直接挂在 session 上的结局记录器（绕过总线，聚焦五类路径的发射事实）。"""

    def __init__(self, session: Session) -> None:
        self.calls: list[tuple[str, str | None]] = []
        session.set_outcome_emitter(self._record)

    async def _record(self, session: Session, kind: str) -> None:
        self.calls.append((kind, session.stop_reason))


def test_gate_failed_outcome_kind_is_failed():
    assert outcome_kind_for_reason("gate_failed") == "failed"


def _engine_doubles() -> tuple[MagicMock, MagicMock]:
    router = MagicMock()
    router.update_loop_status = AsyncMock()
    router.store_loop_result = AsyncMock()
    router.store_evaluation = AsyncMock()
    router.get_events = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.emit = AsyncMock()
    return router, bus


def _worker(loop_max: int = 5) -> Session:
    return Session(
        config=SessionConfig(
            name="worker",
            role="worker",
            initial_prompt="p",
            loop=LoopConfig(max_iterations=loop_max),
        )
    )


def _yielding_query(session: Session):
    async def query(_prompt: str, iteration: int = 1):
        yield Event.create(
            session_id=session.id,
            iteration=iteration,
            event_type=EventType.ASSISTANT_MESSAGE,
            data={"content": []},
        )

    return query


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
    orchestrator = Orchestrator(
        AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
        default_project_dir=tmp_path,
    )
    monkeypatch.setattr(orchestrator, "_new_loop", lambda _sid: _BlockingLoop())
    return orchestrator


async def _drain_async_work(predicate, timeout: float = 2.0) -> None:
    """等待 spawn 出的发射任务落定（有界轮询，避免时序脆断言）。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "异步发射任务未在时限内完成"
        await asyncio.sleep(0.01)


# ── 路径 1：成功三种（统一发射点，载荷升级为 kind=completed）────────────


async def test_evaluator_complete_emits_completed_outcome():
    router, bus = _engine_doubles()
    evaluator = MagicMock()
    evaluator.evaluate = AsyncMock(
        return_value=EvaluationResult(
            is_complete=True,
            progress=1.0,
            summary="done",
        )
    )
    engine = SingleLoopEngine(router, bus)
    session = _worker()
    session.query = _yielding_query(session)
    rec = _OutcomeRecorder(session)

    await engine.take_over(session)
    engine.configure_for_session(session, evaluator)
    await asyncio.wait_for(engine.run(), timeout=2)

    assert rec.calls == [("completed", "evaluator_complete")]


# ── 路径 2：迭代异常（直写旁路收编进统一发射点）──────────────────────


async def test_iteration_exception_emits_failed_outcome():
    router, bus = _engine_doubles()
    router.get_events = AsyncMock(side_effect=RuntimeError("boom"))
    engine = SingleLoopEngine(router, bus)
    session = _worker()
    session.query = _yielding_query(session)
    rec = _OutcomeRecorder(session)

    await engine.take_over(session)
    engine.configure_for_session(session, None)
    await asyncio.wait_for(engine.run(), timeout=2)

    assert rec.calls == [("failed", "error")]
    assert session.last_error and "RuntimeError" in session.last_error


# ── 路径 3：driver 级错误（session 侧发射，kind=failed）──────────────


async def test_driver_query_error_emits_failed_outcome(monkeypatch):
    session = _worker()
    rec = _OutcomeRecorder(session)

    class _RaisingQuery:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("driver dead")

    def fake_driver_stream(prompt, **kwargs):
        return _RaisingQuery()

    driver = MagicMock()
    driver.query = fake_driver_stream
    monkeypatch.setattr(
        "workflow_controller.driver.factory.create_driver",
        lambda *a, **k: driver,
    )
    events = [e async for e in session.query("p", iteration=1)]

    assert rec.calls == [("failed", "error")]
    assert [e.event_type for e in events[-2:]] == [EventType.ERROR, EventType.ITERATION_END]
    assert session.last_error and "driver dead" in session.last_error


# ── 路径 4：gate stop / gate-block max_iter（双路径一致性）────────────


async def test_gate_stop_emits_stopped_outcome():
    router, bus = _engine_doubles()

    async def gate(_sid, _session, _iteration):
        return IterationGateResult(action="stop", reason="breather_handoff")

    engine = SingleLoopEngine(router, bus, iteration_gate=gate)
    session = _worker()
    session.query = _yielding_query(session)
    rec = _OutcomeRecorder(session)

    await engine.take_over(session)
    engine.configure_for_session(session, None)
    await asyncio.wait_for(engine.run(), timeout=2)

    assert rec.calls == [("stopped", "breather_handoff")]


async def test_max_iterations_outcome_consistent_across_gate_and_plain_paths():
    # gate block 走到上限
    router, bus = _engine_doubles()

    async def gate_block(_sid, _session, _iteration):
        return IterationGateResult(action="block", feedback="redo")

    gated = _worker(loop_max=1)
    gated.query = _yielding_query(gated)
    gated_rec = _OutcomeRecorder(gated)
    gated_engine = SingleLoopEngine(router, bus, iteration_gate=gate_block)
    await gated_engine.take_over(gated)
    gated_engine.configure_for_session(gated, None)
    await asyncio.wait_for(gated_engine.run(), timeout=2)

    # 无 gate 自然到上限
    plain = _worker(loop_max=1)
    plain.query = _yielding_query(plain)
    plain_rec = _OutcomeRecorder(plain)
    plain_engine = SingleLoopEngine(router, bus)
    await plain_engine.take_over(plain)
    plain_engine.configure_for_session(plain, None)
    await asyncio.wait_for(plain_engine.run(), timeout=2)

    assert gated_rec.calls == plain_rec.calls == [("completed", "max_iterations")]


# ── 路径 5：user_pause 全路径 / 切换监视 / kind 消歧──────────────────


async def test_pause_stop_and_kind_disambiguation():
    router, bus = _engine_doubles()
    engine = SingleLoopEngine(router, bus)
    session = _worker()
    rec = _OutcomeRecorder(session)

    await engine.take_over(session)
    await engine.pause_current()
    assert rec.calls == [("paused", "user_pause")]

    # 已终态再 pause 不重发
    await session.pause()
    assert len(rec.calls) == 1

    # user_stop 的 stop_reason 历史上写作 user_pause——kind 消歧为 stopped
    await session.stop("user_stop")
    assert rec.calls[-1] == ("stopped", "user_pause")

    # auth 失败类意图 → failed
    await session.stop("auth_error")
    assert rec.calls[-1] == ("failed", "auth_error")


async def test_switching_session_emits_stopped_outcome_for_old_one():
    router, bus = _engine_doubles()
    engine = SingleLoopEngine(router, bus)
    old_session = _worker()
    old_rec = _OutcomeRecorder(old_session)

    await engine.take_over(old_session)
    await engine.take_over(_worker())

    assert old_rec.calls == [("stopped", "switched_to_another")]


# ── 激活回执 + 真实总线链路（SO 发射 → 观察者可见）───────────────────


async def _fail_activation(orchestrator: Orchestrator, monkeypatch) -> None:
    """让下一个 session 的激活前段失败（provider 校验不过）。"""

    async def _reject(session) -> None:
        raise ValueError("provider validation failed")

    monkeypatch.setattr(orchestrator, "_validate_session_provider_bindings", _reject)


async def test_activation_receipt_success_and_failure(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    receipts: list[Event] = []

    async def capture(event: Event) -> None:
        receipts.append(event)

    orchestrator.bus.observe_awaited(EventType.SESSION_ACTIVATED, capture)

    session = await orchestrator.start_session_with_config(
        SessionConfig(
            name="first",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="p",
        )
    )
    await _drain_async_work(lambda: receipts)

    assert [r.data for r in receipts if r.session_id == session.id] == [
        {"ok": True, "error": None},
    ]
    # 激活回执带 run 维度（隐式 mint 在创建咽喉先行完成）
    assert receipts[0].run_id == session.run_id

    await _fail_activation(orchestrator, monkeypatch)
    with pytest.raises(ValueError):
        await orchestrator.start_session_with_config(
            SessionConfig(
                name="second",
                role="worker",
                cwd=str(tmp_path),
                initial_prompt="p",
            )
        )

    failed = [r for r in receipts if r.data.get("ok") is False]
    assert len(failed) == 1
    assert "provider validation failed" in failed[0].data["error"]


async def test_activation_failure_receipt_carries_explicit_run_stamp(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    receipts: list[Event] = []

    async def capture(event: Event) -> None:
        receipts.append(event)

    orchestrator.bus.observe_awaited(EventType.SESSION_ACTIVATED, capture)

    run = await orchestrator.run_store.create("prog", "dag-yaml@1")
    await orchestrator.start_session_with_config(
        SessionConfig(
            name="first",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="p",
        )
    )
    # 显式 run 的 session 激活失败（provider 校验不过）：run 活过失败，回执须带 run 戳
    await _fail_activation(orchestrator, monkeypatch)
    with pytest.raises(ValueError):
        await orchestrator.start_session_with_config(
            SessionConfig(
                name="second",
                role="worker",
                cwd=str(tmp_path),
                initial_prompt="p",
                run_id=run.run_id,
            )
        )
    await _drain_async_work(
        lambda: [r for r in receipts if r.data.get("ok") is False],
    )

    failed = [r for r in receipts if r.data.get("ok") is False]
    assert len(failed) == 1
    assert failed[0].run_id == run.run_id


async def test_outcome_payload_shape_through_real_emitter(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    outcomes: list[Event] = []

    async def capture(event: Event) -> None:
        outcomes.append(event)

    orchestrator.bus.observe_awaited(EventType.SESSION_OUTCOME, capture)

    session = await orchestrator.start_session_with_config(
        SessionConfig(
            name="worker",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="p",
        )
    )
    await orchestrator.stop_session(session.id)
    await _drain_async_work(lambda: outcomes)

    assert outcomes, "session_outcome 未经总线发射"
    payload = outcomes[-1].data
    assert set(payload) == {
        "session_id",
        "kind",
        "stop_reason",
        "last_error",
        "result_digest",
        "iteration",
        "context",
    }
    assert payload["kind"] == "stopped"
    assert payload["session_id"] == session.id


async def test_state_changed_emitted_through_spawned_task(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    states: list[tuple[str, str]] = []

    async def capture(event: Event) -> None:
        states.append((event.data["from"], event.data["to"]))

    orchestrator.bus.observe_awaited(EventType.SESSION_STATE_CHANGED, capture)

    session = await orchestrator.start_session_with_config(
        SessionConfig(
            name="worker",
            role="worker",
            cwd=str(tmp_path),
            initial_prompt="p",
        )
    )
    session.state = SessionState.RUNNING
    await _drain_async_work(lambda: states)

    assert ("idle", "running") in states
