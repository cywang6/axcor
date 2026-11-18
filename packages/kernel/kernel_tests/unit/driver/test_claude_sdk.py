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

"""覆盖 SDK 事件转换、会话 ID 传播和工具事件对齐。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from workflow_controller.driver.claude_sdk import _sdk_to_events
from workflow_controller.session.events import EventType


def assert_two_tool_pair_events(events) -> None:
    """cli/sdk 双轨共用的「1 文本 + 2 工具对」消息转换事件断言。"""
    # 事件构成：1 条 ASSISTANT_MESSAGE，加 2 组 USE→RESULT→CALL，共 7 条
    assert len(events) == 7
    assert [e.event_type for e in events] == [
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_USE,
        EventType.TOOL_RESULT,
        EventType.TOOL_CALL,
        EventType.TOOL_USE,
        EventType.TOOL_RESULT,
        EventType.TOOL_CALL,
    ]
    # ASSISTANT_MESSAGE carries only the text block
    assert [b["type"] for b in events[0].data["content"]] == ["text"]
    # tool_name back-fill is per-pair (matches each pair's use.name)
    assert events[2].data["tool_name"] == "Read"
    assert events[5].data["tool_name"] == "Bash"


# ── Fake SDK message / block types (named to match _sdk_to_events dispatch) ──


class _FakeSystemMessage:
    """Mimics claude_agent_sdk.SystemMessage: subtype + data."""

    def __init__(
        self, subtype: str = "init", session_id: str = "", data: dict | None = None
    ) -> None:
        self.subtype = subtype
        self.session_id = session_id
        self.data = data or {}


class _FakeTaskStarted:
    """Mimics TaskStartedMessage — a SystemMessage subclass whose ``type().__name__
    differs from "SystemMessage", but it carries an early session_id (the key to sdk resume).
    """

    def __init__(self, session_id: str = "") -> None:
        self.subtype = "task_started"
        self.session_id = session_id
        self.data = {}


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    def __init__(self, *, block_id: str, name: str, input_data: dict) -> None:
        self.id = block_id
        self.name = name
        self.input = input_data


class ToolResultBlock:
    def __init__(self, *, tool_use_id: str, content, is_error: bool = False) -> None:
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content: list, model: str = "claude-sonnet-4-6") -> None:
        self.content = content
        self.model = model


class ResultMessage:
    """Mimics claude_agent_sdk.ResultMessage。SDK 0.2.128 起带 subtype='success'
    与 session_id ——回归点：subtype 兜底分支若排在 ResultMessage 分支之前，
    会把它抢先映射成 ITERATION_START，探针（worker validation）只认 STEP_FINISH
    判成功，丢失即「ended without a successful step」、全部 agent 会话被拦。
    """

    def __init__(self, session_id: str = "cli-abc", is_error: bool = False) -> None:
        self.subtype = "success"
        self.session_id = session_id
        self.data = {}
        self.usage = {"input_tokens": 1, "output_tokens": 1}
        self.model_usage = {}
        self.duration_ms = 10
        self.num_turns = 1
        self.total_cost_usd = 0.0
        self.is_error = is_error


# ── ResultMessage: 回归（subtype 抢道） ————————————————————————————————


def test_result_message_maps_to_step_finish() -> None:
    msg = ResultMessage(session_id="cli-abc")
    events = _sdk_to_events(msg, session_id="ops-1", iteration=1)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type is EventType.STEP_FINISH
    assert ev.data["session_id"] == "cli-abc"
    assert ev.data["is_error"] is False


def test_result_message_error_flag_propagates() -> None:
    msg = ResultMessage(session_id="cli-abc", is_error=True)
    events = _sdk_to_events(msg, session_id="ops-1", iteration=1)
    assert events[0].event_type is EventType.STEP_FINISH
    assert events[0].data["is_error"] is True


# ── SystemMessage / TaskStarted: session_id propagation (resume 前置) ────────


def test_system_message_extracts_session_id() -> None:
    msg = _FakeSystemMessage(subtype="init", session_id="cli-abc")
    events = _sdk_to_events(msg, session_id="ops-1", iteration=1)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type is EventType.ITERATION_START
    assert ev.data["session_id"] == "cli-abc"
    assert ev.data["subtype"] == "init"


def test_task_started_is_not_query_boundary() -> None:
    """子代理任务启动只发通知，不归档仍在运行的兄弟代理。"""
    msg = _FakeTaskStarted(session_id="sdk-xyz")
    events = _sdk_to_events(msg, session_id="ops-1", iteration=1)
    assert len(events) == 1
    assert events[0].event_type is EventType.NOTIFICATION
    assert events[0].data["session_id"] == "sdk-xyz"


def test_notification_without_session_id_stays_notification() -> None:
    """Pure notification SystemMessage (no session_id) stays in the
    NOTIFICATION residual bucket — not a lifecycle event.
    """
    msg = _FakeSystemMessage(subtype="thinking_tokens")
    events = _sdk_to_events(msg, session_id="ops-1", iteration=1)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type is EventType.NOTIFICATION
    assert "session_id" not in ev.data


# ── tool-event alignment for AssistantMessage ──────────────────────────────


class TestSdkToolEventAlignment:
    """claude_sdk._sdk_to_events 从 AssistantMessage 的 ToolUseBlock /
    ToolResultBlock 配对发独立 TOOL_USE / TOOL_RESULT / TOOL_CALL 事件。
    ASSISTANT_MESSAGE.content 只保留 text/thinking 块。
    """

    @staticmethod
    def test_tool_use_without_paired_result_skips_tool_result_event():
        # tool_use 块无配对的 tool_result：只发 TOOL_USE + TOOL_CALL（is_error=False）。
        # 无 text/thinking 块时也不发 ASSISTANT_MESSAGE。
        msg = AssistantMessage(
            content=[
                ToolUseBlock(block_id="tu1", name="Read", input_data={"file_path": "foo.java"}),
            ]
        )
        events = _sdk_to_events(msg, "sid", 1)
        assert [e.event_type for e in events] == [
            EventType.TOOL_USE,
            EventType.TOOL_CALL,
        ]
        assert events[1].data["is_error"] is False
        assert "error" not in events[1].data

    @staticmethod
    def test_no_assistant_message_when_only_tool_blocks():
        # AssistantMessage 只含 tool_use/tool_result（无 text/thinking）时
        # 跳过 ASSISTANT_MESSAGE（空 content 无意义），只发独立 tool 事件。
        msg = AssistantMessage(
            content=[
                ToolUseBlock(block_id="tu1", name="Read", input_data={"file_path": "x"}),
                ToolResultBlock(tool_use_id="tu1", content="ok"),
            ]
        )
        events = _sdk_to_events(msg, "sid", 1)
        assert [e.event_type for e in events] == [
            EventType.TOOL_USE,
            EventType.TOOL_RESULT,
            EventType.TOOL_CALL,
        ]

    @staticmethod
    def test_multiple_tool_uses_each_get_independent_events():
        msg = AssistantMessage(
            content=[
                TextBlock("multi"),
                ToolUseBlock(block_id="tu1", name="Read", input_data={"file_path": "a.py"}),
                ToolUseBlock(block_id="tu2", name="Bash", input_data={"command": "ls"}),
                ToolResultBlock(tool_use_id="tu1", content="A"),
                ToolResultBlock(tool_use_id="tu2", content="B"),
            ]
        )
        events = _sdk_to_events(msg, "sid", 1)
        assert_two_tool_pair_events(events)

    @staticmethod
    def _assistant(
        *,
        tool_use_id: str = "tu1",
        tool_name: str = "Read",
        tool_input: dict | None = None,
        result_content: str = "ok",
        is_error: bool = False,
        include_text: bool = True,
    ) -> AssistantMessage:
        if tool_input is None:
            tool_input = {"file_path": "foo.java"}
        content: list = []
        if include_text:
            content.append(TextBlock("thinking aloud"))
        content.append(ToolUseBlock(block_id=tool_use_id, name=tool_name, input_data=tool_input))
        content.append(
            ToolResultBlock(tool_use_id=tool_use_id, content=result_content, is_error=is_error)
        )
        return AssistantMessage(content=content)

    def test_emits_four_events_in_order(self):
        events = _sdk_to_events(self._assistant(), "sid", 1)
        assert [e.event_type for e in events] == [
            EventType.ASSISTANT_MESSAGE,
            EventType.TOOL_USE,
            EventType.TOOL_RESULT,
            EventType.TOOL_CALL,
        ]

    def test_assistant_message_only_carries_text_block(self):
        # ASSISTANT_MESSAGE.content 只含 text/thinking 块；工具调用信息在
        # 独立 TOOL_USE/TOOL_RESULT 事件里。
        events = _sdk_to_events(self._assistant(), "sid", 1)
        blocks = events[0].data["content"]
        assert [b["type"] for b in blocks] == ["text"]
        assert blocks[0]["text"] == "thinking aloud"

    def test_tool_use_data_shape(self):
        events = _sdk_to_events(self._assistant(), "sid", 1)
        use_ev = events[1]
        assert use_ev.event_type == EventType.TOOL_USE
        assert use_ev.data == {"id": "tu1", "name": "Read", "input": {"file_path": "foo.java"}}

    def test_tool_result_data_shape_includes_tool_name(self):
        # TOOL_RESULT.data.tool_name 从配对的 TOOL_USE.name 回填
        # （ToolResultBlock 本身没有 name 字段）。
        events = _sdk_to_events(self._assistant(), "sid", 1)
        result_ev = events[2]
        assert result_ev.event_type == EventType.TOOL_RESULT
        assert result_ev.data == {
            "tool_use_id": "tu1",
            "tool_name": "Read",
            "content": "ok",
            "is_error": False,
        }

    def test_tool_call_data_shape(self):
        events = _sdk_to_events(self._assistant(), "sid", 1)
        call_ev = events[3]
        assert call_ev.event_type == EventType.TOOL_CALL
        assert call_ev.data == {
            "name": "Read",
            "arg": "foo.java",
            "tool_use_id": "tu1",
            "is_error": False,
        }

    def test_error_result_propagates_to_tool_call_error_field(self):
        events = _sdk_to_events(
            self._assistant(result_content="boom: file missing", is_error=True),
            "sid",
            1,
        )
        result_ev = events[2]
        assert result_ev.data["is_error"] is True
        call_ev = events[3]
        assert call_ev.data["is_error"] is True
        # extract_tool_error takes the result content verbatim
        assert call_ev.data["error"] == "boom: file missing"

    def test_no_error_field_when_not_error(self):
        events = _sdk_to_events(self._assistant(), "sid", 1)
        assert "error" not in events[3].data


class TestSdkLimitEvents:
    """_sdk_limit_events — recognize limit/auth error in an SDK exception's
    free text and emit a RATE_LIMIT carrying the normalized signal.

    SDK exceptions surface as strings (no structured rate-limit channel), so
    the translation reuses the driver's `_parse_limit_error` regex. Non-limit exceptions return []
    (only ERROR is emitted upstream; the detector does not act on it).
    """

    @staticmethod
    def test_5h_limit_string_emits_rejected_signal():
        from workflow_controller.driver.claude_sdk import _sdk_limit_events

        events = _sdk_limit_events(
            "sid",
            1,
            RuntimeError("5-hour usage limit reached. Try again at 18:30"),
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.RATE_LIMIT
        sig = ev.data["limit_signal"]
        assert sig["kind"] == "rejected"
        assert sig["scope"] == "5h"
        assert sig["source"].startswith("sdk:regex:")
        assert sig["reset_at"] is not None  # resolved from the 18:30 hint
        assert ev.data["raw"].startswith("5-hour")

    @staticmethod
    def test_weekly_limit_string_emits_rejected_weekly():
        from workflow_controller.driver.claude_sdk import _sdk_limit_events

        events = _sdk_limit_events(
            "sid",
            1,
            RuntimeError("Weekly limit reached — try later"),
        )
        assert len(events) == 1
        sig = events[0].data["limit_signal"]
        assert sig["kind"] == "rejected"
        assert sig["scope"] == "weekly"
        assert sig["reset_at"] is None  # weekly has no reset scheduling

    @staticmethod
    def test_auth_error_string_emits_auth_error():
        from workflow_controller.driver.claude_sdk import _sdk_limit_events

        events = _sdk_limit_events(
            "sid",
            1,
            RuntimeError("Invalid API key provided"),
        )
        assert len(events) == 1
        sig = events[0].data["limit_signal"]
        assert sig["kind"] == "auth_error"
        assert sig["scope"] == "auth_error"

    @staticmethod
    def test_non_limit_exception_returns_empty():
        """Errors that don't match any limit/auth pattern yield no signal —
        only an ERROR is emitted upstream (observability; the detector does
        not act on it).
        """
        from workflow_controller.driver.claude_sdk import _sdk_limit_events

        assert _sdk_limit_events("sid", 1, RuntimeError("node: command not found")) == []


class TestSdkLimitTextMatching:
    """The SDK driver's regex library for classifying limit/auth errors in
    exception free-text (the SDK has no structured rate-limit channel).
    """

    @staticmethod
    def test_parse_limit_error_extracts_reset_hhmm() -> None:
        from workflow_controller.driver.claude_sdk import _parse_limit_error

        match = _parse_limit_error("5-hour usage limit reached. Try again at 18:45")
        assert match is not None
        assert match.scope == "5h"
        assert match.reset_hhmm == "18:45"

    @staticmethod
    def test_parse_limit_error_no_hhmm_when_pattern_lacks_time() -> None:
        from workflow_controller.driver.claude_sdk import _parse_limit_error

        match = _parse_limit_error("Usage limit reached")
        assert match is not None
        assert match.reset_hhmm is None

    @staticmethod
    def test_resolve_reset_at_future_hour_today() -> None:
        from workflow_controller.driver.claude_sdk import _resolve_reset_at

        now = datetime(2026, 5, 25, 10, 0)
        reset = _resolve_reset_at("14:30", now=now)
        assert reset == datetime(2026, 5, 25, 14, 30)

    @staticmethod
    def test_resolve_reset_at_past_hour_means_tomorrow() -> None:
        from workflow_controller.driver.claude_sdk import _resolve_reset_at

        now = datetime(2026, 5, 25, 22, 0)
        reset = _resolve_reset_at("03:00", now=now)
        assert reset == datetime(2026, 5, 26, 3, 0)

    # --- HH:MM → absolute timestamp ----------------------------------------

    @staticmethod
    def test_resolve_reset_at_none_returns_now_plus_5h() -> None:
        from workflow_controller.driver.claude_sdk import _resolve_reset_at

        now = datetime(2026, 5, 25, 10, 0)
        reset = _resolve_reset_at(None, now=now)
        assert reset == now + timedelta(hours=5)

    @pytest.mark.parametrize(
        "text,expected_scope",
        [
            ("5-hour usage limit reached. Try again at 14:30", "5h"),
            ("5 hour usage limit reached. Resets at 09:15", "5h"),
            ("Weekly limit reached for your account", "weekly"),
            ("Usage limit reached", "5h"),  # generic → conservative 5h
            ("Usage limit exceeded", "5h"),
            ("Rate limit exceeded", "rate_limit"),
            ("Invalid API key provided", "auth_error"),
            ("Authentication failed", "auth_error"),
        ],
    )
    def test_parse_limit_error_recognises_known_patterns(
        self, text: str, expected_scope: str
    ) -> None:
        from workflow_controller.driver.claude_sdk import _parse_limit_error

        match = _parse_limit_error(text)
        assert match is not None, f"expected match for {text!r}"
        assert match.scope == expected_scope

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "some random error",
            "Network unreachable",
            "tool_use input was invalid JSON",
        ],
    )
    def test_parse_limit_error_returns_none_for_unrelated_text(self, text: str) -> None:
        from workflow_controller.driver.claude_sdk import _parse_limit_error

        assert _parse_limit_error(text) is None


class TestSdkPermissionDowngrade:
    """SDK (default runtime) path — permission_mode must go through the same
    root downgrade before reaching ClaudeAgentOptions. Regression for the
    review feedback that the original patch only covered claude_cli: the SDK
    SubprocessCLITransport spawns the same `claude` CLI, so root + default
    runtime would otherwise still hit the immediate-exit failure.
    """

    @staticmethod
    def test_query_exception_emits_iteration_end(monkeypatch) -> None:
        """异常路径收尾（docs/event-system.md）：SDK query 异常先 ERROR 再
        ITERATION_END，事件流消费者依赖 iteration_end 判定 query 边界。
        """
        import asyncio

        pytest.importorskip("claude_agent_sdk")
        import claude_agent_sdk
        import claude_agent_sdk.types as sdk_types

        def _raise_query(*_args, **_kwargs):
            raise RuntimeError("sdk query blew up")

        class _FakeOptions:
            def __init__(self, **_kwargs) -> None:
                pass

        monkeypatch.setattr(claude_agent_sdk, "query", _raise_query)
        monkeypatch.setattr(sdk_types, "ClaudeAgentOptions", _FakeOptions)

        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        async def _run() -> list:
            driver = ClaudeSDKDriver()
            return [event async for event in driver.query("hi", session_id="sid")]

        events = asyncio.run(_run())

        assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
        assert events[-1].data["reason"] == "internal_error"

    @staticmethod
    def test_sdk_missing_emits_iteration_end(monkeypatch) -> None:
        """ImportError 分支（SDK 未安装）：ERROR 后补 ITERATION_END，对齐
        上游 862cebc 的覆盖面。
        """
        import asyncio
        import sys

        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        async def _run() -> list:
            driver = ClaudeSDKDriver()
            return [event async for event in driver.query("hi", session_id="sid")]

        events = asyncio.run(_run())

        assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
        assert events[0].data["error"] == "claude-agent-sdk not installed"
        assert events[-1].data["reason"] == "internal_error"

    @staticmethod
    def _capture_options(
        monkeypatch,
        *,
        is_root: bool,
        keep_bypass: bool = False,
        constructor_mode: str = "bypassPermissions",
        per_call_mode: str | None = None,
    ) -> dict[str, object]:
        pytest.importorskip("claude_agent_sdk")
        import claude_agent_sdk
        import claude_agent_sdk.types as sdk_types

        captured: dict[str, object] = {}

        class RecordingOptions:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        async def _fake_query(prompt, options, transport):
            if False:
                yield None

        monkeypatch.setattr(
            "workflow_controller.driver._permission._is_root",
            lambda: is_root,
        )
        monkeypatch.setattr("workflow_controller.driver._permission._warned_root_downgrade", False)
        monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
        monkeypatch.setattr(sdk_types, "ClaudeAgentOptions", RecordingOptions)
        if keep_bypass:
            monkeypatch.setenv("WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS", "1")

        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        async def _run() -> None:
            driver = ClaudeSDKDriver(permission_mode=constructor_mode)
            async for _ev in driver.query(
                "hi",
                permission_mode=per_call_mode,
            ):
                pass

        import asyncio

        asyncio.run(_run())
        return captured

    def test_root_downgrades_sdk_permission_mode(self, monkeypatch) -> None:
        captured = self._capture_options(monkeypatch, is_root=True)
        assert captured["permission_mode"] == "acceptEdits"

    def test_non_root_preserves_sdk_bypass_permissions(self, monkeypatch) -> None:
        captured = self._capture_options(monkeypatch, is_root=False)
        assert captured["permission_mode"] == "bypassPermissions"

    def test_root_escape_hatch_keeps_sdk_bypass_permissions(self, monkeypatch) -> None:
        captured = self._capture_options(monkeypatch, is_root=True, keep_bypass=True)
        assert captured["permission_mode"] == "bypassPermissions"

    def test_per_call_override_wins_over_constructor_mode(self, monkeypatch) -> None:
        """per-call permission_mode beats the constructor default — the SDK
        driver must agree with claude_cli.
        """
        captured = self._capture_options(
            monkeypatch,
            is_root=True,
            constructor_mode="bypassPermissions",
            per_call_mode="acceptEdits",
        )
        assert captured["permission_mode"] == "acceptEdits"
