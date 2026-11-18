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

"""Synchronous, fail-open file-change capture seam shared by all runtimes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.file_changes import (
    TOOL_EXECUTION_ABORTED,
    TOOL_EXECUTION_FINISHED,
    TOOL_WILL_EXECUTE,
    AfterHandler,
    BeforeHandler,
    FileChangeContext,
    FileChangeOutcome,
)

if TYPE_CHECKING:
    from workflow_controller.event.event_bus import EventBus
    from workflow_controller.session.events import Event

logger = logging.getLogger(__name__)


def _same_run(left: FileChangeContext, right: FileChangeContext) -> bool:
    # run_id 为 None 表示无归属，双方均无归属不算同 run，仍按冲突判定
    return left.run_id is not None and left.run_id == right.run_id


class FileChangeRegistry:
    """Awaited pre/post callbacks; a broken observer never breaks a tool call."""

    def __init__(self) -> None:
        self._subscribers: dict[int, tuple[BeforeHandler, AfterHandler]] = {}
        self._inflight: dict[
            str, tuple[FileChangeContext, tuple[tuple[BeforeHandler, AfterHandler], ...]]
        ] = {}
        self._closing: set[str] = set()
        self._next_token = 1
        self._lock = asyncio.Lock()
        self._bound = False

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    @staticmethod
    async def _complete_after(
        context: FileChangeContext,
        outcome: FileChangeOutcome,
        subscribers: tuple[tuple[BeforeHandler, AfterHandler], ...],
    ) -> None:
        for _, after in subscribers:
            try:
                await after(context, outcome)
            except Exception:
                logger.exception("file-change after capture failed: %s", context.capture_id)

    def bind(self, bus: EventBus) -> None:
        if self._bound:
            return
        bus.observe_awaited(TOOL_WILL_EXECUTE, self._on_before_event)
        bus.observe_awaited(TOOL_EXECUTION_FINISHED, self._on_after_event)
        bus.observe_awaited(TOOL_EXECUTION_ABORTED, self._on_after_event)
        self._bound = True

    def subscribe(self, before: BeforeHandler, after: AfterHandler) -> int:
        token = self._next_token
        self._next_token += 1
        self._subscribers[token] = (before, after)
        return token

    def unsubscribe(self, token: int) -> None:
        self._subscribers.pop(token, None)

    async def before(self, context: FileChangeContext) -> None:
        await self._dispatch_before(context)

    async def after(self, context: FileChangeContext, outcome: FileChangeOutcome) -> None:
        await self._dispatch_after(context, outcome)

    async def _on_before_event(self, event: Event) -> None:
        raw_context = event.data.get("context")
        if not isinstance(raw_context, dict):
            logger.warning("tool_will_execute event missing file-change context")
            return
        await self.before(FileChangeContext.from_dict(raw_context))

    async def _on_after_event(self, event: Event) -> None:
        raw_context = event.data.get("context")
        raw_outcome = event.data.get("outcome")
        if not isinstance(raw_context, dict) or not isinstance(raw_outcome, dict):
            logger.warning("tool completion event missing file-change payload")
            return
        await self.after(
            FileChangeContext.from_dict(raw_context),
            FileChangeOutcome.from_dict(raw_outcome),
        )

    async def _dispatch_before(self, context: FileChangeContext) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.values())
            overlapping = tuple(self._inflight.values())
            if subscribers:
                self._inflight[context.capture_id] = (context, subscribers)
        # overlap 只对跨 run / 无归属的并发捕获生效：同 run 的并发捕获是
        # run 编排内的协调行为，归因保留各自 actor（session），不互标冲突
        overlapping = tuple(
            item
            for item in overlapping
            if item[0].cwd == context.cwd and not _same_run(item[0], context)
        )
        if overlapping:
            context.attributes["file_change_overlap"] = "true"
            for active_context, _ in overlapping:
                active_context.attributes["file_change_overlap"] = "true"
        started: list[tuple[BeforeHandler, AfterHandler]] = []
        for before, after in subscribers:
            try:
                await before(context)
                started.append((before, after))
            except asyncio.CancelledError:
                async with self._lock:
                    self._inflight.pop(context.capture_id, None)
                if started:
                    await self._finish_cancelled_capture(context, started)
                raise
            except Exception:
                logger.exception("file-change before capture failed: %s", context.capture_id)
        if started:
            async with self._lock:
                self._inflight[context.capture_id] = (context, tuple(started))
        else:
            async with self._lock:
                self._inflight.pop(context.capture_id, None)

    async def _finish_cancelled_capture(
        self,
        context: FileChangeContext,
        started: list[tuple[BeforeHandler, AfterHandler]],
    ) -> None:
        """取消路径收尾：为已启动的 before 订阅者补发失败 after（等 shield
        任务跑完再返回，调用方随后重抛取消）。
        """
        cleanup = FileChangeOutcome(
            succeeded=False,
            is_error=True,
            tool_output={"reason": "pre-tool capture cancelled"},
            path_hints=(),
            finished_at=context.started_at,
            incomplete=True,
        )
        task = asyncio.create_task(self._complete_after(context, cleanup, tuple(started)))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()

    async def _dispatch_after(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> None:
        async with self._lock:
            if context.capture_id in self._closing:
                return
            inflight = self._inflight.get(context.capture_id)
            if inflight is not None:
                self._closing.add(context.capture_id)
        subscribers = inflight[1] if inflight is not None else ()
        if not subscribers:
            return
        if inflight is not None:
            context = inflight[0]
        task = asyncio.create_task(
            self._complete_after(context, outcome, subscribers),
            name=f"file-change-after-{context.capture_id}",
        )
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        finally:
            async with self._lock:
                self._inflight.pop(context.capture_id, None)
                self._closing.discard(context.capture_id)
        if cancelled:
            raise asyncio.CancelledError


MUTATING_TOOL_NAMES = frozenset(
    {
        "edit",
        "write",
        "bash",
        "shell",
        "multiedit",
        "notebookedit",
        "patch",
        "applypatch",
    }
)


def is_mutating_tool(name: str) -> bool:
    return name.replace("_", "").replace("-", "").lower() in MUTATING_TOOL_NAMES


def path_hints(tool_input: dict[str, Any], cwd: Path) -> tuple[Path, ...]:
    hints: list[Path] = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            path = Path(value).expanduser()
            hints.append(path if path.is_absolute() else cwd / path)
    return tuple(hints)
