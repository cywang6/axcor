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

from typing import TYPE_CHECKING, Any

from workflow_controller.session.events import Event, EventType

if TYPE_CHECKING:
    from workflow_controller.event.event_bus import EventBus


class HookPayloadAdapter:
    """Inbound adapter: HookServer's neutral dict protocol -> domain Event -> bus.emit.

    HookServer._emit_event emits a payload-agnostic dict
    (``{"session_id","event_type","iteration","data"}``). This adapter parses
    it into a typed :class:`Event` and forwards it to the :class:`EventBus`,
    keeping HookServer a leaf that only knows aiohttp — it neither depends on
    EventBus nor understands domain event types.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def __call__(self, data: dict[str, Any]) -> None:
        await self._bus.emit(
            Event.create(
                session_id=data["session_id"],
                iteration=data.get("iteration", 0),
                event_type=EventType(data["event_type"]),
                data=data.get("data", {}),
            )
        )
