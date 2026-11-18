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

import pytest

from workflow_controller.driver.claude_sdk import ClaudeSDKDriver
from workflow_controller.driver.hooks import DriverHooks

# ── SDK adapter: native SDK hook payload -> orchestrator handler -> back ──


@pytest.mark.asyncio
async def test_sdk_pre_tool_hook_forwards_to_handler():
    captured: dict = {}

    async def on_pre(sid: str, data: dict) -> dict:
        captured["sid"] = sid
        captured["data"] = data
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "选 A",
            }
        }

    hooks = DriverHooks(session_id="mig-1", on_pre_tool_use=on_pre)
    d = ClaudeSDKDriver()
    sdk_hooks = getattr(d, "_build_sdk_hooks")(hooks)

    assert "PreToolUse" in sdk_hooks
    matcher = sdk_hooks["PreToolUse"][0]
    assert matcher.matcher == "AskUserQuestion"
    cb = matcher.hooks[0]
    out = await cb(
        {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
            "session_id": "native-child",
            "parent_session_id": "native-parent",
            "parent_tool_use_id": "task-1",
            "agent_id": "agent-1",
            "agent_type": "researcher",
        },
        "tu-1",
        None,
    )
    assert captured["sid"] == "mig-1"
    assert captured["data"]["tool_name"] == "AskUserQuestion"
    assert captured["data"]["native_session_id"] == "native-child"
    assert captured["data"]["parent_session_id"] == "native-parent"
    assert captured["data"]["agent_id"] == "agent-1"
    assert captured["data"]["agent_type"] == "researcher"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_sdk_stop_hook_forwards_last_assistant_text(monkeypatch):
    captured: dict = {}

    async def on_stop(sid: str, data: dict) -> dict:
        captured["data"] = data
        return {"decision": "block", "reason": "继续"}

    hooks = DriverHooks(session_id="mig-1", on_stop=on_stop)
    d = ClaudeSDKDriver()
    monkeypatch.setattr(d, "_last_assistant_text", "done analysis")
    sdk_hooks = getattr(d, "_build_sdk_hooks")(hooks)

    cb = sdk_hooks["Stop"][0].hooks[0]
    out = await cb({}, None, None)
    assert captured["data"]["last_assistant_message"] == "done analysis"
    assert out["decision"] == "block"


def test_sdk_hooks_omits_unset_callbacks():
    d = ClaudeSDKDriver()
    assert getattr(d, "_build_sdk_hooks")(DriverHooks(session_id="x")) == {}


# ── orchestrator wiring: make_driver_hooks routes to the reviewer core ──


def test_make_driver_hooks_routes_to_decide(tmp_path):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    dh = orch.make_driver_hooks("mig-9")
    assert dh.session_id == "mig-9"
    assert dh.on_pre_tool_use is not None and dh.on_stop is not None


@pytest.mark.asyncio
async def test_make_driver_hooks_pre_tool_routes(tmp_path, monkeypatch):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    seen: dict = {}

    async def fake_decide_pre(mig_id, data):
        seen.update(mig_id=mig_id, data=data)
        return {"ok": True}

    monkeypatch.setattr(orch, "_decide_pre_tool_use", fake_decide_pre)
    dh = orch.make_driver_hooks("mig-9")
    out = await dh.on_pre_tool_use("mig-9", {"tool_name": "AskUserQuestion"})
    assert seen["mig_id"] == "mig-9"
    assert out == {"ok": True}


@pytest.mark.asyncio
async def test_make_driver_hooks_stop_passes_final_text(tmp_path, monkeypatch):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    seen: dict = {}

    async def fake_decide_stop(mig_id, final_text, *, block_key):
        seen.update(mig_id=mig_id, final_text=final_text, block_key=block_key)
        return None

    monkeypatch.setattr(orch, "_decide_stop", fake_decide_stop)
    dh = orch.make_driver_hooks("mig-9")
    await dh.on_stop("mig-9", {"last_assistant_message": "spec done"})
    assert seen == {"mig_id": "mig-9", "final_text": "spec done", "block_key": "mig-9"}


# ── subagent hooks: SDK adapter registers + forwards ──


@pytest.mark.asyncio
async def test_sdk_registers_and_forwards_subagent_hooks():
    seen: dict = {}

    async def on_start(sid, data):
        seen["start"] = (sid, data)

    async def on_stop(sid, data):
        seen["stop"] = (sid, data)

    hooks = DriverHooks(session_id="mig-1", on_subagent_start=on_start, on_subagent_stop=on_stop)
    d = ClaudeSDKDriver()
    sdk_hooks = getattr(d, "_build_sdk_hooks")(hooks)

    assert "SubagentStart" in sdk_hooks and "SubagentStop" in sdk_hooks

    start_cb = sdk_hooks["SubagentStart"][0].hooks[0]
    await start_cb({"agent_id": "a1", "agent_type": "a2h-android-analyzer"}, None, None)
    assert seen["start"][0] == "mig-1"
    assert seen["start"][1]["agent_type"] == "a2h-android-analyzer"

    stop_cb = sdk_hooks["SubagentStop"][0].hooks[0]
    await stop_cb(
        {"agent_id": "a1", "agent_type": "x", "agent_transcript_path": "/no/such.jsonl"}, None, None
    )
    # Boundary normalization: the transcript path is consumed at the cb and
    # forwarded as content/total_tokens (missing path -> 0 tokens, "" content).
    assert seen["stop"][1]["agent_id"] == "a1"
    assert "agent_transcript_path" not in seen["stop"][1]
    assert seen["stop"][1]["total_tokens"] == 0
    assert seen["stop"][1]["content"] == ""


@pytest.mark.asyncio
async def test_sdk_subagent_stop_cb_normalizes_transcript(tmp_path):
    """claude_sdk boundary: _subagent_stop_cb reads the SDK payload's
    agent_transcript_path, summarizes it (tokens + truncated content), and
    forwards the same {content, total_tokens} shape SubagentTracker.on_stop expects.
    """
    seen: dict = {}

    async def on_stop(sid, data):
        seen["stop"] = (sid, data)

    hooks = DriverHooks(session_id="mig-1", on_subagent_stop=on_stop)
    d = ClaudeSDKDriver()
    sdk_hooks = getattr(d, "_build_sdk_hooks")(hooks)
    stop_cb = sdk_hooks["SubagentStop"][0].hooks[0]

    tr = tmp_path / "tr.jsonl"
    tr.write_text(
        '{"message":{"content":[{"type":"text","text":"subagent done"}],'
        '"usage":{"output_tokens":55}}}\n',
        encoding="utf-8",
    )
    await stop_cb(
        {"agent_id": "a1", "agent_type": "researcher", "agent_transcript_path": str(tr)},
        None,
        None,
    )
    assert seen["stop"][0] == "mig-1"
    payload = seen["stop"][1]
    assert payload["agent_id"] == "a1"
    assert payload["total_tokens"] == 55
    assert "subagent done" in payload["content"]
    # path is not forwarded — consumed at the boundary
    assert "agent_transcript_path" not in payload


def test_sdk_omits_subagent_hooks_when_unset():
    d = ClaudeSDKDriver()
    sdk_hooks = getattr(d, "_build_sdk_hooks")(DriverHooks(session_id="x"))
    assert "SubagentStart" not in sdk_hooks and "SubagentStop" not in sdk_hooks


# ── orchestrator: in-flight subagent tracking ──


@pytest.mark.asyncio
async def test_orchestrator_tracks_in_flight_subagents(tmp_path):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    dh = orch.make_driver_hooks("mig-9")
    assert dh.on_subagent_start is not None and dh.on_subagent_stop is not None

    await getattr(orch, "_subagents").on_start(
        "mig-9", {"agent_id": "a1", "agent_type": "analyzer"}
    )
    await getattr(orch, "_subagents").on_start(
        "mig-9", {"agent_id": "a2", "agent_type": "analyzer"}
    )
    assert set(getattr(getattr(orch, "_subagents"), "_status")["mig-9"]) == {"a1", "a2"}

    await getattr(orch, "_subagents").on_stop("mig-9", {"agent_id": "a1"})
    assert set(getattr(getattr(orch, "_subagents"), "_status")["mig-9"]) == {"a2"}


def test_hook_config_includes_subagent():
    from workflow_controller.hooks.hook_config import HookConfigGenerator

    s = HookConfigGenerator().generate_settings()
    assert "SubagentStart" in s["hooks"] and "SubagentStop" in s["hooks"]
    assert s["hooks"]["SubagentStop"][0]["hooks"][0]["url"].endswith("/hooks/subagent-stop")


# ── CLI hook server: subagent endpoints ──


@pytest.mark.asyncio
async def test_hook_server_subagent_start_fires_callback():
    from aiohttp.test_utils import TestClient, TestServer

    from workflow_controller.hooks.server import HookServer

    seen: dict = {}

    async def on_start(session_id, data):
        seen["session_id"] = session_id
        seen["data"] = data

    server = HookServer(port=0, on_subagent_start_request=on_start)
    await server.start()

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-start",
            json={
                "session_id": "cli-uuid-1",
                "agent_id": "a1",
                "agent_type": "a2h-android-analyzer",
            },
        )
        assert resp.status == 200

    await server.stop_server()
    assert seen["session_id"] == "cli-uuid-1"
    assert seen["data"]["agent_id"] == "a1"
    assert seen["data"]["agent_type"] == "a2h-android-analyzer"


@pytest.mark.asyncio
async def test_hook_server_subagent_stop_fires_callback():
    from aiohttp.test_utils import TestClient, TestServer

    from workflow_controller.hooks.server import HookServer

    seen: dict = {}

    async def on_stop(session_id, data):
        seen["session_id"] = session_id
        seen["data"] = data

    server = HookServer(port=0, on_subagent_stop_request=on_stop)
    await server.start()

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-stop",
            json={
                "session_id": "cli-uuid-2",
                "agent_id": "a1",
                "agent_type": "analyzer",
                "agent_transcript_path": "/tmp/t.jsonl",
            },
        )
        assert resp.status == 200

    await server.stop_server()
    assert seen["session_id"] == "cli-uuid-2"
    assert seen["data"]["agent_id"] == "a1"
    assert seen["data"]["agent_transcript_path"] == "/tmp/t.jsonl"


@pytest.mark.asyncio
async def test_hook_server_subagent_no_callback_is_noop():
    from aiohttp.test_utils import TestClient, TestServer

    from workflow_controller.hooks.server import HookServer

    server = HookServer(port=0)
    await server.start()

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-start",
            json={"session_id": "x"},
        )
        assert resp.status == 200

        resp = await client.post(
            "/hooks/subagent-stop",
            json={"session_id": "x"},
        )
        assert resp.status == 200

    await server.stop_server()


# ── Phase 2: precise wake + reviewer status injection ──


def test_loop_wake_retry_sets_event():
    from unittest.mock import MagicMock

    from workflow_controller.core.loop import SingleLoopEngine

    engine = SingleLoopEngine(event_router=MagicMock(), bus=MagicMock())
    assert not getattr(engine, "_retry_wake_event").is_set()
    engine.wake_retry()
    assert getattr(engine, "_retry_wake_event").is_set()


@pytest.mark.asyncio
async def test_subagent_stop_wakes_waiting_retry(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.session.session import SessionState

    orch = Orchestrator.create_default(config_dir=tmp_path)
    getattr(getattr(orch, "_subagents"), "_status")["mig-9"] = {"a1": {"agent_type": "analyzer"}}
    sess = MagicMock(state=SessionState.WAITING_RETRY)
    monkeypatch.setattr(orch.pool, "get", lambda sid: sess)
    engine = MagicMock()
    engine.is_current.return_value = True
    monkeypatch.setattr(orch, "_single_loop", engine)

    await getattr(orch, "_subagents").on_stop("mig-9", {"agent_id": "a1"})
    engine.wake_retry.assert_called_once()


@pytest.mark.asyncio
async def test_subagent_stop_no_wake_when_in_flight_remain(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.session.session import SessionState

    orch = Orchestrator.create_default(config_dir=tmp_path)
    getattr(getattr(orch, "_subagents"), "_status")["mig-9"] = {
        "a1": {"agent_type": "x"},
        "a2": {"agent_type": "y"},
    }
    monkeypatch.setattr(orch.pool, "get", lambda sid: MagicMock(state=SessionState.WAITING_RETRY))
    engine = MagicMock()
    monkeypatch.setattr(orch, "_single_loop", engine)

    await getattr(orch, "_subagents").on_stop("mig-9", {"agent_id": "a1"})  # a2 still in flight
    engine.wake_retry.assert_not_called()


# ── Phase 3: transcript summary + SUBAGENT_STOP event emission ──


def test_summarize_transcript_tokens_and_text(tmp_path):
    from workflow_controller.core._subagent import summarize_subagent_transcript

    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}],"usage":{"output_tokens":10}}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"world"}],"usage":{"output_tokens":5}}}\n'
        "not-json-line\n",
        encoding="utf-8",
    )
    tokens, content = summarize_subagent_transcript(str(p))
    assert tokens == 15
    assert "hello" in content and "world" in content


def test_summarize_transcript_missing_or_empty():
    from workflow_controller.core._subagent import summarize_subagent_transcript

    assert summarize_subagent_transcript("/no/such/file.jsonl") == (0, "")
    assert summarize_subagent_transcript("") == (0, "")


def test_summarize_transcript_truncates(tmp_path):
    from workflow_controller.core._subagent import summarize_subagent_transcript

    big = "x" * 5000
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"message":{"content":[{"type":"text","text":"'
        + big
        + '"}],"usage":{"output_tokens":1}}}\n',
        encoding="utf-8",
    )
    _, content = summarize_subagent_transcript(str(p), max_chars=2000)
    assert len(content) <= 2001  # 2000 chars + ellipsis


def test_truncate_subagent_content():
    """Shared truncation helper: short text untouched, long text capped with an
    ellipsis. Used by both the transcript path (cc) and the inline-content path
    (opencode) so both runtimes cap subagent display text identically.
    """
    from workflow_controller.core._subagent import truncate_subagent_content

    assert truncate_subagent_content("short") == "short"
    out = truncate_subagent_content("y" * 3000)
    assert len(out) == 2001  # default 2000-char cap + ellipsis
    assert out.endswith("…")
    assert truncate_subagent_content("y" * 50, max_chars=10) == "yyyyyyyyyy…"


@pytest.mark.asyncio
async def test_subagent_stop_request_normalizes_transcript(tmp_path):
    """CLI boundary: _handle_subagent_stop_request reads the cc hook body's
    agent_transcript_path, summarizes it (tokens + truncated content), and
    fills content/total_tokens so SubagentTracker.on_stop sees the normalized shape.
    on_stop itself no longer reads transcript paths.
    """
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    getattr(orch, "_cli_to_workflow_controller_session")["cli-uuid"] = "mig-9"
    getattr(getattr(orch, "_subagents"), "_status")["mig-9"] = {"a1": {"agent_type": "analyzer"}}
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]

    tr = tmp_path / "tr.jsonl"
    tr.write_text(
        '{"message":{"content":[{"type":"text","text":"done"}],"usage":{"output_tokens":42}}}\n',
        encoding="utf-8",
    )
    await getattr(orch, "_handle_subagent_stop_request")(
        "cli-uuid",
        {"agent_id": "a1", "agent_type": "analyzer", "agent_transcript_path": str(tr)},
    )
    ev = captured["event"]
    assert ev.event_type.value == "subagent_stop"
    assert "phase" not in ev.data
    assert ev.data["total_tokens"] == 42
    assert "done" in ev.data["content"]


@pytest.mark.asyncio
async def test_subagent_stop_uses_inline_content(tmp_path):
    """opencode has the subagent's return text inline (no transcript file), so
    the driver passes ``content`` directly. The orchestrator surfaces it and the
    driver-supplied ``total_tokens`` without reading any path. cc keeps using
    ``agent_transcript_path`` (covered by test_subagent_stop_request_normalizes_transcript).
    """
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]

    await getattr(orch, "_subagents").on_stop(
        "mig-9",
        {
            "agent_id": "a1",
            "agent_type": "researcher",
            "content": "the subagent report",
            "total_tokens": 99,
        },
    )
    ev = captured["event"]
    assert ev.event_type.value == "subagent_stop"
    assert "phase" not in ev.data
    assert ev.data["content"] == "the subagent report"
    assert ev.data["total_tokens"] == 99


@pytest.mark.asyncio
async def test_subagent_stop_passes_content_through(tmp_path):
    """SubagentTracker.on_stop is a pure consumer: it surfaces the boundary-supplied
    content verbatim. Truncation now lives at each driver/hook boundary
    (opencode _dispatch_hook / cc summarize_subagent_transcript), not here.
    """
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]

    await getattr(orch, "_subagents").on_stop(
        "mig-9",
        {"agent_id": "a1", "content": "z" * 5000, "total_tokens": 0},
    )
    content = captured["event"].data["content"]
    assert content == "z" * 5000  # full length, not truncated at the consumer


@pytest.mark.asyncio
async def test_subagent_start_emits_event(tmp_path):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]
    await getattr(orch, "_subagents").on_start(
        "mig-9", {"agent_id": "a1", "agent_type": "analyzer"}
    )
    ev = captured["event"]
    assert ev.event_type.value == "subagent_start"
    assert ev.data["agent_type"] == "analyzer"
    assert "phase" not in ev.data
    # 无 parent_agent_id 的实时事件（claude_sdk）：透传为空串，前端挂 main
    assert ev.data["parent_agent_id"] == ""


@pytest.mark.asyncio
async def test_subagent_start_passes_parent_agent_id(tmp_path):
    """opencode 轮询器提供的 parent_agent_id 原样透传进 SUBAGENT_START——
    前端拓扑据此把孙 agent 挂到派发者之下。
    """
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]
    await getattr(orch, "_subagents").on_start(
        "mig-9",
        {"agent_id": "ses_grand", "agent_type": "explore", "parent_agent_id": "ses_child"},
    )
    ev = captured["event"]
    assert ev.event_type.value == "subagent_start"
    assert ev.data["parent_agent_id"] == "ses_child"


# ── CLI hook server: end-to-end orchestrator integration ──


async def _cli_hook_capture_env(tmp_path, cli_uuid: str) -> tuple:
    """起一个映射了 cli 会话的 orchestrator 与 HookServer，并捕获 bus 事件。

    返回 (orch, captured, server)；调用方负责 server.stop_server()。
    """
    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.hooks.server import HookServer

    orch = Orchestrator.create_default(config_dir=tmp_path)
    getattr(orch, "_cli_to_workflow_controller_session")[cli_uuid] = "mig-9"
    captured: dict = {}

    async def fake_submit(event):
        captured["event"] = event

    orch.bus.emit = fake_submit  # type: ignore[method-assign]

    server = HookServer(
        port=0,
        on_subagent_start_request=getattr(orch, "_handle_subagent_start_request"),
        on_subagent_stop_request=getattr(orch, "_handle_subagent_stop_request"),
    )
    await server.start()
    return orch, captured, server


@pytest.mark.asyncio
async def test_cli_hook_to_orchestrator_subagent_start(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    orch, captured, server = await _cli_hook_capture_env(tmp_path, "cli-uuid-aa")

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-start",
            json={
                "session_id": "cli-uuid-aa",
                "agent_id": "a1",
                "agent_type": "analyzer",
            },
        )
        assert resp.status == 200

    await server.stop_server()
    assert "mig-9" in getattr(getattr(orch, "_subagents"), "_status")
    assert set(getattr(getattr(orch, "_subagents"), "_status")["mig-9"]) == {"a1"}
    ev = captured["event"]
    assert ev.event_type.value == "subagent_start"
    assert "phase" not in ev.data
    assert ev.data["agent_type"] == "analyzer"


@pytest.mark.asyncio
async def test_cli_hook_to_orchestrator_subagent_stop(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    orch, captured, server = await _cli_hook_capture_env(tmp_path, "cli-uuid-bb")
    getattr(getattr(orch, "_subagents"), "_status")["mig-9"] = {"a1": {"agent_type": "analyzer"}}

    tr = tmp_path / "tr.jsonl"
    tr.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"分析完成"}],'
        '"usage":{"output_tokens":77}}}\n',
        encoding="utf-8",
    )

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-stop",
            json={
                "session_id": "cli-uuid-bb",
                "agent_id": "a1",
                "agent_type": "analyzer",
                "agent_transcript_path": str(tr),
            },
        )
        assert resp.status == 200

    await server.stop_server()
    assert getattr(getattr(orch, "_subagents"), "_status")["mig-9"] == {}
    ev = captured["event"]
    assert ev.event_type.value == "subagent_stop"
    assert "phase" not in ev.data
    assert ev.data["total_tokens"] == 77
    assert "分析完成" in ev.data["content"]


@pytest.mark.asyncio
async def test_cli_hook_unknown_session_noop(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.hooks.server import HookServer

    orch = Orchestrator.create_default(config_dir=tmp_path)

    server = HookServer(
        port=0,
        on_subagent_start_request=getattr(orch, "_handle_subagent_start_request"),
        on_subagent_stop_request=getattr(orch, "_handle_subagent_stop_request"),
    )
    await server.start()

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/hooks/subagent-start",
            json={"session_id": "unknown-uuid", "agent_id": "x"},
        )
        assert resp.status == 200

        resp = await client.post(
            "/hooks/subagent-stop",
            json={"session_id": "unknown-uuid", "agent_id": "x"},
        )
        assert resp.status == 200

    await server.stop_server()
    assert not getattr(getattr(orch, "_subagents"), "_status")


# ── Event store integration: subagent events stored + retrievable ──


@pytest.mark.asyncio
async def test_subagent_event_stored_and_retrieved(tmp_path):
    from workflow_controller.core.orchestrator import Orchestrator

    orch = Orchestrator.create_default(config_dir=tmp_path)
    orch.bus.start()
    max_ts, _ = orch.event_router.get_events_since("")
    await asyncio.sleep(0.05)

    await getattr(orch, "_subagents").on_start(
        "mig-9", {"agent_id": "a1", "agent_type": "analyzer"}
    )
    await getattr(orch, "_subagents").on_start(
        "mig-9", {"agent_id": "a2", "agent_type": "analyzer"}
    )

    tr = tmp_path / "t.jsonl"
    tr.write_text(
        '{"message":{"content":[{"type":"text","text":"done"}],"usage":{"output_tokens":10}}}\n',
        encoding="utf-8",
    )
    await getattr(orch, "_subagents").on_stop(
        "mig-9",
        {"agent_id": "a1", "agent_type": "analyzer", "agent_transcript_path": str(tr)},
    )

    await asyncio.sleep(0.15)
    await orch.bus.stop()

    _, events = orch.event_router.get_events_since(max_ts)
    types = [e.event_type.value for e in events if hasattr(e.event_type, "value")]
    assert types.count("subagent_start") == 2
    assert types.count("subagent_stop") == 1
