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

from unittest.mock import AsyncMock, MagicMock

from workflow_controller.core.hook_decision import HookDecision
from workflow_controller.core.reviewer import ReviewerInterceptor
from workflow_controller.session.events import Event


def _stop_interceptor(
    *,
    in_flight: bool,
) -> tuple[ReviewerInterceptor, MagicMock, MagicMock]:
    host = MagicMock()
    host.get_reviewer_mode.return_value = "auto"
    host.get_stop_block_count.return_value = 0
    host.subagent_status.return_value = (
        {"agent-1": {"agent_type": "general-purpose"}} if in_flight else {}
    )
    host.reviewer_for.return_value.decide_stop = AsyncMock(
        return_value=HookDecision("let_exit", "", "done"),
    )
    emitter = MagicMock()
    return ReviewerInterceptor(host, MagicMock(), emitter), host, emitter


def _stop_event(text: str = "等待子代理完成") -> Event:
    return Event.create(
        session_id="session-1",
        iteration=1,
        event_type="stop_requested",
        data={"final_text": text, "block_key": "claude-query-1"},
    )


async def test_in_flight_subagent_allows_stop_without_completing_loop() -> None:
    interceptor, host, emitter = _stop_interceptor(in_flight=True)

    result = await getattr(interceptor, "_on_stop")(_stop_event())

    assert result is not None and result.action == "stop"
    emitter.emit_async.assert_called_once_with(
        "session-1",
        "stop_hook_native_subagent_wait",
        {
            "action": "stop",
            "rationale": "query remains active while sub-agents run",
            "subagents": [{"agent_type": "general-purpose", "agent_id": "agent-1"}],
        },
    )
    host.reviewer_for.return_value.decide_stop.assert_not_awaited()
    host.mark_loop_let_exit.assert_not_called()
    host.schedule_retry.assert_not_called()
    host.bump_stop_block_count.assert_not_called()


async def test_reviewer_resumes_after_subagent_finishes() -> None:
    interceptor, host, _ = _stop_interceptor(in_flight=True)

    first = await getattr(interceptor, "_on_stop")(_stop_event())
    host.subagent_status.return_value = {}
    second = await getattr(interceptor, "_on_stop")(_stop_event("任务已经完成"))

    assert first is not None and first.action == "stop"
    assert second is not None and second.action == "stop"
    host.reviewer_for.return_value.decide_stop.assert_awaited_once()
    host.mark_loop_let_exit.assert_called_once_with("session-1")


async def test_in_flight_subagent_short_circuits_nl_question() -> None:
    interceptor, host, _ = _stop_interceptor(in_flight=True)
    host.reviewer_for.return_value.decide_nl_question = AsyncMock(
        return_value=HookDecision("block_with_feedback", "", "ask user"),
    )

    result = await getattr(interceptor, "_on_stop")(_stop_event("要继续吗？"))

    assert result is not None and result.action == "stop"
    host.reviewer_for.return_value.decide_stop.assert_not_awaited()
    host.reviewer_for.return_value.decide_nl_question.assert_not_awaited()
    host.bump_stop_block_count.assert_not_called()


async def test_nl_question_review_still_runs_without_in_flight() -> None:
    interceptor, host, _ = _stop_interceptor(in_flight=False)
    host.reviewer_for.return_value.decide_nl_question = AsyncMock(
        return_value=HookDecision("block_with_feedback", "", "ask user"),
    )

    result = await getattr(interceptor, "_on_stop")(_stop_event("要继续吗？"))

    assert result is not None and result.action == "block"
    host.reviewer_for.return_value.decide_nl_question.assert_awaited_once()


async def test_regular_review_runs_without_in_flight() -> None:
    interceptor, host, _ = _stop_interceptor(in_flight=False)

    result = await getattr(interceptor, "_on_stop")(_stop_event("任务已经完成"))

    assert result is not None and result.action == "stop"
    host.reviewer_for.return_value.decide_stop.assert_awaited_once()
    host.mark_loop_let_exit.assert_called_once_with("session-1")
