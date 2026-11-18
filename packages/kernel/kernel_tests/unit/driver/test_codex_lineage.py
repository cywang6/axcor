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

from workflow_controller.driver.codex_cli import _codex_subagent_starts


def _thread_started(thread_id: str) -> dict:
    """构造原生线程启动事件。"""
    return {"type": "thread.started", "thread_id": thread_id}


def _spawn_completed(sender_id: str, receiver_id: str) -> dict:
    """构造原生代理派发完成事件。"""
    return {
        "type": "item.completed",
        "item": {
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": sender_id,
            "receiver_thread_ids": [receiver_id],
        },
    }


def test_collab_events_restore_nested_parent() -> None:
    """原生 collab 事件把 explorer 挂到 designer。"""
    agent_by_thread = {}
    assert _codex_subagent_starts(_thread_started("main"), agent_by_thread) == []
    designer = _codex_subagent_starts(_spawn_completed("main", "designer"), agent_by_thread)[0]
    assert _codex_subagent_starts(_thread_started("designer"), agent_by_thread) == []
    explorer = _codex_subagent_starts(_spawn_completed("designer", "explorer"), agent_by_thread)[0]

    assert designer["parent_agent_id"] == ""
    assert explorer["parent_agent_id"] == "designer"
