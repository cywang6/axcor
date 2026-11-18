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

from dataclasses import dataclass
from typing import Any


@dataclass
class EventTypeDef:
    name: str
    description: str = ""
    schema: dict[str, Any] | None = None


_DEFAULT_TYPE_NAMES = [
    "tool_use",
    "tool_result",
    "tool_call",
    "assistant_message",
    "notification",
    "iteration_start",
    "iteration_end",
    "user_message",
    "result",
    "context_usage",
    "error",
    "rate_limit_event",
    "subagent_start",
    "subagent_progress",
    "subagent_stop",
    "prompt_overlay_registered",
    "prompt_overlay_cleared",
    "log_auto",
    "log_query",
    "log_eval",
    "log_loop",
    "log_manual",
    "stop_requested",
    "pre_tool_use",
    "iteration_boundary",
    "gate_question",
    "tool_will_execute",
    "tool_execution_finished",
    "tool_execution_aborted",
    "session_state_changed",
    "session_outcome",
    "session_activated",
]


def _default_type_defs() -> list[EventTypeDef]:
    return [EventTypeDef(name=n) for n in _DEFAULT_TYPE_NAMES]


class EventTypeRegistry:
    def __init__(self) -> None:
        self._types: dict[str, EventTypeDef] = {}
        for td in _default_type_defs():
            self._types[td.name] = td

    def register(self, td: EventTypeDef) -> None:
        if td.name in self._types:
            raise ValueError(f"Event type '{td.name}' already registered")
        self._types[td.name] = td

    def get(self, name: str) -> EventTypeDef | None:
        return self._types.get(name)

    def all(self) -> list[EventTypeDef]:
        return list(self._types.values())
