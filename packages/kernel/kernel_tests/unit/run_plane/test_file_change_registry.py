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
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.core.file_changes import FileChangeRegistry
from workflow_controller.event.event_bus import EventBus
from workflow_controller.event.type_registry import EventTypeRegistry
from workflow_controller.plugin.file_changes import (
    TOOL_EXECUTION_ABORTED,
    FileChangeContext,
    FileChangeOutcome,
    tool_execution_outcome_event,
    tool_will_execute_event,
)
from workflow_controller.session.events import Event, EventType


def context(tmp_path, *, capture_id="c", session_id="s", run_id=None):
    return FileChangeContext(
        capture_id,
        "agent",
        session_id,
        None,
        None,
        "claude_sdk",
        "m",
        None,
        "Write",
        "t",
        tmp_path,
        {},
        datetime.now(tz=UTC),
        {},
        run_id=run_id,
    )


def event_bus(registry: FileChangeRegistry) -> EventBus:
    store = MagicMock()
    store.store = AsyncMock()
    store.store_run_event = AsyncMock()
    bus = EventBus(store_router=store, type_registry=EventTypeRegistry())
    registry.bind(bus)
    return bus


def test_lifecycle_event_payload_is_serializable_and_round_trips(tmp_path):
    capture = context(tmp_path)
    before = tool_will_execute_event(capture)
    serialized = json.loads(json.dumps(before.data))
    restored = FileChangeContext.from_dict(serialized["context"])
    assert restored.capture_id == capture.capture_id
    assert restored.cwd == capture.cwd
    restored_event = Event.from_dict(json.loads(json.dumps(before.to_dict())))
    assert restored_event.event_type is EventType.TOOL_WILL_EXECUTE

    outcome = FileChangeOutcome(
        False, True, {"reason": "cancelled"}, (), datetime.now(tz=UTC), True
    )
    after = tool_execution_outcome_event(capture, outcome)
    assert after.event_type == TOOL_EXECUTION_ABORTED
    serialized_after = json.loads(json.dumps(after.data))
    restored_outcome = FileChangeOutcome.from_dict(serialized_after["outcome"])
    assert restored_outcome.incomplete is True
    restored_after_event = Event.from_dict(json.loads(json.dumps(after.to_dict())))
    assert restored_after_event.event_type is EventType.TOOL_EXECUTION_ABORTED


async def test_registry_is_fail_open_and_unsubscribes(tmp_path):
    registry = FileChangeRegistry()
    bus = event_bus(registry)
    calls = []

    async def broken_before(_):
        raise RuntimeError("boom")

    async def after(_, __):
        calls.append("after")

    token = registry.subscribe(broken_before, after)
    capture = context(tmp_path)
    await bus.emit(tool_will_execute_event(capture))
    await bus.emit(
        tool_execution_outcome_event(
            capture,
            FileChangeOutcome(True, False, None, (), datetime.now(tz=UTC)),
        )
    )
    assert calls == []
    registry.unsubscribe(token)
    assert registry.has_subscribers is False


async def test_inflight_capture_keeps_original_subscriber_after_reload(tmp_path):
    registry = FileChangeRegistry()
    bus = event_bus(registry)
    calls = []

    async def before(_):
        calls.append("old-before")

    async def old_after(_, __):
        calls.append("old-after")

    async def new_after(_, __):
        calls.append("new-after")

    capture = context(tmp_path)
    token = registry.subscribe(before, old_after)
    await bus.emit(tool_will_execute_event(capture))
    registry.unsubscribe(token)
    registry.subscribe(before, new_after)
    await bus.emit(
        tool_execution_outcome_event(
            capture,
            FileChangeOutcome(True, False, None, (), datetime.now(tz=UTC)),
        )
    )

    assert calls == ["old-before", "old-after"]


async def test_registry_with_no_subscriber_is_noop(tmp_path):
    registry = FileChangeRegistry()
    bus = event_bus(registry)
    assert registry.has_subscribers is False
    capture = context(tmp_path)
    await bus.emit(tool_will_execute_event(capture))
    await bus.emit(
        tool_execution_outcome_event(
            capture,
            FileChangeOutcome(True, False, None, (), datetime.now(tz=UTC)),
        )
    )


async def _dispatched_captures(*contexts):
    """顺序发射捕获事件，返回注册表分发给订阅者的上下文列表。"""
    registry = FileChangeRegistry()
    bus = event_bus(registry)
    dispatched = []

    async def before(dispatched_context):
        dispatched.append(dispatched_context)

    async def after(_, __):
        return None

    registry.subscribe(before, after)
    for capture in contexts:
        await bus.emit(tool_will_execute_event(capture))
    return dispatched


async def _collect_dispatched_contexts(tmp_path, first_run_id, second_run_id):
    """起两个并发捕获（同 cwd），返回注册表分发给订阅者的上下文对。"""
    return await _dispatched_captures(
        context(tmp_path, capture_id="c1", session_id="s1", run_id=first_run_id),
        context(tmp_path, capture_id="c2", session_id="s2", run_id=second_run_id),
    )


async def test_same_run_concurrent_captures_attributed_to_run(tmp_path):
    """同 run 并发捕获不互标冲突：归因保留 run 维度，actor 仍记各自 session。"""
    first, second = await _collect_dispatched_contexts(tmp_path, "run-1", "run-1")

    assert "file_change_overlap" not in first.attributes
    assert "file_change_overlap" not in second.attributes
    assert first.run_id == "run-1" and second.run_id == "run-1"
    assert (
        first.workflow_controller_session_id == "s1"
        and second.workflow_controller_session_id == "s2"
    )


async def test_cross_run_concurrent_captures_conflict(tmp_path):
    """跨 run 的并发捕获仍互标冲突。"""
    first, second = await _collect_dispatched_contexts(tmp_path, "run-1", "run-2")

    assert first.attributes["file_change_overlap"] == "true"
    assert second.attributes["file_change_overlap"] == "true"


async def test_unaffiliated_concurrent_captures_conflict(tmp_path):
    """无 run 归属的独立 session 并发捕获仍互标冲突。"""
    first, second = await _collect_dispatched_contexts(tmp_path, None, None)

    assert first.attributes["file_change_overlap"] == "true"
    assert second.attributes["file_change_overlap"] == "true"


async def test_same_run_agent_and_automation_captures_do_not_conflict(tmp_path):
    """同 run 的 agent 与 automation 并发捕获（并行工作流常态）不互标冲突。"""
    automation = FileChangeContext(
        "c1",
        "automation",
        None,
        None,
        None,
        "graph",
        None,
        None,
        "Bash",
        "t",
        tmp_path,
        {},
        datetime.now(tz=UTC),
        {},
        run_id="run-1",
        run_context="node=A",
    )
    agent = context(tmp_path, capture_id="c2", session_id="s1", run_id="run-1")
    dispatched = await _dispatched_captures(automation, agent)

    assert "file_change_overlap" not in dispatched[0].attributes
    assert "file_change_overlap" not in dispatched[1].attributes


async def test_cancelled_after_waits_for_capture_cleanup(tmp_path):
    registry = FileChangeRegistry()
    bus = event_bus(registry)
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def before(_):
        return None

    async def after(_, __):
        entered.set()
        await release.wait()
        completed.set()

    capture = context(tmp_path)
    registry.subscribe(before, after)
    await bus.emit(tool_will_execute_event(capture))
    task = asyncio.create_task(
        bus.emit(
            tool_execution_outcome_event(
                capture,
                FileChangeOutcome(True, False, None, (), datetime.now(tz=UTC)),
            )
        )
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
