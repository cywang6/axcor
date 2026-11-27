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

"""派发屏障能力矩阵与 SDK 前置屏障接线测试。

矩阵断言固化各 runtime 的能力声明：claude SDK 不声明子代理 cwd 注入
（捆绑 CLI 上实测不成立），其余四项为代码级结论；claude CLI 的 matcher
面由部署侧管理按缺失处理；opencode DB 轮询全缺。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from workflow_controller.driver.claude_sdk import (
    ClaudeSDKDriver,
    _sdk_pre_tool_cb,
    _sdk_subagent_stop_cb,
)
from workflow_controller.driver.hooks import (
    BarrierDeny,
    BarrierProceed,
    DriverHooks,
    is_dispatch_tool,
    is_side_effect_tool,
)
from workflow_controller.driver.opencode import build_restricted_agent_config
from workflow_controller.plugin.dispatch_barrier import (
    DispatchCapability,
    RuntimeDispatchCapabilities,
    dispatch_capabilities_for,
    group_arbitration_block_reason,
    writer_dispatch_block_reason,
)

# claude SDK 经代码级核验的四项屏障能力（不含子代理 cwd 注入）
_SDK_CODE_LEVEL = frozenset(
    {
        DispatchCapability.TASK_PRE_INTERCEPT,
        DispatchCapability.PARENT_LOCAL_QUIESCE,
        DispatchCapability.PRE_DISPATCH_ANCHOR,
        DispatchCapability.SIDE_EFFECT_ARBITRATION,
    }
)


def test_claude_sdk_omits_child_cwd_injection_and_denies_writer_in_strict_mode() -> None:
    """claude SDK：四项代码级能力在册、子代理 cwd 注入不在册。

    严格档据此拒绝 writer 子代理，副作用屏障照常放行；弱化档两者都放行。
    """
    caps = dispatch_capabilities_for("claude_sdk")
    assert caps.capabilities >= _SDK_CODE_LEVEL
    assert not caps.supports(DispatchCapability.CHILD_CWD_INJECTION)
    reason = writer_dispatch_block_reason(caps, strict=True)
    assert reason is not None and "cwd 注入" in reason
    assert group_arbitration_block_reason(caps, strict=True) is None
    assert writer_dispatch_block_reason(caps, strict=False) is None


def test_claude_sdk_declares_subagent_completion_hook() -> None:
    """claude SDK 声明子代理完成前置钩子（时序为代码级推断，未在真实 runtime 验证）。"""
    caps = dispatch_capabilities_for("claude_sdk")
    assert caps.supports(DispatchCapability.SUBAGENT_RESULT_PRE_HOOK)


def test_runtime_missing_completion_hook_denies_writer_in_strict_mode() -> None:
    """有 cwd 注入但缺完成前置钩子：严格档仍拒绝 writer 子代理，弱化档放行。"""
    caps = RuntimeDispatchCapabilities("fake", frozenset({DispatchCapability.CHILD_CWD_INJECTION}))
    reason = writer_dispatch_block_reason(caps, strict=True)
    assert reason is not None and "完成前置钩子" in reason
    assert writer_dispatch_block_reason(caps, strict=False) is None


def test_opencode_declares_no_capabilities_and_strict_denies() -> None:
    """opencode：DB 轮询全缺；严格档拒绝 writer 子代理与组仲裁。"""
    caps = dispatch_capabilities_for("opencode")
    assert not caps.capabilities
    writer_reason = writer_dispatch_block_reason(caps, strict=True)
    assert writer_reason is not None and "cwd 注入" in writer_reason
    group_reason = group_arbitration_block_reason(caps, strict=True)
    assert group_reason is not None and "组仲裁" in group_reason


def test_claude_cli_matcher_face_unmanaged_counts_as_missing() -> None:
    """claude CLI：matcher 面不受本仓控制，①②④⑤按缺失处理。"""
    caps = dispatch_capabilities_for("claude_cli")
    assert caps.capabilities == frozenset({DispatchCapability.PARENT_LOCAL_QUIESCE})
    assert writer_dispatch_block_reason(caps, strict=True) is not None


def test_unknown_runtime_counts_as_missing() -> None:
    """未知名 runtime 按全缺处理（不静默放行）。"""
    caps = dispatch_capabilities_for("some_new_runtime")
    assert not caps.capabilities
    assert writer_dispatch_block_reason(caps, strict=True) is not None


def test_non_strict_mode_does_not_block() -> None:
    """弱化档（compat_late_barrier）不做 fail-closed 拦截。"""
    caps = dispatch_capabilities_for("opencode")
    assert writer_dispatch_block_reason(caps, strict=False) is None
    assert group_arbitration_block_reason(caps, strict=False) is None


def test_opencode_denied_tools_translate_to_task_permission_deny() -> None:
    """严格档出生侧拒绝：disallowed_tools 无 allowlist 也产出顶层 task deny。"""
    config = build_restricted_agent_config(None, ["Task"])
    assert config is not None
    assert config["permission"] == {"task": "deny"}
    # allowlist 面行为不变：只读沙箱仍按未列类目拒绝
    readonly = build_restricted_agent_config(["Read", "Grep"])
    assert readonly is not None
    assert "task" in readonly["permission"]
    assert "read" not in readonly["permission"]


def _hooks(
    *,
    dispatch: Any = None,
    side_effect: Any = None,
    pre_tool: Any = None,
) -> DriverHooks:
    """构造带屏障回调的 hooks 桩（回调统一包成协程函数）。"""
    return DriverHooks(
        session_id="ses-1",
        on_pre_tool_use=_async_barrier(pre_tool),
        before_subagent_dispatch=_async_barrier(dispatch),
        before_side_effect_tool=_async_barrier(side_effect),
    )


def _async_barrier(fn: Any) -> Any:
    """把回调包成异步（同步/异步函数与 None 均可）。"""
    if fn is None:
        return None

    async def wrapper(sid: str, data: dict[str, Any]) -> Any:
        outcome = fn(sid, data)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    return wrapper


@pytest.mark.parametrize(
    "requested", [{}, {"run_in_background": True}], ids=["unset", "background"]
)
async def test_sdk_pre_tool_dispatch_proceeds_with_cwd_injection(requested: dict[str, Any]) -> None:
    """task 派发屏障放行：updatedInput 带子代理工作区 cwd，并强制同步派发。

    捆绑 CLI 的子代理缺省在后台启动，模型即便要求后台运行也改回同步：
    父会话要停在工具调用上，子作用域才能在结果交付前并入。
    """
    hooks = _hooks(dispatch=lambda sid, data: _ok("/ws/child-1"))
    result = await _sdk_pre_tool_cb(
        {"tool_name": "Task", "tool_input": {"prompt": "work", **requested}, "session_id": "nat-1"},
        "toolu-1",
        None,
        hooks=hooks,
    )
    specific = result["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "allow"
    assert specific["updatedInput"] == {
        "prompt": "work",
        **requested,
        "cwd": "/ws/child-1",
        "run_in_background": False,
    }


async def test_sdk_pre_tool_dispatch_deny_blocks() -> None:
    """task 派发屏障拒绝：工具阻断，fault 语义并入理由。"""
    hooks = _hooks(
        dispatch=lambda sid, data: BarrierDeny(
            reason="缺少 cwd 注入能力", fault="fault.scope_quiesce_failed"
        )
    )
    result = await _sdk_pre_tool_cb(
        {"tool_name": "Task", "tool_input": {}, "session_id": "nat-1"},
        "toolu-1",
        None,
        hooks=hooks,
    )
    assert result["decision"] == "block"
    assert "缺少 cwd 注入能力" in result["reason"]
    assert "fault.scope_quiesce_failed" in result["reason"]


async def test_sdk_pre_tool_passes_workspace_declaration() -> None:
    """task 输入的 workspace 声明随屏障载荷透传（reader 快照声明）。"""
    seen: list[dict[str, Any]] = []

    def dispatch(sid: str, data: dict[str, Any]) -> BarrierProceed:
        seen.append(data)
        return BarrierProceed(child_cwd="/ws/reader-1")

    hooks = _hooks(dispatch=dispatch)
    await _sdk_pre_tool_cb(
        {
            "tool_name": "Task",
            "tool_input": {"prompt": "read only", "workspace": "reader"},
            "session_id": "nat-1",
        },
        "toolu-1",
        None,
        hooks=hooks,
    )
    assert seen[0]["workspace"] == "reader"
    # 未声明时按 writer 默认
    await _sdk_pre_tool_cb(
        {"tool_name": "Task", "tool_input": {"prompt": "write"}, "session_id": "nat-1"},
        "toolu-2",
        None,
        hooks=hooks,
    )
    assert seen[1]["workspace"] == "writer"


async def test_sdk_pre_tool_side_effect_barrier_routes() -> None:
    """写文件类工具走副作用屏障；非副作用工具不触发。"""
    seen: list[str] = []

    async def barrier(sid: str, data: dict[str, Any]) -> BarrierProceed:
        seen.append(data["tool_name"])
        return BarrierProceed()

    hooks = _hooks(side_effect=barrier)
    result = await _sdk_pre_tool_cb(
        {"tool_name": "Write", "tool_input": {"file_path": "a"}, "session_id": "nat-1"},
        "toolu-2",
        None,
        hooks=hooks,
    )
    assert result == {}
    assert seen == ["Write"]
    assert is_side_effect_tool("Write") and is_side_effect_tool("Bash")
    assert not is_side_effect_tool("Read")
    assert is_dispatch_tool("Task") and is_dispatch_tool("Agent")


@pytest.mark.parametrize("tool_name", ["Bash", "Task"])
async def test_sdk_pre_tool_passes_subagent_origin_fields(tool_name: str) -> None:
    """子代理自身的调用：屏障载荷带父工具调用号与子代理标识；主线程的调用两者为空、行为不变。"""
    seen: list[tuple[str, str]] = []

    async def barrier(sid: str, data: dict[str, Any]) -> BarrierProceed:
        seen.append((data["parent_tool_use_id"], data["agent_id"]))
        return BarrierProceed()

    hooks = _hooks(dispatch=barrier, side_effect=barrier)
    child_call = await _sdk_pre_tool_cb(
        {
            "tool_name": tool_name,
            "tool_input": {"command": "ls"},
            "parent_tool_use_id": "toolu-parent",
            "agent_id": "agent-1",
        },
        "toolu-child",
        None,
        hooks=hooks,
    )
    main_call = await _sdk_pre_tool_cb(
        {"tool_name": tool_name, "tool_input": {"command": "ls"}},
        "toolu-main",
        None,
        hooks=hooks,
    )
    assert child_call == {} and main_call == {}
    assert seen == [("toolu-parent", "agent-1"), ("", "")]


async def test_sdk_pre_tool_reviewer_block_wins_over_injection() -> None:
    """拦截面阻断优先于 cwd 注入（不放行被 reviewer 拒绝的派发）。"""
    hooks = _hooks(
        dispatch=lambda sid, data: _ok("/ws/child-2"),
        pre_tool=lambda sid, data: {"decision": "block", "reason": "not allowed"},
    )
    result = await _sdk_pre_tool_cb(
        {"tool_name": "Task", "tool_input": {}, "session_id": "nat-1"},
        "toolu-1",
        None,
        hooks=hooks,
    )
    assert result == {"decision": "block", "reason": "not allowed"}


async def test_sdk_pre_tool_without_barriers_keeps_legacy_shape() -> None:
    """未接屏障时回调形态与旧行为零差异（旧路径回归面）。"""
    hooks = _hooks(pre_tool=lambda sid, data: {"decision": "block", "reason": "r"})
    result = await _sdk_pre_tool_cb(
        {"tool_name": "Write", "tool_input": {}, "session_id": "nat-1"},
        "toolu-1",
        None,
        hooks=hooks,
    )
    assert result == {"decision": "block", "reason": "r"}
    plain = DriverHooks(session_id="ses-1")
    assert (
        await _sdk_pre_tool_cb(
            {"tool_name": "Write", "tool_input": {}, "session_id": "nat-1"},
            "toolu-1",
            None,
            hooks=plain,
        )
        == {}
    )


def test_sdk_hook_matchers_follow_barrier_wiring() -> None:
    """matcher 装配：派发屏障接 Task 面，副作用屏障独立可接，子代理停止钩子随回调接线。"""
    driver = ClaudeSDKDriver()
    with_dispatch = driver._build_sdk_hooks(_hooks(dispatch=lambda sid, data: _ok(None)))
    matchers = [m.matcher for m in with_dispatch["PreToolUse"]]
    assert any("Task" in matcher for matcher in matchers)

    side_only = driver._build_sdk_hooks(_hooks(side_effect=lambda sid, data: _ok(None)))
    assert [m.matcher for m in side_only["PreToolUse"]]

    legacy = driver._build_sdk_hooks(_hooks(pre_tool=lambda sid, data: None))
    legacy_matchers = [m.matcher for m in legacy["PreToolUse"]]
    assert "AskUserQuestion" in legacy_matchers
    assert not any("Task" in matcher for matcher in legacy_matchers)

    async def noop(sid: str, data: dict[str, Any]) -> None:
        return None

    result_only = driver._build_sdk_hooks(
        DriverHooks(session_id="ses-1", before_subagent_result=noop)
    )
    assert "SubagentStop" in result_only
    assert "SubagentStop" not in driver._build_sdk_hooks(DriverHooks(session_id="ses-1"))


async def test_sdk_subagent_stop_awaits_result_hook_before_tracker() -> None:
    """子代理停止钩子：先等结果交付前的回调，再通知在途跟踪；回调异常不外抛。"""
    calls: list[str] = []

    async def before_result(sid: str, data: dict[str, Any]) -> None:
        calls.append(f"before:{data['agent_id']}")

    async def on_stop(sid: str, data: dict[str, Any]) -> None:
        calls.append(f"stop:{data['agent_id']}")

    hooks = DriverHooks(
        session_id="ses-1", before_subagent_result=before_result, on_subagent_stop=on_stop
    )
    result = await _sdk_subagent_stop_cb(
        {"agent_id": "agent-1", "agent_type": "general-purpose"}, None, None, hooks=hooks
    )
    assert result == {}
    assert calls == ["before:agent-1", "stop:agent-1"]

    async def failing(sid: str, data: dict[str, Any]) -> None:
        raise RuntimeError("并入父作用域失败")

    hooks = DriverHooks(
        session_id="ses-1", before_subagent_result=failing, on_subagent_stop=on_stop
    )
    assert await _sdk_subagent_stop_cb({"agent_id": "agent-2"}, None, None, hooks=hooks) == {}
    assert calls[-1] == "stop:agent-2"


def _ok(cwd: str | None) -> BarrierProceed:
    """放行结果桩（可带 cwd）。"""
    return BarrierProceed(child_cwd=cwd)
