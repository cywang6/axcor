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

"""Plugin-facing contract for observing file-changing tool calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from workflow_controller.session.events import Event, EventType

ActorKind = Literal["agent", "automation"]
TOOL_WILL_EXECUTE = EventType.TOOL_WILL_EXECUTE
TOOL_EXECUTION_FINISHED = EventType.TOOL_EXECUTION_FINISHED
TOOL_EXECUTION_ABORTED = EventType.TOOL_EXECUTION_ABORTED


def _aware_iso(raw: str) -> datetime:
    """历史事件携带 naive 本地时间串，按本地时区补全；全量 aware 写入一个版本后可删除。"""
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


@dataclass(frozen=True)
class FileChangeContext:
    capture_id: str

    actor_kind: ActorKind

    workflow_controller_session_id: str | None

    native_session_id: str | None

    parent_native_session_id: str | None

    runtime: str | None

    model: str | None

    agent_name: str | None

    tool_name: str

    tool_use_id: str

    cwd: Path

    tool_input: dict[str, Any]

    started_at: datetime

    attributes: dict[str, str] = field(default_factory=dict)

    native_agent_id: str | None = None

    # run 归属维度：automation 捕获以 run 键落账，并发 overlap 按同 run 免冲突；
    # run_context 与 run 平面 context（驱动分派键，如 node=X）同值对账
    run_id: str | None = None

    run_context: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChangeContext:
        return cls(
            capture_id=str(data["capture_id"]),
            actor_kind=cast("ActorKind", data["actor_kind"]),
            workflow_controller_session_id=data.get("workflow_controller_session_id"),
            native_session_id=data.get("native_session_id"),
            parent_native_session_id=data.get("parent_native_session_id"),
            runtime=data.get("runtime"),
            model=data.get("model"),
            agent_name=data.get("agent_name"),
            tool_name=str(data["tool_name"]),
            tool_use_id=str(data["tool_use_id"]),
            cwd=Path(str(data["cwd"])),
            tool_input=dict(data.get("tool_input") or {}),
            started_at=_aware_iso(str(data["started_at"])),
            attributes=dict(data.get("attributes") or {}),
            native_agent_id=data.get("native_agent_id"),
            run_id=data.get("run_id"),
            run_context=data.get("run_context"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "actor_kind": self.actor_kind,
            "workflow_controller_session_id": self.workflow_controller_session_id,
            "native_session_id": self.native_session_id,
            "parent_native_session_id": self.parent_native_session_id,
            "runtime": self.runtime,
            "model": self.model,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "cwd": str(self.cwd),
            "tool_input": self.tool_input,
            "started_at": self.started_at.isoformat(),
            "attributes": self.attributes,
            "native_agent_id": self.native_agent_id,
            "run_id": self.run_id,
            "run_context": self.run_context,
        }


@dataclass(frozen=True)
class FileChangeOutcome:
    succeeded: bool

    is_error: bool

    tool_output: Any

    path_hints: tuple[Path, ...]

    finished_at: datetime

    incomplete: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChangeOutcome:
        return cls(
            succeeded=bool(data.get("succeeded", False)),
            is_error=bool(data.get("is_error", False)),
            tool_output=data.get("tool_output"),
            path_hints=tuple(Path(str(path)) for path in data.get("path_hints") or ()),
            finished_at=_aware_iso(str(data["finished_at"])),
            incomplete=bool(data.get("incomplete", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "is_error": self.is_error,
            "tool_output": self.tool_output,
            "path_hints": [str(path) for path in self.path_hints],
            "finished_at": self.finished_at.isoformat(),
            "incomplete": self.incomplete,
        }


def tool_will_execute_event(context: FileChangeContext) -> Event:
    return _capture_lifecycle_event(context, TOOL_WILL_EXECUTE)


def tool_execution_outcome_event(
    context: FileChangeContext,
    outcome: FileChangeOutcome,
) -> Event:
    event_type = TOOL_EXECUTION_ABORTED if outcome.incomplete else TOOL_EXECUTION_FINISHED
    return _capture_lifecycle_event(context, event_type, {"outcome": outcome.to_dict()})


def _capture_lifecycle_event(
    context: FileChangeContext,
    event_type: EventType,
    extra_data: dict[str, Any] | None = None,
) -> Event:
    """按捕获身份构造事件：有 session 身份走 session 键，否则以 run 键承载。"""
    data: dict[str, Any] = {"context": context.to_dict()}
    if extra_data:
        data.update(extra_data)
    session_id = context.workflow_controller_session_id or context.native_session_id
    event = Event.create(
        session_id=session_id or "",
        iteration=0,
        event_type=event_type,
        data=data,
    )
    if not session_id:
        # 无 session 身份的捕获（automation 进程面）：run 维度只能随上下文
        # 带入；session 键事件的 run 维度由写库路径打戳器统一补
        event.run_id = context.run_id
        event.context = context.run_context
    return event


BeforeHandler = Callable[[FileChangeContext], Awaitable[None]]
AfterHandler = Callable[[FileChangeContext, FileChangeOutcome], Awaitable[None]]


class FileChangeCapture(Protocol):
    @property
    def has_subscribers(self) -> bool:
        raise NotImplementedError

    def subscribe(self, before: BeforeHandler, after: AfterHandler) -> int:
        raise NotImplementedError

    def unsubscribe(self, token: int) -> None:
        raise NotImplementedError

    async def before(self, context: FileChangeContext) -> None:
        raise NotImplementedError

    async def after(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> None:
        raise NotImplementedError
