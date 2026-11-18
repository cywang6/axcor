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

"""Event API — 事件增量查询。

提供基于 rowid 的增量查询，供 SSE 推送和 Web/Headless 拉取使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_controller.store.event_router import EventStoreRouter


@dataclass
class EventInfo:
    """事件纯数据结构。"""

    rowid: int
    session_id: str
    event_type: str
    iteration: int
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    context: str | None = None

    @classmethod
    def from_event(cls, rowid: int, event: object) -> EventInfo:
        event_type = event.event_type  # type: ignore[attr-defined]
        timestamp = event.timestamp  # type: ignore[attr-defined]
        return cls(
            rowid=rowid,
            session_id=event.session_id,  # type: ignore[attr-defined]
            event_type=event_type.value if hasattr(event_type, "value") else str(event_type),
            iteration=event.iteration,  # type: ignore[attr-defined]
            timestamp=timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
            data=event.data if isinstance(event.data, dict) else {},  # type: ignore[attr-defined]
            run_id=getattr(event, "run_id", None),
            context=getattr(event, "context", None),
        )


class EventAPI:
    """事件增量查询 — get_since(last_rowid) 只返回新事件。"""

    def __init__(self, event_router: EventStoreRouter) -> None:
        self._store = event_router

    def get_since(self, last_ts: str, session_id: str = "") -> tuple[str, list[EventInfo]]:
        """增量查 timestamp > last_ts 的事件。session_id 非空时只查该 session 所在库。"""
        new_ts, events = self._store.get_events_since(last_ts, session_id=session_id)
        return new_ts, [EventInfo.from_event(0, e) for e in events]

    def get_recent(self, limit: int = 50) -> list[EventInfo]:
        """跨库最近 N 条（SSE 重连回放用）。"""
        events = self._store.get_recent(limit)
        return [EventInfo.from_event(0, e) for e in events]

    def snapshot_cursor(self) -> str:
        return self._store.snapshot_event_cursor()

    def snapshot_run_event_cursor(self) -> str:
        return self._store.snapshot_run_event_cursor()

    def get_run_events_since_cursor(self, cursor: str) -> tuple[str, list[dict[str, Any]]]:
        """run 键事件增量（run_events 表，SSE 推送面）。"""
        return self._store.get_run_events_since_cursor(cursor)

    def get_recent_at_cursor(self, cursor: str, limit: int = 50) -> list[EventInfo]:
        events = self._store.get_recent_at_cursor(cursor, limit)
        return [EventInfo.from_event(0, event) for event in events]

    def get_since_cursor(
        self,
        cursor: str,
        session_id: str = "",
    ) -> tuple[str, list[EventInfo]]:
        new_cursor, events = self._store.get_events_since_cursor(
            cursor,
            session_id=session_id,
        )
        return new_cursor, [EventInfo.from_event(0, event) for event in events]

    async def get_handoff_chain(self, session_id: str) -> dict[str, Any]:
        """交接谱系查询（前驱与后继链）。"""
        return await self._store.get_handoff_chain(session_id)
