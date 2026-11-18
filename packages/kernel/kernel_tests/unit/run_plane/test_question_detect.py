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

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.api.session_api import SessionInfo
from workflow_controller.config.settings import LoopConfig, SessionConfig, SessionReviewerConfig
from workflow_controller.core.loop import SingleLoopEngine
from workflow_controller.core.orchestrator_helpers import _session_reviewer_mode
from workflow_controller.session.events import Event, EventType
from workflow_controller.session.session import Session

_DEFAULT_QUESTION_TEXT = "两种方案，是否继续执行方案 A？"


def _question_event(session_id: str, text: str = _DEFAULT_QUESTION_TEXT) -> Event:
    return Event.create(
        session_id=session_id,
        iteration=1,
        event_type=EventType.ASSISTANT_MESSAGE,
        data={"content": [{"type": "text", "text": text}]},
    )


def _setup(
    question_detect: bool | None,
    text: str = _DEFAULT_QUESTION_TEXT,
    reviewer: SessionReviewerConfig | None = None,
) -> tuple[SingleLoopEngine, Session, MagicMock]:
    # get_events 恒返回问句消息：检测未跳过时必命中等人等待
    router = MagicMock()
    router.update_loop_status = AsyncMock()
    router.store_loop_result = AsyncMock()
    router.get_events = AsyncMock(side_effect=lambda sid: [_question_event(sid, text)])
    bus = MagicMock()
    bus.emit = AsyncMock()
    session = Session(
        config=SessionConfig(
            name="worker",
            loop=LoopConfig(max_iterations=1, question_detect=question_detect),
            reviewer=reviewer,
        )
    )

    async def query(_prompt: str, iteration: int = 1):
        if iteration == 1:
            yield _question_event(session.id, text)

    session.query = query  # type: ignore[method-assign]
    return (
        SingleLoopEngine(router, bus, mode_getter=lambda: _session_reviewer_mode(session, "auto")),
        session,
        bus,
    )


def _reasons(bus: MagicMock) -> list[str]:
    return [
        call.args[0].data.get("reason")
        for call in bus.emit.await_args_list
        if call.args[0].event_type == EventType.LOG_LOOP
    ]


async def _run(engine: SingleLoopEngine, session: Session) -> None:
    # 检测未跳过时 run 会挂在等人输入上，wait_for 超时即失败
    await engine.take_over(session)
    engine.configure_for_session(session, None)
    await asyncio.wait_for(engine.run(), timeout=2)
    assert session.stop_reason == "max_iterations"


async def test_none_default_waits_for_user_answer():
    engine, session, bus = _setup(question_detect=None)

    async def answer_later() -> None:
        await asyncio.sleep(0.05)
        await session.inject_prompt("继续方案 A")

    asyncio.get_running_loop().create_task(answer_later())
    await _run(engine, session)
    assert "unanswered_question" in _reasons(bus)


@pytest.mark.parametrize(
    ("enabled", "mode", "fallback"),
    [(True, "auto", True), (True, "manual", False), (False, "auto", False)],
)
async def test_reply_mode_during_question_wait_and_next_query(enabled, mode, fallback):
    reviewer = SessionReviewerConfig(enabled=enabled, mode=mode)
    engine, session, bus = _setup(question_detect=None, reviewer=reviewer)
    session.config.loop.max_iterations = 2
    waiting = asyncio.Event()
    next_query = asyncio.Event()
    finish_query = asyncio.Event()

    async def observe(event):
        if event.data.get("reason") == "unanswered_question":
            waiting.set()

    async def query(prompt: str, iteration: int = 1):
        if iteration == 1:
            yield _question_event(session.id)
        else:
            assert "继续方案 A" in prompt
            assert _session_reviewer_mode(session, "auto") == mode
            assert not SessionInfo.from_session(session).waiting_for_auto_reply_fallback
            next_query.set()
            await finish_query.wait()

    bus.emit.side_effect = observe
    session.query = query  # type: ignore[method-assign]
    await engine.take_over(session)
    task = asyncio.create_task(engine.run())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        info = SessionInfo.from_session(session)
        expected_mode = "manual" if fallback else mode
        assert _session_reviewer_mode(session, "auto") == expected_mode
        assert info.reviewer_mode == expected_mode
        assert info.waiting_for_auto_reply_fallback is fallback
        assert reviewer.mode == mode
        await session.inject_prompt("继续方案 A")
        await asyncio.wait_for(next_query.wait(), timeout=2)
    finally:
        engine.request_stop()
        await asyncio.wait_for(task, timeout=2)


async def test_cancelled_wait_releases_reply_mode_and_leaves_later_input_available():
    engine, session, bus = _setup(question_detect=None, reviewer=SessionReviewerConfig())
    waiting = asyncio.Event()

    async def observe(event):
        if event.data.get("reason") == "unanswered_question":
            waiting.set()

    bus.emit.side_effect = observe
    await engine.take_over(session)
    task = asyncio.create_task(engine.run())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    engine.request_stop()
    await asyncio.wait_for(task, timeout=2)

    assert _session_reviewer_mode(session, "auto") == "auto"
    await session.inject_prompt("恢复时继续方案 A")
    await asyncio.sleep(0)
    assert await session.poll_manual_prompt() == "恢复时继续方案 A"


@pytest.mark.parametrize(
    "text",
    ["两种方案已列出，请指示下一步方向。", "batch done, shall i proceed with the next one."],
)
async def test_loop_only_marker_still_waits_for_user_answer(text):
    # 措辞仅在 loop 词表、不在 reviewer 共享默认词表，且不以问号结尾
    engine, session, bus = _setup(question_detect=None, text=text)

    async def answer_later() -> None:
        await asyncio.sleep(0.05)
        await session.inject_prompt("继续")

    asyncio.get_running_loop().create_task(answer_later())
    await _run(engine, session)
    assert "unanswered_question" in _reasons(bus)


async def test_false_skips_question_detection():
    engine, session, bus = _setup(question_detect=False)
    await _run(engine, session)
    assert "unanswered_question" not in _reasons(bus)
