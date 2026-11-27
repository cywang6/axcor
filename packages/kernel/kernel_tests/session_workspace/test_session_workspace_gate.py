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

"""编排器侧的单会话工作区门控测试：开关、归属、屏障接线零变化，以及驱动形态的位置调用经接线到达宿主。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from workflow_controller.config.settings import SessionConfig, SessionTracebackConfig
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.core.runs import RunKind, RunStatus
from workflow_controller.driver.claude_sdk import _sdk_pre_tool_cb, _sdk_subagent_stop_cb
from workflow_controller.driver.hooks import BarrierProceed

if TYPE_CHECKING:
    from workflow_controller.config.settings import AppConfig

pytestmark = pytest.mark.asyncio


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    """测试用最小应用配置（复用钩子处理器测试的构造形态）。"""
    from workflow_controller.config.settings import (
        AppConfig,
        HookServerConfig,
        ReviewerConfig,
    )

    return AppConfig(
        project_dir=str(tmp_path),
        harmony_output_path=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )


async def _make_session(orch: Orchestrator, *, traceback: SessionTracebackConfig | None):
    """池内建一个会话并返回（隐式归属随创建铸造）。"""
    config = SessionConfig(name="s1", cwd=".", traceback=traceback)
    return await orch.pool.create(config)


async def test_untraced_session_gate_off_and_hooks_unchanged(tmp_path, base_config):
    """未开启 traceback：门控关闭、屏障回调不接线（行为零变化）。"""
    orch = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    try:
        session = await _make_session(orch, traceback=None)

        assert await orch._session_workspace_enabled(session) is False
        hooks = orch.make_driver_hooks(session.id)
        assert hooks.before_side_effect_tool is None
        assert hooks.before_subagent_dispatch is None
        assert hooks.before_subagent_result is None
        assert not orch.session_workspaces.owns(session.id)
    finally:
        await orch.shutdown()


async def test_traceback_standalone_session_gate_on(tmp_path, base_config):
    """开启 traceback 的独立会话（隐式归属）：门控放行。"""
    orch = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    try:
        session = await _make_session(orch, traceback=SessionTracebackConfig())

        assert await orch._session_workspace_enabled(session) is True
    finally:
        await orch.shutdown()


async def test_traceback_run_member_gate_off(tmp_path, base_config):
    """开启 traceback 但归属显式 run（如容器成员）：run 平面管辖，门控关闭。"""
    orch = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    try:
        run = await orch.run_store.create(
            "",
            "",
            labels={"name": "container"},
            status=RunStatus.RUNNING,
            kind=RunKind.CONTAINER,
        )
        config = SessionConfig(
            name="s1",
            cwd=".",
            traceback=SessionTracebackConfig(),
            run_id=run.run_id,
        )
        session = await orch.pool.create(config)
        await orch.affiliation.attach(session.id, run.run_id, role="worker")

        assert await orch._session_workspace_enabled(session) is False
    finally:
        await orch.shutdown()


async def test_driver_shaped_barrier_and_result_hooks_reach_run_port(tmp_path, base_config):
    """驱动按位置传参调用屏障与结果回调：请求带着类别、工具、工作区声明与子代理来源到达 run 平面端口并放行。"""
    orch = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    try:
        barrier_calls: list[tuple[str, str, object]] = []
        origins: list[tuple[str | None, str | None]] = []
        agent_ids: list[str] = []

        async def barrier_port(session_id: str, kind: str, data: dict, **kwargs) -> BarrierProceed:
            barrier_calls.append((kind, data["tool_name"], kwargs.get("workspace")))
            origins.append((kwargs.get("parent_tool_use_id"), kwargs.get("agent_id")))
            return BarrierProceed(child_cwd="/ws/child" if kind == "dispatch" else None)

        async def result_port(session_id: str, data: dict) -> None:
            agent_ids.append(data["agent_id"])

        orch.set_dispatch_barrier_port(barrier_port)
        orch.set_subagent_result_port(result_port)
        session = await _make_session(orch, traceback=None)
        hooks = orch.make_driver_hooks(session.id)

        dispatch = await hooks.before_subagent_dispatch(
            session.id,
            {"tool_name": "Task", "tool_input": {"prompt": "work"}, "workspace": "reader"},
        )
        side_effect = await hooks.before_side_effect_tool(session.id, {"tool_name": "Write"})
        child_call = await hooks.before_side_effect_tool(
            session.id,
            {
                "tool_name": "Bash",
                "parent_tool_use_id": "toolu-parent",
                "agent_id": "agent-1",
            },
        )
        await hooks.before_subagent_result(session.id, {"agent_id": "agent-1"})

        assert isinstance(dispatch, BarrierProceed) and dispatch.child_cwd == "/ws/child"
        assert isinstance(side_effect, BarrierProceed) and side_effect.child_cwd is None
        assert isinstance(child_call, BarrierProceed)
        assert barrier_calls == [
            ("dispatch", "Task", "reader"),
            ("side_effect", "Write", "writer"),
            ("side_effect", "Bash", "writer"),
        ]
        # 子代理来源随载荷到达端口：主会话的调用两个字段都为空
        assert origins == [("", ""), ("", ""), ("toolu-parent", "agent-1")]
        assert agent_ids == ["agent-1"]
    finally:
        await orch.shutdown()


async def test_driver_shaped_hooks_run_whole_chain_on_session_tree(
    tmp_path, base_config, full_capability_runtime
):
    """接入托管工作区的会话：驱动的位置调用走完整条链——派发注入子工作区 cwd、完成并入主写者，不进端口。"""
    orch = Orchestrator(base_config, default_project_dir=tmp_path / "gate.db")
    try:
        port_calls: list[tuple[object, ...]] = []

        async def unexpected_port(*args: object, **kwargs: object) -> BarrierProceed:
            port_calls.append(args)
            return BarrierProceed()

        orch.set_dispatch_barrier_port(unexpected_port)
        orch.set_subagent_result_port(unexpected_port)
        await orch.run_store.list_runs()  # 铺检查点表
        project = tmp_path / "proj"
        (project / "design").mkdir(parents=True)
        (project / "design" / "base.md").write_text("base\n", encoding="utf-8")
        traced = await orch.pool.create(
            SessionConfig(
                name="s2",
                cwd=str(project),
                runtime=full_capability_runtime,
                traceback=SessionTracebackConfig(),
            )
        )
        await orch.session_workspaces.prepare_session(traced)
        hooks = orch.make_driver_hooks(traced.id)

        # SDK 驱动自己的回调：Task 的 PreToolUse 放行时把子工作区 cwd 注入工具输入
        dispatch = await _sdk_pre_tool_cb(
            {
                "tool_name": "Task",
                "tool_input": {"prompt": "sub"},
                "session_id": "native-1",
                "prompt_id": "msg-1",
                "tool_use_id": "tool-1",
            },
            "tool-1",
            None,
            hooks=hooks,
        )
        child_cwd = Path(dispatch["hookSpecificOutput"]["updatedInput"]["cwd"])
        assert child_cwd.is_dir()
        await hooks.on_subagent_start(
            traced.id, {"agent_id": "agent-native-1", "tool_use_id": "tool-1"}
        )
        side_effect = await hooks.before_side_effect_tool(
            traced.id, {"tool_name": "Bash", "tool_use_id": "tool-2", "prompt_id": "msg-1"}
        )
        assert isinstance(side_effect, BarrierProceed) and side_effect.child_cwd is None
        (child_cwd / "design" / "from-child.md").write_text("child\n", encoding="utf-8")

        stop_output = await _sdk_subagent_stop_cb(
            {"agent_id": "agent-native-1"}, None, None, hooks=hooks
        )

        assert stop_output == {}
        # 接入后 cwd 指向主写者工作区
        assert (Path(traced.config.cwd) / "design" / "from-child.md").read_text(
            encoding="utf-8"
        ) == "child\n"
        assert not child_cwd.exists()
        assert port_calls == []
    finally:
        await orch.shutdown()
