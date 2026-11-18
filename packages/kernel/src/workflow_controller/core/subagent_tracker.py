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

import logging
from typing import TYPE_CHECKING, Any

from workflow_controller.session.events import Event, EventType
from workflow_controller.session.session import SessionState

if TYPE_CHECKING:
    from collections.abc import Callable

    from workflow_controller.core.loop import SingleLoopEngine
    from workflow_controller.event.event_bus import EventBus
    from workflow_controller.session.manager import SessionPool

logger = logging.getLogger(__name__)


class SubagentTracker:
    """In-flight sub-agent tracking + SUBAGENT_* event emission.

    OverlayRegistry 风格（非 bus observer）：on_* 方法即 DriverHooks 的
    on_subagent_* callback —— 既维护在途状态又 bus.emit SUBAGENT_* 事件。
    make_driver_hooks 直接绑这些方法。WAITING_RETRY 精确唤醒经构造注入的
    loop_getter + pool 在 on_stop 内部触发（在途归零即提前唤醒，不等满
    _WAIT_RETRY_SECONDS）。reviewer 经 in_flight() 读在途状态。
    """

    def __init__(
        self,
        bus: EventBus,
        pool: SessionPool,
        loop_getter: Callable[[str], SingleLoopEngine | None],
    ) -> None:
        self._bus = bus
        self._pool = pool
        self._loop_getter = loop_getter
        # mig_session_id -> {agent_id: {"agent_type": str}} — in-flight sub-agents.
        self._status: dict[str, dict[str, dict[str, Any]]] = {}
        self._interrupted: dict[str, set[str]] = {}
        self._stopped: dict[str, set[str]] = {}
        self._native_agent_ids: dict[str, dict[str, str]] = {}

    @staticmethod
    def _stop_event_data(data: dict[str, Any], agent_id: str, existing_type: str) -> dict[str, Any]:
        """SUBAGENT_STOP 事件 payload：非通用类型沿用首次登记的 agent_type。"""
        content = data.get("content", "")
        total_tokens = int(data.get("total_tokens", 0) or 0)
        agent_type = data.get("agent_type", "")
        if existing_type and existing_type != "general-purpose":
            agent_type = existing_type
        stop_data = {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "total_tokens": total_tokens,
            "content": content,
            # Structured subagent message stream (opencode path, from
            # opencode.db part table). Each item is a normalized block
            # (text/thinking/tool_use/tool_result/step_start). The cc path
            # does not populate this yet — messages stays absent and the
            # panel falls back to the truncated content line.
            "messages": data.get("messages", []),
        }
        if data.get("status"):
            stop_data["status"] = data["status"]
        return stop_data

    async def on_start(self, mig_session_id: str, data: dict[str, Any]) -> None:
        """登记子代理并保留首个已确认的直接派发者。"""
        agent_id = str(data.get("agent_id") or "")
        agent_type = str(data.get("agent_type") or "")
        if (
            not agent_id
            or agent_id in self._stopped.get(mig_session_id, set())
            or agent_id in self._interrupted.get(mig_session_id, set())
        ):
            return
        native_parent_id = str(data.get("session_id") or data.get("native_session_id") or "")
        parent = str(data.get("parent_agent_id") or "") or self._native_agent_ids.get(
            mig_session_id, {}
        ).get(native_parent_id, "")
        existing = self._status.get(mig_session_id, {}).get(agent_id)
        if existing:
            current_type = existing.get("agent_type", "")
            known_parent = existing.get("parent_agent_id", "")
            type_is_final = current_type and current_type != "general-purpose"
            type_is_unchanged = not agent_type or agent_type == current_type
            parent_is_confirmed = not parent or bool(known_parent)
            if type_is_final and parent_is_confirmed:
                return
            if type_is_unchanged and parent_is_confirmed:
                return
            parent = known_parent or parent
            agent_type = current_type if type_is_final else agent_type or current_type
        self._status.setdefault(mig_session_id, {})[agent_id] = {
            "agent_type": agent_type,
            "parent_agent_id": parent,
        }
        self._native_agent_ids.setdefault(mig_session_id, {})[agent_id] = agent_id
        logger.info(
            "subagent start: %s (%s) for %s",
            agent_id[:12],
            agent_type,
            mig_session_id[:12],
        )
        await self._bus.emit(
            Event.create(
                session_id=mig_session_id,
                iteration=0,
                event_type=EventType.SUBAGENT_START,
                data={
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "parent_agent_id": parent,
                },
            )
        )

    async def on_progress(self, mig_session_id: str, data: dict[str, Any]) -> None:
        """Stream a sub-agent's incremental message blocks (opencode only).

        Called by the driver's background poller with new message blocks
        discovered since the last poll, so the panel shows subagent thinking
        in real time. Claude Code context sampling reads its transcript separately.
        """
        agent_id = data.get("agent_id", "")
        if not agent_id or agent_id in self._interrupted.get(mig_session_id, set()):
            return
        await self._bus.emit(
            Event.create(
                session_id=mig_session_id,
                iteration=0,
                event_type=EventType.SUBAGENT_PROGRESS,
                data={
                    "agent_id": agent_id,
                    "agent_type": data.get("agent_type", ""),
                    "messages": data.get("messages", []),
                },
            )
        )

    async def on_stop(self, mig_session_id: str, data: dict[str, Any]) -> None:
        """A sub-agent finished — drop it from in-flight tracking.

        Emits a SUBAGENT_STOP event with token/content; if the owning session
        is parked in WAITING_RETRY purely for sub-agents and none remain,
        wakes it now instead of waiting out the full retry window.
        """
        agent_id = data.get("agent_id", "")
        if agent_id in self._interrupted.get(
            mig_session_id, set()
        ) or agent_id in self._stopped.get(mig_session_id, set()):
            return
        self._stopped.setdefault(mig_session_id, set()).add(agent_id)
        existing_type = self._status.get(mig_session_id, {}).get(agent_id, {}).get("agent_type", "")
        self._status.get(mig_session_id, {}).pop(agent_id, None)
        remaining = len(self._status.get(mig_session_id, {}))
        logger.info(
            "subagent stop: %s for %s (in-flight=%d)",
            agent_id[:12],
            mig_session_id[:12],
            remaining,
        )
        # Emit a SUBAGENT_STOP event with `token total` and `content`.
        await self._bus.emit(
            Event.create(
                session_id=mig_session_id,
                iteration=0,
                event_type=EventType.SUBAGENT_STOP,
                data=self._stop_event_data(data, agent_id, existing_type),
            )
        )
        # Precise wake: if this session is waiting out a wait_and_retry window
        # purely for sub-agents and none remain, resume now instead of waiting
        # the full window.
        if remaining == 0:
            self._maybe_wake_waiting_retry(mig_session_id)

    def in_flight(self, mig_session_id: str) -> dict[str, dict[str, Any]]:
        """In-flight sub-agents for a session (reviewer reads this)."""
        return self._status.get(mig_session_id, {})

    def clear_session(self, mig_session_id: str) -> None:
        """Discard stale in-flight state before its owning query starts.

        清理时机在「下一轮 query 开始」而非「上轮 query 结束」：query 结束后到
        iteration 边界门禁之间仍可能有子代理在在途（wait_and_retry 在子代理在途
        时结束 query），breather 依赖 in_flight 判断是否推迟 handoff，故此处只在
        新一轮 query 起步时丢弃上轮残留。
        """
        self._status.pop(mig_session_id, None)
        self._stopped.pop(mig_session_id, None)
        self._interrupted.pop(mig_session_id, None)
        self._native_agent_ids.pop(mig_session_id, None)

    async def interrupt_all(
        self,
        mig_session_id: str,
        snapshot: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Retire children whose parent runtime was stopped before stop hooks ran."""
        agents = self._status.pop(mig_session_id, {})
        if snapshot is not None:
            agents = {**agents, **snapshot}
        # 去重：poller 被 cancel 时已补发过 interrupted stop（见 claude_cli
        # _poll_cli_subagents 的 finally），这些子代理已进 _stopped；此处再发
        # 会重复。同样跳过已标记 interrupted 的，保证幂等。
        stopped = self._stopped.get(mig_session_id, set())
        interrupted = self._interrupted.get(mig_session_id, set())
        for agent_id in list(agents):
            if agent_id in stopped or agent_id in interrupted:
                agents.pop(agent_id, None)
        self._interrupted.setdefault(mig_session_id, set()).update(agents)
        for agent_id, info in agents.items():
            await self._bus.emit(
                Event.create(
                    session_id=mig_session_id,
                    iteration=0,
                    event_type=EventType.SUBAGENT_STOP,
                    data={
                        "agent_id": agent_id,
                        "agent_type": info.get("agent_type", ""),
                        "status": "interrupted",
                        "total_tokens": 0,
                        "content": "交接时中断",
                    },
                )
            )

    def _maybe_wake_waiting_retry(self, mig_session_id: str) -> None:
        """在途归零时精确唤醒 WAITING_RETRY 中的 session（不等满重试窗口）。"""
        session_obj = self._pool.get(mig_session_id)
        loop = self._loop_getter(mig_session_id)
        session_waiting_retry = (
            session_obj is not None and session_obj.state == SessionState.WAITING_RETRY
        )
        loop_owns_session = False
        if session_waiting_retry and session_obj is not None and loop is not None:
            loop_owns_session = loop.is_current(session_obj)
        if session_obj is not None and loop is not None and loop_owns_session:
            logger.info(
                "subagent: all sub-agents done — waking WAITING_RETRY session %s",
                mig_session_id[:12],
            )
            loop.wake_retry()
