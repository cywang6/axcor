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

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from workflow_controller.driver._permission import effective_permission_mode
from workflow_controller.driver.claude_cli import (
    ClaudeCLIDriver,
    ClaudeCLIExitError,
    _build_args,
    _cli_line_to_events,
    _split_lines,
    ensure_workspace_trusted,
)
from workflow_controller.session.events import EventType

from .test_claude_sdk import assert_two_tool_pair_events


def _result_json_line(**overrides) -> bytes:
    """构造一条 CLI result JSONL 行（默认 success，可用 overrides 覆盖字段）。"""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "usage": {},
        "modelUsage": {},
        "session_id": "abc",
        "duration_ms": 500,
        "num_turns": 1,
        "total_cost_usd": 0,
    }
    payload.update(overrides)
    return json.dumps(payload).encode() + b"\n"


class _ChunkedFakeStream:
    """按块顺序吐出的假子进程流（stdout/stderr 通用）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._i = 0

    async def read(self, size=-1):
        if self._i >= len(self._chunks):
            return b""
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


@pytest.mark.asyncio
async def test_cli_process_uses_session_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spawn = AsyncMock(side_effect=FileNotFoundError("claude"))
    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", spawn
    )
    driver = ClaudeCLIDriver()

    events = [event async for event in driver.query("hello", cwd=str(tmp_path))]

    assert events[0].event_type == EventType.ERROR
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_spawn_failure_emits_iteration_end(tmp_path, monkeypatch):
    """异常路径收尾（docs/event-system.md）：spawn 失败先 ERROR 再 ITERATION_END，
    事件流消费者依赖 iteration_end 判定 query 边界。
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spawn = AsyncMock(side_effect=FileNotFoundError("claude"))
    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", spawn
    )

    events = [
        event
        async for event in ClaudeCLIDriver().query("prompt", session_id="sid", cwd=str(tmp_path))
    ]

    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
    assert events[1].data["reason"] == "internal_error"


@pytest.mark.asyncio
async def test_missing_cwd_emits_iteration_end(tmp_path, monkeypatch):
    """cwd 不存在的提前返回分支：ERROR 后补 ITERATION_END（对齐上游
    862cebc 的覆盖面；前端拓扑归档依赖 iteration_end 判定 query 边界）。
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    events = []
    async for event in ClaudeCLIDriver().query(
        "prompt", session_id="sid", cwd=str(tmp_path / "does-not-exist")
    ):
        events.append(event)

    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
    assert events[0].data["error"].startswith("project directory not found:")
    assert events[1].data["reason"] == "internal_error"


@pytest.mark.asyncio
async def test_missing_cpx_reports_wrapper_command_and_remedy(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spawn = AsyncMock(side_effect=FileNotFoundError("cpx"))
    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", spawn
    )
    events = []
    async for event in ClaudeCLIDriver(
        claude_bin="claude-that-is-not-installed",
        command_prefix=["cpx"],
    ).query("prompt", session_id="sid", cwd=str(tmp_path)):
        events.append(event)

    assert spawn.call_args.args[:2] == ("cpx", "claude-that-is-not-installed")
    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
    assert events[0].data["error"].startswith("command not found: cpx;")
    assert "install cpx" in events[0].data["error"]


@pytest.mark.asyncio
async def test_unexecutable_command_reports_execute_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spawn = AsyncMock(side_effect=PermissionError("Permission denied"))
    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", spawn
    )
    events = []
    async for event in ClaudeCLIDriver(
        claude_bin="claude",
        command_prefix=["cpx"],
    ).query("prompt", session_id="sid", cwd=str(tmp_path)):
        events.append(event)

    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
    assert events[0].data["error"].startswith("failed to execute cpx:")
    assert "command not found" not in events[0].data["error"]


@pytest.mark.asyncio
async def test_nonzero_exit_reports_cli_reason(tmp_path):
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\necho 'No conversation found with session ID: missing' >&2\nexit 1\n"
    )
    script.chmod(0o755)
    driver = ClaudeCLIDriver(claude_bin=str(script))
    with pytest.raises(ClaudeCLIExitError, match="No conversation found"):
        [event async for event in driver.query("prompt", session_id="sid", cwd=str(tmp_path))]


@pytest.mark.asyncio
async def test_nonzero_exit_when_stopped_yields_iteration_end(tmp_path, monkeypatch):
    driver = ClaudeCLIDriver()

    class FakeStream:
        def __init__(self):
            self.first = True

        async def read(self, size=-1):
            if self.first:
                self.first = False
                getattr(driver, "_stop_event").set()
            return b""

    class FakeProc:
        returncode = -9

        def __init__(self):
            self.stdin = self
            self.stdout = FakeStream()
            self.stderr = FakeStream()

        @staticmethod
        def close():
            pass

        async def wait(self):
            return -9

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    events = [event async for event in driver.query("prompt", session_id="sid", cwd=str(tmp_path))]
    assert [event.event_type for event in events] == [EventType.ITERATION_END]


@pytest.mark.asyncio
async def test_nonzero_exit_after_result_yields_iteration_end(tmp_path, monkeypatch):
    # 已产出 result 行后 CLI 慢退被 SIGKILL（code -9）不应误报失败。
    driver = ClaudeCLIDriver()
    result_line = _result_json_line()

    class FakeProc:
        returncode = -9

        def __init__(self):
            self.stdin = self
            self.stdout = _ChunkedFakeStream([result_line])
            self.stderr = _ChunkedFakeStream([])

        @staticmethod
        def close():
            pass

        async def wait(self):
            return -9

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    events = [event async for event in driver.query("prompt", session_id="sid", cwd=str(tmp_path))]
    assert [event.event_type for event in events] == [
        EventType.STEP_FINISH,
        EventType.ITERATION_END,
    ]


@pytest.mark.asyncio
async def test_nonzero_exit_after_error_result_still_reports_exit_code(tmp_path, monkeypatch):
    # error 子类型的 result 行不算「完成」：CLI 产出 error result 后非零退出，
    # 退出码仍须上报（result_received 门控在 not is_error 上）。
    driver = ClaudeCLIDriver()
    result_line = _result_json_line(subtype="error_during_execution", is_error=True)

    class FakeProc:
        returncode = 1

        def __init__(self):
            self.stdin = self
            self.stdout = _ChunkedFakeStream([result_line])
            self.stderr = _ChunkedFakeStream([b"boom: execution failed\n"])

        @staticmethod
        def close():
            pass

        async def wait(self):
            return 1

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    with pytest.raises(ClaudeCLIExitError, match="boom: execution failed"):
        [event async for event in driver.query("prompt", session_id="sid", cwd=str(tmp_path))]


@pytest.mark.asyncio
async def test_stderr_drain_timeout_does_not_hang(tmp_path, monkeypatch):
    # 分离孙进程持有 stderr 写端时管道永不 EOF：排空须超时放弃，query 不挂死。
    monkeypatch.setattr("workflow_controller.driver.claude_cli._STDERR_DRAIN_TIMEOUT_SECONDS", 0.05)
    driver = ClaudeCLIDriver()

    class EndlessStream:
        async def read(self, size=-1):
            await asyncio.Event().wait()  # 永不返回，模拟 stderr 管道被孙进程持有、永不 EOF

    class EofStream:
        async def read(self, size=-1):
            return b""

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdin = self
            self.stdout = EofStream()
            self.stderr = EndlessStream()

        @staticmethod
        def close():
            pass

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    events = [event async for event in driver.query("prompt", session_id="sid", cwd=str(tmp_path))]
    assert [event.event_type for event in events] == [EventType.ITERATION_END]


class TestPermissionMode:
    """_permission helpers — root downgrade shared by claude_cli and claude_sdk."""

    @staticmethod
    def test_cli_build_args_supports_command_prefix() -> None:
        """验证 Claude 命令保留 wrapper 前缀。"""
        args = _build_args(
            bin_path="claude",
            prompt="p",
            model="",
            cwd=None,
            max_turns=None,
            allowed_tools=None,
            disallowed_tools=None,
            permission_mode="bypassPermissions",
            add_dirs=[],
            resume=None,
            command_prefix=["cpx"],
        )
        assert args[:2] == ["cpx", "claude"]

    @staticmethod
    def test_root_bypass_permissions_downgrades_to_accept_edits(monkeypatch, caplog):
        monkeypatch.setattr("workflow_controller.driver._permission._is_root", lambda: True)
        monkeypatch.setattr("workflow_controller.driver._permission._warned_root_downgrade", False)
        with caplog.at_level("WARNING", logger="workflow_controller.driver._permission"):
            assert effective_permission_mode("bypassPermissions") == "acceptEdits"
        assert any("downgrading permission mode" in r.message for r in caplog.records)

    @staticmethod
    def test_non_root_bypass_permissions_is_preserved(monkeypatch):
        monkeypatch.setattr("workflow_controller.driver._permission._is_root", lambda: False)
        assert effective_permission_mode("bypassPermissions") == "bypassPermissions"

    @staticmethod
    def test_root_explicit_mode_is_preserved(monkeypatch):
        monkeypatch.setattr("workflow_controller.driver._permission._is_root", lambda: True)
        assert effective_permission_mode("default") == "default"

    @staticmethod
    def test_warns_only_once_per_process(monkeypatch, caplog):
        monkeypatch.setattr("workflow_controller.driver._permission._is_root", lambda: True)
        monkeypatch.setattr("workflow_controller.driver._permission._warned_root_downgrade", False)
        with caplog.at_level("WARNING", logger="workflow_controller.driver._permission"):
            assert effective_permission_mode("bypassPermissions") == "acceptEdits"
            assert effective_permission_mode("bypassPermissions") == "acceptEdits"
            assert effective_permission_mode("bypassPermissions") == "acceptEdits"
        downgrade_warnings = [
            r for r in caplog.records if "downgrading permission mode" in r.message
        ]
        assert len(downgrade_warnings) == 1

    @staticmethod
    def test_root_escape_hatch_keeps_bypass_permissions(monkeypatch):
        monkeypatch.setattr("workflow_controller.driver._permission._is_root", lambda: True)
        monkeypatch.setenv("WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS", "1")
        assert effective_permission_mode("bypassPermissions") == "bypassPermissions"


class TestCliLineToEvents:
    """_cli_line_to_events returns a list[Event] (one JSON line may produce
    multiple Events — assistant lines with tool_use+tool_result produce
    ASSISTANT_MESSAGE + TOOL_USE + TOOL_RESULT + TOOL_CALL).
    """

    @staticmethod
    def test_system_init():
        line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "abc123",
                "tools": ["Read"],
                "model": "claude-sonnet-4-6",
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.ITERATION_START
        assert ev.data.get("subtype") == "init"
        assert ev.data.get("session_id") == "abc123"

    @staticmethod
    def test_system_notification_stays_notification():
        """Pure notification system line (non-init subtype / no session_id)
        stays in the NOTIFICATION residual bucket.
        """
        line = json.dumps({"type": "system", "subtype": "thinking_tokens", "tools": ["Read"]})
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.NOTIFICATION
        assert ev.data.get("subtype") == "thinking_tokens"

    @staticmethod
    def test_assistant_text():
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "Hello world"}],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.ASSISTANT_MESSAGE
        assert ev.data["content"][0]["type"] == "text"
        assert ev.data["content"][0]["text"] == "Hello world"

    @staticmethod
    def test_assistant_text_empty_skipped():
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "  "}],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert events == []

    @staticmethod
    def test_assistant_tool_use_only_emits_tool_events_no_assistant_message():
        # 只有 tool_use 块（无 text/thinking）：不发 ASSISTANT_MESSAGE；
        # 只发 TOOL_USE + TOOL_CALL（无配对 result，is_error=False）。
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "Read",
                            "input": {"file_path": "foo.java"},
                        }
                    ],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 2
        use_ev = events[0]
        assert use_ev.event_type == EventType.TOOL_USE
        assert use_ev.data == {"id": "tu1", "name": "Read", "input": {"file_path": "foo.java"}}
        call_ev = events[1]
        assert call_ev.event_type == EventType.TOOL_CALL
        assert call_ev.data["name"] == "Read"
        assert call_ev.data["arg"] == "foo.java"
        assert call_ev.data["is_error"] is False
        assert "error" not in call_ev.data

    @staticmethod
    def test_assistant_tool_result_only_emits_nothing():
        # 孤儿 tool_result（无配对 tool_use、无 text/thinking）被丢弃：
        # ASSISTANT_MESSAGE 因空 content 跳过；TOOL_RESULT 缺 tool_name 来源不发。
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": "file content",
                            "is_error": False,
                        }
                    ],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert events == []

    @staticmethod
    def test_assistant_thinking():
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "thinking", "thinking": "Let me think..."}],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        block = events[0].data["content"][0]
        assert block["type"] == "thinking"
        assert block["thinking"] == "Let me think..."

    @staticmethod
    def test_user():
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "result"}]
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        assert events[0].event_type == EventType.USER_MESSAGE

    @staticmethod
    def test_result_success():
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 100},
                "modelUsage": {},
                "session_id": "abc",
                "duration_ms": 500,
                "num_turns": 1,
                "total_cost_usd": 0.01,
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.STEP_FINISH
        assert ev.data["is_error"] is False
        assert ev.data["usage"]["input_tokens"] == 100
        assert ev.data["context_usage"] == ev.data["usage"]

    @staticmethod
    def test_result_uses_last_assistant_request_for_context():
        tracker = {}
        for tokens in (200, 350):
            _cli_line_to_events(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"usage": {"input_tokens": tokens}, "content": []},
                    }
                ),
                "sid",
                1,
                tracker,
            )
        result = _cli_line_to_events(
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 550},
                    "modelUsage": {"model": {"contextWindow": 1000}},
                }
            ),
            "sid",
            1,
            tracker,
        )[0]
        assert result.data["context_usage"]["input_tokens"] == 350
        assert result.data["usage"]["input_tokens"] == 550
        assert result.data["modelUsage"]["model"]["contextWindow"] == 1000

    @staticmethod
    def test_result_error():
        line = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "usage": {},
                "modelUsage": {},
                "session_id": "abc",
                "duration_ms": 100,
                "num_turns": 0,
                "total_cost_usd": 0,
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        assert events[0].data["is_error"] is True

    @staticmethod
    def test_rate_limit():
        line = json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"allowedRequests": 1000, "remainingRequests": 500},
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == EventType.RATE_LIMIT
        # normalized signal — no status field → unknown kind (detector no-ops)
        assert ev.data["limit_signal"]["kind"] == "unknown"
        # native payload preserved verbatim in raw
        assert ev.data["raw"]["remainingRequests"] == 500

    @staticmethod
    def test_unknown_type_skipped():
        line = json.dumps({"type": "unknown_foo", "data": "bar"})
        assert _cli_line_to_events(line, "sid", 1) == []

    @staticmethod
    def test_invalid_json_skipped():
        assert _cli_line_to_events("not valid json {{{", "sid", 1) == []

    @staticmethod
    def test_empty_line_skipped():
        assert _cli_line_to_events("", "sid", 1) == []

    @staticmethod
    def test_non_dict_root_skipped():
        assert _cli_line_to_events("[1,2,3]", "sid", 1) == []


class TestToolEventAlignment:
    """claude_cli._parse_assistant 从 tool_use + tool_result block 配对发
    独立 TOOL_USE / TOOL_RESULT / TOOL_CALL 事件。ASSISTANT_MESSAGE.content
    只保留 text/thinking 块。
    """

    @staticmethod
    def test_multiple_tool_uses_each_get_independent_events():
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "text", "text": "multi"},
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": "tu2",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": "A",
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu2",
                            "content": "B",
                            "is_error": False,
                        },
                    ],
                },
            }
        )
        events = _cli_line_to_events(line, "sid", 1)
        assert_two_tool_pair_events(events)

    @staticmethod
    def _assistant_line(
        *,
        tool_use_id: str = "tu1",
        tool_name: str = "Read",
        tool_input: dict | None = None,
        result_content: str = "ok",
        is_error: bool = False,
        include_text: bool = True,
    ) -> str:
        if tool_input is None:
            tool_input = {"file_path": "foo.java"}
        content: list = []
        if include_text:
            content.append({"type": "text", "text": "thinking aloud"})
        content.append(
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}
        )
        content.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_content,
                "is_error": is_error,
            }
        )
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": content,
                },
            }
        )

    def test_emits_four_events_in_order(self):
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        assert [e.event_type for e in events] == [
            EventType.ASSISTANT_MESSAGE,
            EventType.TOOL_USE,
            EventType.TOOL_RESULT,
            EventType.TOOL_CALL,
        ]

    def test_assistant_message_only_carries_text_block(self):
        # ASSISTANT_MESSAGE.content 只含 text/thinking 块；工具调用信息在
        # 独立 TOOL_USE/TOOL_RESULT 事件里。
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        blocks = events[0].data["content"]
        assert [b["type"] for b in blocks] == ["text"]
        assert blocks[0]["text"] == "thinking aloud"

    def test_no_assistant_message_when_only_tool_blocks(self):
        # AssistantMessage 只含 tool_use/tool_result（无 text/thinking）时
        # 跳过 ASSISTANT_MESSAGE（空 content 无意义），只发独立 tool 事件。
        events = _cli_line_to_events(self._assistant_line(include_text=False), "sid", 1)
        assert [e.event_type for e in events] == [
            EventType.TOOL_USE,
            EventType.TOOL_RESULT,
            EventType.TOOL_CALL,
        ]

    def test_tool_use_data_shape(self):
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        use_ev = events[1]
        assert use_ev.event_type == EventType.TOOL_USE
        assert use_ev.data == {"id": "tu1", "name": "Read", "input": {"file_path": "foo.java"}}

    def test_tool_result_data_shape_includes_tool_name(self):
        # TOOL_RESULT.data.tool_name 从配对的 TOOL_USE.name 回填——
        # breather._on_notify_skill_hook 据此匹配 notify_skill。
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        result_ev = events[2]
        assert result_ev.event_type == EventType.TOOL_RESULT
        assert result_ev.data == {
            "tool_use_id": "tu1",
            "tool_name": "Read",
            "content": "ok",
            "is_error": False,
        }

    def test_tool_call_data_shape(self):
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        call_ev = events[3]
        assert call_ev.event_type == EventType.TOOL_CALL
        assert call_ev.data == {
            "name": "Read",
            "arg": "foo.java",
            "tool_use_id": "tu1",
            "is_error": False,
        }

    def test_error_result_propagates_to_tool_call_error_field(self):
        events = _cli_line_to_events(
            self._assistant_line(result_content="boom: file missing", is_error=True),
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
        events = _cli_line_to_events(self._assistant_line(), "sid", 1)
        assert "error" not in events[3].data


class TestSplitLines:
    """_split_lines 分块切行：跨 chunk 长行、单 chunk 多行、EOF 无换行尾串。
    用假 StreamReader（async read）覆盖 stdout/stderr 相同循环的边界。
    """

    @staticmethod
    async def _collect(chunks: list[bytes]) -> list[bytes]:
        class _ChunkReader:
            def __init__(self) -> None:
                self._chunks = iter(chunks)

            async def read(self, n: int) -> bytes:
                try:
                    return next(self._chunks)
                except StopIteration:
                    return b""

        return [line async for line in _split_lines(_ChunkReader())]

    async def test_long_line_spanning_chunks(self) -> None:
        # 单行 JSON > 64KB，换行符出现在第二个 chunk 内——
        # StreamReader.readline 的 64KB 行上限场景。
        line = b"a" * 70000 + b"b" * 3000 + b"\n"
        first, second = line[:65536], line[65536:]
        assert second  # 换行确实跨到了第二个 chunk
        assert await self._collect([first, second]) == [b"a" * 70000 + b"b" * 3000]

    async def test_multiple_lines_in_one_chunk(self) -> None:
        assert await self._collect([b"line1\nline2\nline3\n"]) == [
            b"line1",
            b"line2",
            b"line3",
        ]

    async def test_eof_trailing_line_without_newline(self) -> None:
        assert await self._collect([b"line1\nno-newline-tail"]) == [
            b"line1",
            b"no-newline-tail",
        ]

    async def test_line_split_across_many_chunks_with_trailing_tail(self) -> None:
        chunks = [b"x" * 60000, b"y" * 60000 + b"\n" + b"z" * 100]
        assert await self._collect(chunks) == [
            b"x" * 60000 + b"y" * 60000,
            b"z" * 100,
        ]

    async def test_empty_input_yields_nothing(self) -> None:
        assert await self._collect([]) == []


def _assistant_usage_line(usage, parent_tool_use_id=None):
    line = {"type": "assistant", "message": {"usage": usage, "content": []}}
    if parent_tool_use_id:
        line["parent_tool_use_id"] = parent_tool_use_id
    return json.dumps(line)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 100, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300},
        {"input_tokens": 0},
        {"cache_creation_input_tokens": 100},
    ],
)
def test_assistant_usage_emits_context_event_and_feeds_result_snapshot(usage):
    """assistant 用量即时发独立事件；result 沿用最后快照，计费字段不混入。"""
    tracker = {}
    events = _cli_line_to_events(_assistant_usage_line(usage), "sid", 1, tracker)
    assert len(events) == 1
    assert events[0].event_type == EventType.CONTEXT_USAGE
    assert events[0].data["context_usage"] == usage
    assert "usage" not in events[0].data
    assert "total_cost_usd" not in events[0].data
    result = _cli_line_to_events(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 900000},
                "total_cost_usd": 1.5,
            }
        ),
        "sid",
        1,
        tracker,
    )[0]
    assert result.data["context_usage"] == usage
    assert result.data["usage"]["input_tokens"] == 900000
    assert result.data["total_cost_usd"] == 1.5


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"output_tokens": 5},
        {"input_tokens": -1},
        {"input_tokens": None},
        {"input_tokens": "100"},
    ],
)
def test_invalid_usage_keeps_last_snapshot(usage):
    tracker = {"usage": {"input_tokens": 123}}
    events = _cli_line_to_events(_assistant_usage_line(usage), "sid", 1, tracker)
    assert events == []
    assert tracker["usage"] == {"input_tokens": 123}


def test_subagent_usage_does_not_replace_main_context():
    tracker = {"usage": {"input_tokens": 123}}
    events = _cli_line_to_events(
        _assistant_usage_line({"input_tokens": 99999}, parent_tool_use_id="task-1"),
        "sid",
        1,
        tracker,
    )
    assert events == []
    assert tracker["usage"] == {"input_tokens": 123}


class TestEnsureWorkspaceTrusted:
    """spawn 前预置 workspace 信任：幂等写入 Claude 配置，best-effort 不阻断。"""

    @staticmethod
    def test_missing_file_gets_minimal_structure(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        ensure_workspace_trusted(str(tmp_path / "proj"), home=str(home))
        data = json.loads((home / ".claude.json").read_text())
        key = str((tmp_path / "proj").resolve())
        assert data["projects"][key]["hasTrustDialogAccepted"] is True

    @staticmethod
    def test_existing_entries_and_fields_preserved(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg = home / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "numStartups": 5,
                    "projects": {
                        "/other": {"hasTrustDialogAccepted": True, "lastCost": 1.2},
                        "/mine": {"hasTrustDialogAccepted": False, "history": ["a"]},
                    },
                }
            )
        )
        ensure_workspace_trusted("/mine", home=str(home))
        data = json.loads(cfg.read_text())
        assert data["numStartups"] == 5
        assert data["projects"]["/other"]["lastCost"] == 1.2
        assert data["projects"]["/mine"]["hasTrustDialogAccepted"] is True
        assert data["projects"]["/mine"]["history"] == ["a"]

    @staticmethod
    def test_already_trusted_is_noop(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg = home / ".claude.json"
        cfg.write_text(json.dumps({"projects": {"/p": {"hasTrustDialogAccepted": True}}}))
        before = cfg.read_text()
        ensure_workspace_trusted("/p", home=str(home))
        assert cfg.read_text() == before

    @staticmethod
    def test_corrupt_json_left_alone(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg = home / ".claude.json"
        cfg.write_text("{not json")
        ensure_workspace_trusted("/p", home=str(home))
        assert cfg.read_text() == "{not json"

    @staticmethod
    def test_non_utf8_config_left_alone(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg = home / ".claude.json"
        raw = b'{"projects": {"\xff\xfe": true}}'
        cfg.write_bytes(raw)
        ensure_workspace_trusted("/p", home=str(home))
        assert cfg.read_bytes() == raw

    @staticmethod
    def test_lone_surrogate_config_left_alone(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg = home / ".claude.json"
        # json.loads 接受孤代理项转义，但 ensure_ascii=False 无法按 UTF-8 编码写回
        raw = b'{"note": "\\ud800"}'
        cfg.write_bytes(raw)
        ensure_workspace_trusted("/p", home=str(home))
        assert cfg.read_bytes() == raw
        assert list(home.iterdir()) == [cfg]

    @staticmethod
    def test_none_cwd_skips_trust_write(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        ensure_workspace_trusted(None, home=str(home))
        assert not (home / ".claude.json").exists()

    @staticmethod
    def test_empty_cwd_skips_trust_write(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        ensure_workspace_trusted("", home=str(home))
        assert not (home / ".claude.json").exists()

    @staticmethod
    def test_symlink_cwd_resolved_to_real_path(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        ensure_workspace_trusted(str(link), home=str(home))
        data = json.loads((home / ".claude.json").read_text())
        assert str(real.resolve()) in data["projects"]

    @staticmethod
    def test_claude_config_dir_relocates_config_file(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        ensure_workspace_trusted(
            str(tmp_path / "proj"), home=str(home), claude_config_dir=str(config_dir)
        )
        assert not (home / ".claude.json").exists()
        data = json.loads((config_dir / ".claude.json").read_text())
        key = str((tmp_path / "proj").resolve())
        assert data["projects"][key]["hasTrustDialogAccepted"] is True

    @staticmethod
    def test_relative_claude_config_dir_resolved_against_workspace(
        tmp_path,
        monkeypatch,
    ):
        # 子进程以 workspace 为 cwd 解析相对 CLAUDE_CONFIG_DIR，预写须同基准，
        # 否则写进守护进程 cwd 侧、子进程读不到（信任预置静默失效）
        home = tmp_path / "home"
        home.mkdir()
        workspace = tmp_path / "proj"
        workspace.mkdir()
        monkeypatch.chdir(tmp_path)
        ensure_workspace_trusted(str(workspace), home=str(home), claude_config_dir="cfg")
        data = json.loads((workspace / "cfg" / ".claude.json").read_text())
        assert data["projects"][str(workspace.resolve())]["hasTrustDialogAccepted"] is True
        assert not (tmp_path / "cfg").exists()


@pytest.mark.asyncio
async def test_query_marks_workspace_trusted_before_spawn(tmp_path, monkeypatch):
    # query() 会合并 os.environ，需剔除外部 CLAUDE_CONFIG_DIR 以免信任写入逃逸出隔离的 home
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".claude.json"

    async def fake_spawn(*args, **kwargs):
        data = json.loads(cfg.read_text())
        assert data["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"] is True
        raise FileNotFoundError("fake")

    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", fake_spawn
    )
    events = []
    async for event in ClaudeCLIDriver(claude_bin="claude").query(
        "prompt", session_id="sid", cwd=str(tmp_path), env={"HOME": str(home)}
    ):
        events.append(event)
    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]
    assert events[0].data["error"] == "command not found: claude"


async def _no_trust_write_query_events(monkeypatch, tmp_path, **query_kwargs) -> list:
    """fake spawn 跑一次 query：断言信任配置从未被预写，返回事件列表。

    spawn 直接抛 FileNotFoundError（模拟 claude 不存在），供「不写信任」的
    各条路径复用；query_kwargs 透传 cwd / pre_trust 等差异参数。
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".claude.json"

    async def fake_spawn(*args, **kwargs):
        assert not cfg.exists()
        raise FileNotFoundError("fake")

    monkeypatch.setattr(
        "workflow_controller.driver.claude_cli.asyncio.create_subprocess_exec", fake_spawn
    )
    events = []
    async for event in ClaudeCLIDriver(claude_bin="claude").query(
        "prompt", session_id="sid", env={"HOME": str(home)}, **query_kwargs
    ):
        events.append(event)
    assert not cfg.exists()
    return events


@pytest.mark.asyncio
async def test_query_without_cwd_skips_trust_write(tmp_path, monkeypatch):
    # cwd=None（auth 探活等调用）不得把守护进程自身 cwd 预写入信任配置
    events = await _no_trust_write_query_events(monkeypatch, tmp_path)
    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]


@pytest.mark.asyncio
async def test_query_empty_cwd_skips_trust_write(tmp_path, monkeypatch):
    # cwd=""（未显式选定 workspace）不得把守护进程自身 cwd 预写入信任配置
    events = await _no_trust_write_query_events(monkeypatch, tmp_path, cwd="")
    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]


@pytest.mark.asyncio
async def test_query_pre_trust_false_skips_trust_write(tmp_path, monkeypatch):
    # 探测等以内部 scratch 目录为 cwd 的调用关闭信任预写
    events = await _no_trust_write_query_events(
        monkeypatch, tmp_path, cwd=str(tmp_path), pre_trust=False
    )
    assert [e.event_type for e in events] == [EventType.ERROR, EventType.ITERATION_END]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
