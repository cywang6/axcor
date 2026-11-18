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
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.event.event_bus import EventBus, InterceptResult
from workflow_controller.event.type_registry import EventTypeRegistry
from workflow_controller.session.events import Event, EventType


def _bus() -> EventBus:
    store_router = MagicMock()
    store_router.store = AsyncMock()
    return EventBus(store_router=store_router, type_registry=EventTypeRegistry())


def _event() -> Event:
    return Event.create(
        session_id="session-1",
        iteration=0,
        event_type="test_awaited_observer",  # type: ignore[arg-type]
        data={},
    )


async def test_emit_waits_for_awaited_observer() -> None:
    bus = _bus()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: Event) -> None:
        entered.set()
        await release.wait()

    bus.observe_awaited("test_awaited_observer", handler)
    emit_task = asyncio.create_task(bus.emit(_event()))

    await entered.wait()
    assert not emit_task.done()

    release.set()
    assert await emit_task is None


async def test_awaited_observers_run_in_registration_order() -> None:
    bus = _bus()
    calls: list[str] = []

    async def first(event: Event) -> None:
        calls.append("first")

    async def second(event: Event) -> None:
        calls.append("second")

    bus.observe_awaited("test_awaited_observer", first)
    bus.observe_awaited("test_awaited_observer", second)

    await bus.emit(_event())

    assert calls == ["first", "second"]


async def test_awaited_wildcard_observer_runs_after_typed_observers() -> None:
    bus = _bus()
    calls: list[str] = []

    async def typed(event: Event) -> None:
        calls.append("typed")

    async def wildcard(event: Event) -> None:
        calls.append("wildcard")

    bus.observe_all_awaited(wildcard)
    bus.observe_awaited("test_awaited_observer", typed)

    await bus.emit(_event())

    assert calls == ["typed", "wildcard"]


async def test_awaited_observer_exception_does_not_stop_broadcast() -> None:
    bus = _bus()
    calls: list[str] = []

    async def broken(event: Event) -> None:
        calls.append("broken")
        raise RuntimeError("boom")

    async def healthy(event: Event) -> None:
        calls.append("healthy")

    bus.observe_awaited("test_awaited_observer", broken)
    bus.observe_awaited("test_awaited_observer", healthy)

    await bus.emit(_event())

    assert calls == ["broken", "healthy"]


async def test_awaited_observer_runs_after_interceptor_decision() -> None:
    bus = _bus()
    calls: list[str] = []

    async def interceptor(event: Event) -> InterceptResult:
        calls.append("interceptor")
        return InterceptResult(action="block")

    async def observer(event: Event) -> None:
        calls.append("observer")

    bus.intercept("test_awaited_observer", interceptor)
    bus.observe_awaited("test_awaited_observer", observer)

    result = await bus.emit(_event())

    assert result is not None and result.action == "block"
    assert calls == ["interceptor", "observer"]


async def test_detach_awaited_observer() -> None:
    bus = _bus()
    handler = AsyncMock()
    bus.observe_awaited("test_awaited_observer", handler)

    bus.detach_awaited_observer("test_awaited_observer", handler)
    await bus.emit(_event())

    handler.assert_not_awaited()


async def test_detach_awaited_wildcard_observer() -> None:
    bus = _bus()
    handler = AsyncMock()
    bus.observe_all_awaited(handler)

    bus.detach_awaited_observer("*", handler)
    await bus.emit(_event())

    handler.assert_not_awaited()


async def test_awaited_observer_does_not_swallow_cancellation() -> None:
    bus = _bus()

    async def cancelled(event: Event) -> None:
        raise asyncio.CancelledError

    bus.observe_awaited("test_awaited_observer", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await bus.emit(_event())


def _stamped_bus():
    import tempfile
    from dataclasses import dataclass
    from pathlib import Path

    from workflow_controller.store.event_router import EventStoreRouter

    @dataclass(frozen=True)
    class _Aff:
        run_id: str
        role: str = "executor"
        context: str | None = None

    router = EventStoreRouter(default_project_dir=Path(tempfile.mkdtemp()))
    router.set_run_stamper(
        lambda sid: _Aff("run-1", context="node=s3") if sid == "sess-1" else None
    )
    bus = EventBus(store_router=router, type_registry=EventTypeRegistry())
    return bus, router


async def test_emit_stamps_run_dimensions_from_affiliation() -> None:
    bus, router = _stamped_bus()
    seen: list[Event] = []

    async def observer(event: Event) -> None:
        seen.append(event)

    bus.observe_awaited(EventType.NOTIFICATION, observer)
    await bus.emit(Event.create("sess-1", 0, EventType.NOTIFICATION, {"k": 1}))

    assert seen[0].run_id == "run-1" and seen[0].context == "node=s3"
    _, events = router.get_events_since_cursor("0")
    assert events[0].run_id == "run-1" and events[0].context == "node=s3"


async def test_emit_without_affiliation_stays_unstamped() -> None:
    bus, router = _stamped_bus()
    await bus.emit(Event.create("sess-unknown", 0, EventType.NOTIFICATION))

    _, events = router.get_events_since_cursor("0")
    assert events[0].run_id is None and events[0].context is None


async def test_run_key_event_dispatches_to_run_events() -> None:
    """run 键事件（有 run_id 无 session 身份）落 run_events，不落 events。"""
    bus, router = _stamped_bus()
    event = Event.create("", 0, EventType.LOG_AUTO, {"x": 1})
    event.run_id = "run-9"
    await bus.emit(event)

    assert router.get_recent(10) == []
    assert getattr(router, "_store")().get_run_events_sync("run-9")[0]["data"] == {"x": 1}


async def test_stop_cancels_and_drains_processor() -> None:
    """stop() 必须排空处理器任务：只 cancel 不 await 会留下裸死的派发协程。"""
    bus = _bus()
    bus.start()
    processor = getattr(bus, "_processor")
    assert processor is not None
    await bus.stop()
    assert getattr(bus, "_processor") is None
    assert processor.done()
    with pytest.raises(asyncio.CancelledError):
        processor.result()
    await bus.stop()  # 幂等


async def test_stop_is_noop_when_never_started() -> None:
    bus = _bus()
    await bus.stop()
    assert getattr(bus, "_processor") is None
