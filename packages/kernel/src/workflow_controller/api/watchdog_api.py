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

"""Watchdog API — 告警历史查询门面。

只读，返回纯数据结构；直读事件库筛 fault.* 事件并解码，不依赖检测本体。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflow_controller.watchdog.fault import decode_alerts

if TYPE_CHECKING:
    from workflow_controller.store.event_router import EventStoreRouter


class WatchdogAPI:
    """fault.* 告警历史查询的公开面。"""

    def __init__(self, event_router: EventStoreRouter) -> None:
        self._event_router = event_router

    def list_alerts(self, session_id: str) -> list[dict[str, Any]]:
        """按 session 查历史告警。"""
        events = self._event_router.get_events_sync(session_id)
        return [alert.to_dict() for alert in decode_alerts(events)]
