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

"""Event 序列化对称性 — 枚举与开放词表字符串事件类型均可往返。"""

from __future__ import annotations

from workflow_controller.session.events import Event, EventType


def test_enum_event_type_roundtrips() -> None:
    event = Event.create("s1", 0, EventType.NOTIFICATION, {"k": "v"})

    restored = Event.from_dict(event.to_dict())

    assert restored.event_type is EventType.NOTIFICATION


def test_string_event_type_roundtrips() -> None:
    for et in ("fault.subagent_timeout", "stop_requested"):
        event = Event.create("s1", 0, et, {})

        restored = Event.from_dict(event.to_dict())

        assert restored.event_type == et
