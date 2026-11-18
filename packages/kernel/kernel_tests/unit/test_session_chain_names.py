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

"""session chain (前序/后续) name 填充的回归测试。

场景：SessionAPI.list_sessions / get_session 返回的 SessionInfo 中，
predecessor.name 和 successors[].name 必须从 pool 中查到真实名称，
不能是空字符串。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from workflow_controller.api.session_api import SessionAPI
from workflow_controller.config.settings import AppConfig, SessionConfig
from workflow_controller.session.session import Session, SessionState


def _session(sid: str, name: str, role: str = "worker") -> Session:
    cfg = SessionConfig(name=name, role=role, cwd="/tmp", initial_prompt="test")
    s = Session(name=name, role=role, config=cfg)
    s.id = sid
    s.state = SessionState.IDLE
    return s


def _orch_with_chain(sessions, predecessors, successors_map):
    """Mock orchestrator: pool 返回 sessions，event_router 返回 chain 数据。"""
    pool = MagicMock()
    pool.list_sessions.return_value = sessions
    pool.get = MagicMock(side_effect=lambda sid: next((s for s in sessions if s.id == sid), None))

    er = MagicMock()
    er.get_events_sync = MagicMock(return_value=[])
    er.get_predecessor_sync = MagicMock(side_effect=lambda sid: predecessors.get(sid))
    er.get_successors_sync = MagicMock(side_effect=lambda sid: successors_map.get(sid, []))

    orch = MagicMock()
    orch.pool = pool
    orch.event_router = er
    orch.config = AppConfig()
    return orch


def test_predecessor_name_populated():
    """predecessor.name 应该是对应 session 的真实名称，不是空字符串。"""
    worker = _session("w1", "worker-alpha")
    handoff_agent = _session("h1", "worker-alpha-handoff-1", role="handoff_agent")

    sessions = [worker, handoff_agent]
    predecessors = {
        "w1": {"from_session_id": "h1", "handoff_doc": None, "created_at": "2026-01-01"},
    }
    successors_map = {}

    orch = _orch_with_chain(sessions, predecessors, successors_map)
    api = SessionAPI(orch, None)
    result = api.list_sessions()

    w1_info = next(s for s in result if s.id == "w1")
    assert w1_info.predecessor is not None
    assert w1_info.predecessor["id"] == "h1"
    assert w1_info.predecessor["name"] == "worker-alpha-handoff-1"


def test_successor_name_populated():
    """successors[].name 应该是对应 session 的真实名称，不是空字符串。"""
    worker = _session("w1", "worker-alpha")
    continuation = _session("w2", "worker-alpha-continue-1")

    sessions = [worker, continuation]
    predecessors = {}
    successors_map = {
        "w1": [{"to_session_id": "w2", "handoff_doc": None, "created_at": "2026-01-01"}],
    }

    orch = _orch_with_chain(sessions, predecessors, successors_map)
    api = SessionAPI(orch, None)
    result = api.list_sessions()

    w1_info = next(s for s in result if s.id == "w1")
    assert len(w1_info.successors) == 1
    assert w1_info.successors[0]["id"] == "w2"
    assert w1_info.successors[0]["name"] == "worker-alpha-continue-1"


def test_chain_name_falls_back_to_id_prefix():
    """关联 session 不在 pool 中时，name 回退到 id 前 12 位。"""
    worker = _session("w1", "worker-alpha")

    sessions = [worker]
    predecessors = {
        "w1": {
            "from_session_id": "deadbeef12345678",
            "handoff_doc": None,
            "created_at": "2026-01-01",
        },
    }
    successors_map = {}

    orch = _orch_with_chain(sessions, predecessors, successors_map)
    api = SessionAPI(orch, None)
    result = api.list_sessions()

    w1_info = next(s for s in result if s.id == "w1")
    assert w1_info.predecessor is not None
    assert w1_info.predecessor["id"] == "deadbeef12345678"
    assert w1_info.predecessor["name"] == "deadbeef1234"


def test_get_session_also_populates_chain():
    """get_session (单 session 查询) 也填充 predecessor/successors。"""
    worker = _session("w1", "worker-alpha")
    handoff_agent = _session("h1", "worker-alpha-handoff-1", role="handoff_agent")
    continuation = _session("w2", "worker-alpha-continue-1")

    sessions = [worker, handoff_agent, continuation]
    predecessors = {
        "w1": {"from_session_id": "h1", "handoff_doc": None, "created_at": "2026-01-01"},
    }
    successors_map = {
        "w1": [{"to_session_id": "w2", "handoff_doc": None, "created_at": "2026-01-01"}],
    }

    orch = _orch_with_chain(sessions, predecessors, successors_map)
    api = SessionAPI(orch, None)
    info = api.get_session("w1")

    assert info is not None
    assert info.predecessor["name"] == "worker-alpha-handoff-1"
    assert info.successors[0]["name"] == "worker-alpha-continue-1"
