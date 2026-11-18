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

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class EventType(Enum):
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TOOL_CALL = "tool_call"
    TOOL_WILL_EXECUTE = "tool_will_execute"
    TOOL_EXECUTION_FINISHED = "tool_execution_finished"
    TOOL_EXECUTION_ABORTED = "tool_execution_aborted"
    ASSISTANT_MESSAGE = "assistant_message"
    NOTIFICATION = "notification"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    USER_MESSAGE = "user_message"
    STEP_FINISH = "step_finish"
    CONTEXT_USAGE = "context_usage"
    ERROR = "error"
    RATE_LIMIT = "rate_limit_event"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_PROGRESS = "subagent_progress"
    SUBAGENT_CONTEXT = "subagent_context"
    MAIN_CONTEXT = "main_context"
    SUBAGENT_STOP = "subagent_stop"
    # user registered a prompt overlay (per-subagent-type or global)
    PROMPT_OVERLAY_REGISTERED = "prompt_overlay_registered"
    # user cleared a prompt overlay (by index or all)
    PROMPT_OVERLAY_CLEARED = "prompt_overlay_cleared"
    LOG_AUTO = "log_auto"
    LOG_QUERY = "log_query"
    LOG_EVAL = "log_eval"
    LOG_LOOP = "log_loop"
    LOG_MANUAL = "log_manual"
    # session 生命周期事件
    SESSION_STATE_CHANGED = "session_state_changed"
    SESSION_OUTCOME = "session_outcome"
    SESSION_ACTIVATED = "session_activated"


@dataclass
class Event:
    id: str

    session_id: str

    iteration: int

    timestamp: datetime

    # 枚举外的开放词表事件（如 fault.*、stop_requested）以字符串承载
    event_type: EventType | str

    data: dict[str, Any] = field(default_factory=dict)

    # run 维度打戳：写库路径经归属缓存填充，读取路径随行带出
    run_id: str | None = None

    context: str | None = None

    @staticmethod
    def create(
        session_id: str,
        iteration: int,
        event_type: EventType | str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        return Event(
            id=uuid4().hex,
            session_id=session_id,
            iteration=iteration,
            timestamp=datetime.now(tz=UTC),
            event_type=event_type,
            data=data or {},
        )

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Event:
        et_str = d["event_type"]
        if et_str == "result":
            et = EventType.STEP_FINISH
        elif isinstance(et_str, EventType):
            et = et_str
        else:
            try:
                et = EventType(et_str)
            except ValueError:
                et = et_str
        return Event(
            id=d["id"],
            session_id=d["session_id"],
            iteration=d["iteration"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            event_type=et,
            data=d.get("data", {}),
            run_id=d.get("run_id"),
            context=d.get("context"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "iteration": self.iteration,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value
            if hasattr(self.event_type, "value")
            else self.event_type,
            "data": self.data,
            "run_id": self.run_id,
            "context": self.context,
        }


def extract_tool_arg(input_dict: dict[str, Any] | None) -> str:
    """从工具 input 中提取最有代表性的参数摘要。

    优先取 file_path / command / pattern / query / url，
    兜底用 JSON 序列化前 80 字符。
    """
    if not input_dict:
        return ""
    for key in ("file_path", "command", "pattern", "query", "url"):
        if key in input_dict:
            return str(input_dict[key])
    return json.dumps(input_dict, ensure_ascii=False)


COMPLETED_STOP_REASONS = frozenset({"reviewer_complete", "evaluator_complete", "max_iterations"})
FAILED_STOP_REASONS = frozenset({"error", "gate_failed", "auth_error", "timeout", "user_cancel"})
PAUSED_STOP_REASONS = frozenset({"user_pause"})


def outcome_kind_for_reason(reason: str | None) -> str:
    """停止意图 → 结局 kind；词表外细因（自定义停止原因）归 stopped。

    注意入参是停止「意图」（如 user_stop/auth_error），不是 Session.stop
    改写后的 stop_reason——kind 消歧不传播该历史映射。
    """
    if reason in COMPLETED_STOP_REASONS:
        return "completed"
    if reason in FAILED_STOP_REASONS:
        return "failed"
    if reason in PAUSED_STOP_REASONS:
        return "paused"
    return "stopped"


@dataclass(frozen=True)
class SessionOutcome:
    """session 的结构化结局——session_outcome 终态事件载荷。

    同一 session 的多条结局即多个阶段事实（暂停后停止、多次尝试），
    消费端按需取舍；context 为 run 归属上下文（驱动分派键）。
    """

    session_id: str
    kind: str  # completed / paused / stopped / failed
    stop_reason: str | None
    last_error: str | None
    result_digest: str | None  # 末条 assistant 文本
    iteration: int
    context: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "result_digest": self.result_digest,
            "iteration": self.iteration,
            "context": self.context,
        }


def extract_tool_error(content: object) -> str:
    """从 tool_result 的 content 中提取失败原因文本（直接取原始字符串）。

    不做额外解析/截断——长内容的折叠交给前端（与 user message 一致）。
    仅当 tool_call 事件的 is_error=true 时由各 driver / hook server 调用填充。
    """
    if isinstance(content, str):
        return content.strip()
    return ""
