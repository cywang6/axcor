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

"""Reviewer control-event interceptor + hook-decision emitter.

Phase 2.1 of the event-system refactor (see
docs/designs/event-system-refactor.md §5). Extracts the reviewer's 4
control-event interceptors (``stop_requested`` / ``pre_tool_use`` /
``iteration_boundary`` / ``gate_question``) plus the LLM-judgment
orchestration (``_run_llm_stop_review`` / ``_run_nl_question_review``)
and the decision-event emit pattern out of the orchestrator god object.

The orchestrator constructs a :class:`ReviewerInterceptor` in ``start()``
and calls ``register()``. It reads live reviewer / mode / loop state
through the narrow :class:`ReviewerHost` protocol, so ``restart_reviewer``
and ``set_reviewer_mode`` need no re-registration.

This module must NOT import ``orchestrator`` at runtime (cycle): the
host is a :class:`Protocol`, and ``Session`` / ``ReviewerService`` are
referenced under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

from workflow_controller.event.event_bus import EventBus, InterceptResult
from workflow_controller.session.events import Event, EventType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from workflow_controller.core.reviewer_service import ReviewerService
    from workflow_controller.plugin.base import IterationGateResult
    from workflow_controller.session.session import Session

logger = logging.getLogger(__name__)


# ── NL question detection ──────────────────────────────────────────────
# Moved here from orchestrator.py — only the reviewer stop path uses it.
# 中文核心段是 reviewer 停机词表与 loop 等人词表的公共部分（loop.py 经
# NL_QUESTION_CORE_ZH 引用拼装自己的完整词表），两侧的外围词项各自维护。
NL_QUESTION_CORE_ZH = [
    "需要你做出选择",
    "请选择",
    "请做出选择",
    "你的选择",
    "你想怎么做",
    "请确认",
    "请告诉我",
    "你想要",
    "你觉得",
    "你希望",
    "是否需要",
    "你想",
    "请决定",
    "你打算",
]

_NL_QUESTION_MARKERS = [
    "是否继续",
    "要继续吗",
    "需要我继续",
    "你想继续",
    *NL_QUESTION_CORE_ZH,
    # English markers
    "please choose",
    "please select",
    "your choice",
    "what would you like",
    "which option",
    "please confirm",
    "do you want",
    "would you like",
    "should i",
]


def is_nl_question(text: str, markers: Sequence[str] | None = None) -> bool:
    """自然语言疑问判定：命中问询词表或以中英文问号结尾。

    判定逻辑 reviewer 停机路径与 loop 单轮检查共用；词表各路径不同——
    缺省用 reviewer 停机词表，loop 传入自己的完整词表。
    """
    if markers is None:
        markers = _NL_QUESTION_MARKERS
    lower = text.lower()
    if any(m in lower for m in markers):
        return True
    stripped = text.strip()
    return bool(stripped and stripped[-1] in "?？")


class ReviewerHost(Protocol):
    """Narrow live seam the reviewer interceptor reads from its host
    (the orchestrator). All members are read at call time so runtime
    mutations (``restart_reviewer``, ``set_reviewer_mode``) need no
    re-registration of interceptors.
    """

    def reviewer_for(self, sid: str) -> ReviewerService | None:
        raise NotImplementedError

    def get_reviewer_mode(self, sid: str) -> str:
        raise NotImplementedError

    def subagent_status(self, sid: str) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    def get_stop_block_count(self, key: str) -> int:
        raise NotImplementedError

    def bump_stop_block_count(self, key: str) -> None:
        raise NotImplementedError

    def mark_loop_let_exit(self, sid: str) -> None:
        raise NotImplementedError

    def get_session(self, sid: str) -> Session | None:
        raise NotImplementedError

    def schedule_retry(self, sid: str) -> int:
        raise NotImplementedError

    async def resolve_boundary_pending(
        self,
        session: Session,
        iteration: int,
    ) -> IterationGateResult | None:
        raise NotImplementedError


class HookDecisionEmitter:
    """Owns the ``LOG_AUTO`` hook-decision event emission + the single
    fire-and-forget task pool for the reviewer/stop-hook path.

    ``emit`` is the awaited form (caller cares about ordering before the
    next event); ``emit_async`` schedules ``emit`` as a tracked task.
    ``shutdown`` cancels+drains the pool (teardown).
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def extract_auq_questions(tool_input: Any) -> list[Any]:
        """Parse the raw AskUserQuestion tool_input payload into AskQuestion list.

        PreToolUse hook delivers tool_input as the *raw* dict CC sent to the
        tool (``{"questions": [{...}, ...]}``), not the wrapped Event format
        that ``question_payload.extract_questions`` expects. So we do the
        mapping inline here.
        """
        from workflow_controller.core.question_payload import ask_question_from_raw

        if not isinstance(tool_input, dict):
            return []
        raw_questions = tool_input.get("questions", [])
        if not isinstance(raw_questions, list):
            return []
        out: list[Any] = []
        for raw_q in raw_questions:
            if not isinstance(raw_q, dict):
                continue
            out.append(ask_question_from_raw(raw_q, str(raw_q.get("tool_use_id", ""))))
        return out

    @staticmethod
    def _summarize_questions(questions: list[Any]) -> list[dict[str, Any]]:
        """Extract minimal summary from AskQuestion list for event storage."""
        result: list[dict[str, Any]] = []
        for q in questions:
            options = [
                {"label": o.label, "desc": o.description[:80]} for o in getattr(q, "options", [])
            ]
            result.append(
                {
                    "header": getattr(q, "header", ""),
                    "question": getattr(q, "question", "")[:200],
                    "options": options,
                }
            )
        return result

    async def emit(
        self,
        workflow_controller_session_id: str,
        hook: str,
        decision: Any,
        questions: list[Any] | None = None,
    ) -> None:
        if isinstance(decision, dict):
            data: dict[str, Any] = {
                "hook": hook,
                "decision": decision.get("action", decision.get("decision", ""))
                or decision.get("answer", ""),
                "feedback": str(decision.get("feedback", ""))[:300],
                "rationale": str(decision.get("rationale", ""))[:300],
            }
            if "block_count" in decision:
                data["block_count"] = decision["block_count"]
            if "n_questions" in decision:
                data["n_questions"] = decision["n_questions"]
            if "subagents" in decision:
                data["subagents"] = decision["subagents"]
            if questions:
                data["questions"] = self._summarize_questions(questions)
        else:
            data = {
                "hook": hook,
                "decision": getattr(decision, "decision", "")[:50],
                "feedback": getattr(decision, "feedback_to_worker", "")[:300],
                "rationale": getattr(decision, "rationale", "")[:300],
                "selected_option": getattr(decision, "selected_option", None),
            }
        try:
            await self._bus.emit(
                Event.create(
                    session_id=workflow_controller_session_id,
                    iteration=0,
                    event_type=EventType.LOG_AUTO,
                    data=data,
                )
            )
        except Exception:
            logger.exception("failed to emit hook decision event")

    def emit_async(
        self,
        workflow_controller_session_id: str,
        hook: str,
        decision: Any,
        questions: list[Any] | None = None,
    ) -> asyncio.Task[None]:
        """Fire-and-forget variant: schedule ``emit`` and track the task."""
        task = asyncio.create_task(
            self.emit(workflow_controller_session_id, hook, decision, questions)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel + drain in-flight emit tasks. Called from
        ``Orchestrator.shutdown()`` so fire-and-forget emits don't leak
        on teardown.
        """
        pending = [t for t in list(self._tasks) if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


class ReviewerInterceptor:
    """Wires :class:`ReviewerService` into the bus control events.

    Owns reviewer method dispatch, auto/manual mode branching, and
    fire-and-forget ``LOG_AUTO`` decision-event emission for the 4
    control events (priority 0). The orchestrator only constructs +
    registers this; the non-reviewer stop short-circuit (staged deferral)
    lives in the orchestrator's own priority-5 ``_on_stop_pre`` interceptor.
    """

    def __init__(
        self,
        host: ReviewerHost,
        bus: EventBus,
        hook_decision_emitter: HookDecisionEmitter,
    ) -> None:
        self._host = host
        self._bus = bus
        self._emitter = hook_decision_emitter

    def register(self) -> None:
        self._bus.intercept("stop_requested", self._on_stop, priority=0)
        self._bus.intercept("pre_tool_use", self._on_pre_tool_use, priority=0)
        self._bus.intercept("iteration_boundary", self._on_iteration_boundary, priority=0)
        self._bus.intercept("gate_question", self._on_gate_question, priority=0)

    async def _on_stop(self, event: Event) -> InterceptResult | None:
        mig_session_id = event.session_id
        final_text = event.data.get("final_text", "")
        block_key = event.data.get("block_key", "")

        # Manual mode: gate had no objection → let CC end naturally; user
        # injects via the Web UI InputBox, LoopEngine unblocks from queue,
        # next iter resumes session. (handoff/staged short-circuits already
        # ran in the orchestrator's priority-5 _on_stop_pre.)
        if self._host.get_reviewer_mode(mig_session_id) == "manual":
            logger.info(
                "stop_hook: %s manual — gate passed, hand final say to human",
                mig_session_id[:12],
            )
            return InterceptResult(action="stop")

        prior = self._host.get_stop_block_count(block_key)
        reviewer = self._host.reviewer_for(mig_session_id)

        # 在途子代理非空时直接放行本次 Stop：主 agent 只是在等子代理结果，
        # 原查询仍在运行，不经 reviewer 判定。检查必须先于疑问句分支，
        # 否则带疑问句的等待会被误路由到提问路径造成刷屏。
        in_flight = self._host.subagent_status(mig_session_id)
        if in_flight:
            self._emitter.emit_async(
                mig_session_id,
                "stop_hook_native_subagent_wait",
                {
                    "action": "stop",
                    "rationale": "query remains active while sub-agents run",
                    "subagents": [{**v, "agent_id": k} for k, v in in_flight.items()],
                },
            )
            return InterceptResult(action="stop")

        if (
            reviewer is not None
            and self._host.get_reviewer_mode(mig_session_id) != "manual"
            and is_nl_question(final_text)
        ):
            logger.info(
                "stop_hook: detected NL question for %s, routing to nl_question",
                mig_session_id[:12],
            )
            result = await self._run_nl_question_review(
                block_key,
                mig_session_id,
                final_text,
                prior,
            )
            if result is None:
                return InterceptResult(action="stop")
            return InterceptResult(action="block", feedback=result.get("reason", ""))

        result = await self._run_llm_stop_review(
            block_key,
            mig_session_id,
            final_text,
            prior,
        )
        if result is None:
            return InterceptResult(action="stop")
        return InterceptResult(action="block", feedback=result.get("reason", ""))

    def _emit_auq_llm_decision(self, session_id: str, questions: list[Any], decision: Any) -> None:
        """把 AUQ 的 LLM 判定经 pretool_hook_llm 事件外发（两个 AUQ 入口共用）。"""
        self._emitter.emit_async(
            session_id,
            "pretool_hook_llm",
            {
                "action": decision.action,
                "feedback": decision.feedback,
                "rationale": decision.rationale,
                "n_questions": len(questions),
            },
            questions=questions,
        )

    async def _on_pre_tool_use(self, event: Event) -> InterceptResult | None:
        mig_session_id = event.session_id
        reviewer = self._host.reviewer_for(mig_session_id)
        if reviewer is None:
            return None
        data = event.data

        if self._host.get_reviewer_mode(mig_session_id) == "manual":
            manual_reason = (
                "请等待用户从界面输入回复。在用户消息到来前不要重复提问、不要继续推进流程。"
            )
            self._emitter.emit_async(
                mig_session_id,
                "pretool_hook_deny_manual",
                {"answer": manual_reason, "fixed_message": True},
            )
            return InterceptResult(action="block", feedback=manual_reason)

        questions = HookDecisionEmitter.extract_auq_questions(data.get("tool_input"))
        decision = await reviewer.decide_auq(questions)

        self._emit_auq_llm_decision(mig_session_id, questions, decision)
        return InterceptResult(action="block", feedback=decision.feedback)

    async def _on_gate_question(self, event: Event) -> InterceptResult | None:
        workflow_controller_session_id = event.session_id
        reviewer = self._host.reviewer_for(workflow_controller_session_id)
        if reviewer is None:
            return InterceptResult(action="block", feedback="")

        questions = HookDecisionEmitter.extract_auq_questions(
            event.data.get("tool_input", {}),
        )

        if self._host.get_reviewer_mode(workflow_controller_session_id) != "manual":
            decision = await reviewer.decide_auq(questions)
            self._emit_auq_llm_decision(workflow_controller_session_id, questions, decision)
            return InterceptResult(action="block", feedback=decision.feedback)

        # manual: surface question, wait for human
        manual_reason = "请等待用户从界面输入回复。在用户消息到来前不要重复提问、不要继续推进流程。"
        self._emitter.emit_async(
            workflow_controller_session_id,
            "pretool_hook_deny_manual",
            {"answer": manual_reason, "fixed_message": True},
            questions=questions,
        )

        session_obj = self._host.get_session(workflow_controller_session_id)
        if session_obj is None:
            logger.warning(
                "gate manual: session %s not in pool; returning empty answer",
                workflow_controller_session_id[:12],
            )
            return InterceptResult(action="block", feedback="")
        human_input = await session_obj.await_manual_prompt()
        return InterceptResult(action="block", feedback=human_input)

    async def _on_iteration_boundary(
        self,
        event: Event,
    ) -> InterceptResult | None:
        session = self._host.get_session(event.session_id)
        if session is None:
            return None
        iteration = event.data.get("iteration", 0)
        try:
            result = await self._host.resolve_boundary_pending(session, iteration)
        except Exception:
            logger.exception(
                "iteration_boundary: reviewer raised for %s, "
                "returning block to prevent fall-through",
                event.session_id[:12],
            )
            return InterceptResult(
                action="block",
                feedback="reviewer 不可用，请重新汇报当前阶段完成情况。",
                reason="reviewer_error",
            )
        if result is None:
            return None
        if result.action == "block":
            return InterceptResult(
                action="block",
                feedback=result.feedback,
                reason=result.reason,
            )
        if result.action == "stop":
            return InterceptResult(action="stop", reason=result.reason)
        return None

    async def _run_llm_stop_review(
        self,
        block_key: str,
        mig_session_id: str,
        final_text: str,
        prior: int,
    ) -> dict[str, Any] | None:
        """Ask the reviewer LLM to decide as a stand-in user for the stop hook."""
        reviewer = self._host.reviewer_for(mig_session_id)
        if reviewer is None:
            # Defensive: _decide_stop guards `reviewer is None` before emitting
            # stop_requested, so this branch is unreachable today. Use an
            # explicit guard (not assert) so it survives `python -O` and
            # degrades gracefully (worker exits turn) if a future path emits
            # stop_requested without that guard. Mirrors _on_pre_tool_use /
            # _on_gate_question which already do this.
            return None
        # decide_stop never raises (subprocess errors → fallback block).
        decision = await reviewer.decide_stop(final_text)
        logger.info(
            "stop_hook: reviewer %s for %s — %s",
            decision.action,
            mig_session_id[:12],
            decision.rationale,
        )

        self._emitter.emit_async(
            mig_session_id,
            "stop_hook_llm",
            {
                "action": decision.action,
                "feedback": decision.feedback,
                "rationale": decision.rationale,
                "block_count": prior + (1 if decision.action == "block_with_feedback" else 0),
            },
        )

        if decision.action == "let_exit":
            # The reviewer is the completion authority on the stop-hook path.
            # Tell the LoopEngine to end COMPLETED once this worker subprocess
            # exits — otherwise the evaluator (e.g. passthrough) would run and
            # force "continue", swallowing the verdict. Routed through the host
            # (which applies the active-session guard) so we no longer reach
            # into _single_loop directly.
            self._host.mark_loop_let_exit(mig_session_id)
            return None

        if decision.action == "wait_and_retry":
            # design.md §11.6 — flip session to WAITING_RETRY. LoopEngine
            # handles the sleep + resume in its run_loop (symmetric to manual
            # mode). Do NOT bump stop_block_counts: this is a controlled wait,
            # not a reviewer block. schedule_retry encapsulates the session
            # fetch + state flip + the "no session" warning; returns the new
            # retry_count (0 when the session was missing).
            count = self._host.schedule_retry(mig_session_id)
            if count:
                logger.info(
                    "stop_hook: wait_and_retry scheduled for %s (count=%d, rationale=%s)",
                    mig_session_id[:12],
                    count,
                    decision.rationale,
                )
            return None  # tell worker to exit

        self._host.bump_stop_block_count(block_key)

        session_obj = self._host.get_session(mig_session_id)
        if session_obj is not None and decision.feedback:
            await session_obj.inject_prompt(decision.feedback)
            logger.info(
                "stop_hook: injected reviewer feedback for %s (len=%d)",
                mig_session_id[:12],
                len(decision.feedback),
            )
        return {
            "decision": "block",
            "reason": decision.feedback,
        }

    async def _run_nl_question_review(
        self,
        block_key: str,
        mig_session_id: str,
        final_text: str,
        prior: int,
    ) -> dict[str, Any] | None:
        reviewer = self._host.reviewer_for(mig_session_id)
        if reviewer is None:
            # Defensive: _decide_stop guards `reviewer is None` before emitting
            # stop_requested, so this branch is unreachable today. Use an
            # explicit guard (not assert) so it survives `python -O` and
            # degrades gracefully (worker exits turn) if a future path emits
            # stop_requested without that guard. Mirrors _on_pre_tool_use /
            # _on_gate_question which already do this.
            return None
        decision = await reviewer.decide_nl_question(final_text)
        logger.info(
            "stop_hook: nl_question reviewer %s for %s — %s",
            decision.action,
            mig_session_id[:12],
            decision.rationale,
        )
        self._emitter.emit_async(
            mig_session_id,
            "stop_hook_nl_question",
            {
                "action": decision.action,
                "feedback": str(decision.feedback)[:300],
                "rationale": str(decision.rationale)[:300],
            },
        )
        if decision.action == "block_with_feedback":
            self._host.bump_stop_block_count(block_key)
            session_obj = self._host.get_session(mig_session_id)
            if session_obj is not None and decision.feedback:
                await session_obj.inject_prompt(decision.feedback)
            return {"decision": "block", "reason": decision.feedback}
        return None
