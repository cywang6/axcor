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

"""LimitDetector — consumes RATE_LIMIT events (driver-emitted limit_signal)
and dispatches to the AuthRotator. ERROR events are observability-only and
are not handled here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from workflow_controller.core.auth_rotator.signal import LimitMatch
from workflow_controller.session.events import Event, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from workflow_controller.core.auth_rotator import AuthRotator
    from workflow_controller.event.event_bus import EventBus

logger = logging.getLogger(__name__)

_UTILIZATION_WARN_THRESHOLD = 0.7  # Matches claude CLI's internal threshold


class LimitDetector:
    def __init__(
        self,
        rotator: AuthRotator,
        *,
        bus: EventBus | None = None,
        session_handler: Callable[[Event], Awaitable[bool]] | None = None,
    ) -> None:
        self._rotator = rotator
        self._session_handler = session_handler
        if bus is not None:
            bus.observe(EventType.RATE_LIMIT, self._handle_signal)

    async def _handle_signal(self, event: Event) -> None:
        signal = event.data.get("limit_signal") or {}
        kind = signal.get("kind", "unknown")
        scope = signal.get("scope")
        reset_iso = signal.get("reset_at")
        reset_dt = datetime.fromisoformat(reset_iso) if reset_iso else None
        if reset_dt is not None and reset_dt.tzinfo is None:
            # 历史事件携带 naive 本地时间串，按本地时区补全；全量 aware 写入一个版本后可删除
            reset_dt = reset_dt.astimezone()
        utilization = signal.get("utilization")
        source = signal.get("source", "")

        if kind == "allowed":
            return  # high frequency event — every request — ignore silently

        if kind == "warning":
            if utilization is None or utilization < _UTILIZATION_WARN_THRESHOLD:
                return
            self._rotator.on_quota_warning(
                utilization=float(utilization),
                scope=scope or "unknown",
                reset_at=reset_dt,
            )
            return

        if kind == "rejected":
            if self._session_handler is not None and await self._session_handler(event):
                return
            self._rotator.on_rate_limit(scope=scope or "5h", reset_at=reset_dt)
            return

        if kind == "auth_error":
            if self._session_handler is not None and await self._session_handler(event):
                return
            self._rotator.on_limit_detected(
                LimitMatch(scope="auth_error", source=source or "unknown"),
                reset_at=None,
            )
            return

        # Unknown kind (Anthropic added a new status we don't recognize, or a
        # driver emitted a malformed signal). Don't switch (safer than guessing).
        logger.warning("limit_signal: unknown kind=%r source=%r", kind, source)
