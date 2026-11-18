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

"""谱系继承回归：交接/接续成员入父 session 的同一 run。"""

from __future__ import annotations

import pytest

from workflow_controller.config.settings import (
    AppConfig,
    HookServerConfig,
    ReviewerConfig,
    SessionConfig,
)
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.plugins.breather.handoff import HandoffGenerator

from ._helpers import breather_plugin


@pytest.fixture
def orch(tmp_path):
    """起不动 HookServer 的 Orchestrator（run 账本可用，无端口占用）。"""
    cfg = AppConfig(
        project_dir=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )
    return Orchestrator(cfg, default_project_dir=tmp_path)


def _fake_activate(orch, monkeypatch):
    """Mock _activate_session，但保留显式归属经激活缝挂靠的约定。"""

    async def fake(session, *, prepare=None):
        if session.config.run_id is not None and getattr(orch, "_post_attach_hook") is not None:
            await getattr(orch, "_post_attach_hook")(session)

    monkeypatch.setattr(orch, "_activate_session", fake)


async def test_handoff_sessions_join_workers_run(orch, tmp_path, monkeypatch):
    """breather 交接场景：handoff agent 与接续 worker 都入 worker 的 run。"""
    _fake_activate(orch, monkeypatch)

    async def fake_wait(sid, timeout=None):
        orch.pool.get(sid).result = "success"
        return True

    monkeypatch.setattr(orch, "wait_session_done", fake_wait)
    worker = await orch.start_session_with_config(SessionConfig(name="w1", cwd=str(tmp_path)))
    run_id = worker.run_id  # 启动点铸的隐式 run

    plugin = breather_plugin(orch)
    result = await plugin.execute_handoff(
        worker.id,
        SessionConfig(name="handoff-1", role="handoff_agent", cwd=str(tmp_path)),
        SessionConfig(name="w2", cwd=str(tmp_path)),
    )
    assert result["success"] is True
    session_ids = await orch.run_store.list_session_ids(run_id)
    assert set(session_ids) == {
        worker.id,
        result["handoff_session_id"],
        result["new_worker_id"],
    }


async def test_handoff_config_build_carries_run_id(tmp_path):
    """build_new_session_config 透传归属——接续 config 不再静默丢 run_id。"""
    gen = HandoffGenerator("op", "handoff.md")
    old = SessionConfig(name="w1", run_id="run-xyz")
    new = gen.build_new_session_config(
        _SessionShim(old),
        tmp_path / "handoff.md",
        "w2",
    )
    assert new.run_id == "run-xyz"


class _SessionShim:
    """build_new_session_config 只读 config 面——用最小 shim 提供。"""

    def __init__(self, config):
        self.config = config
