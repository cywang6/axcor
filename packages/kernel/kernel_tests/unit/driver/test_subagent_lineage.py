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

from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.core.subagent_tracker import SubagentTracker


@pytest.mark.asyncio
async def test_late_parent_is_kept() -> None:
    """后到父级会补充启动事件，后续空值不会覆盖。"""
    bus = MagicMock()
    bus.emit = AsyncMock()
    tracker = SubagentTracker(bus, MagicMock(), lambda _session_id: None)

    await tracker.on_start("session", {"agent_id": "explore", "agent_type": "Explore"})
    await tracker.on_start(
        "session",
        {"agent_id": "explore", "agent_type": "Explore", "parent_agent_id": "designer"},
    )
    await tracker.on_start("session", {"agent_id": "explore", "agent_type": "Explore"})

    events = [call.args[0] for call in bus.emit.await_args_list]
    assert len(events) == 2
    assert events[-1].data["parent_agent_id"] == "designer"


@pytest.mark.asyncio
async def test_codex_nested_thread_uses_parent_route(tmp_path, monkeypatch) -> None:
    """Codex 嵌套线程的 start 和 stop 都路由到原会话。"""
    orchestrator = Orchestrator.create_default(config_dir=tmp_path)
    emitted = []

    async def capture(event) -> None:
        """记录测试中的事件。"""
        emitted.append(event)

    monkeypatch.setattr(orchestrator.bus, "emit", capture)
    getattr(orchestrator, "_cli_to_workflow_controller_session")["codex-main"] = "session"

    await getattr(orchestrator, "_handle_subagent_start_request")(
        "codex-main", {"agent_id": "designer", "agent_type": "designer"}
    )
    await getattr(orchestrator, "_handle_subagent_start_request")(
        "designer", {"agent_id": "explore", "agent_type": "Explore"}
    )
    await getattr(orchestrator, "_handle_subagent_stop_request")(
        "designer", {"agent_id": "explore", "agent_type": "Explore"}
    )

    assert [event.event_type.value for event in emitted] == [
        "subagent_start",
        "subagent_start",
        "subagent_stop",
    ]
    assert emitted[1].session_id == "session"
    assert emitted[1].data["parent_agent_id"] == "designer"
