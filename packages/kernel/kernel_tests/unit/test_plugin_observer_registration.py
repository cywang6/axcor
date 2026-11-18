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

"""Plugin observer self-registration via _register_observers override.

Symmetric counterpart of test_plugin_interceptor_registration.py. The central
dispatcher (``orchestrator._dispatch_plugin_observer_hooks``) is gone — a
plugin that wants to observe **explicitly** overrides ``_register_observers``
and subscribes its own async handler on the typed event bus. There are no
legacy sync ``on_*`` observer methods and no base translation helpers: the
plugin owns its async handler directly. The default ``install`` body captures
``_ctx`` and dispatches the helper, so subclasses need not override install.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from workflow_controller.event.event_bus import EventBus
from workflow_controller.event.type_registry import EventTypeRegistry
from workflow_controller.plugin.base import Plugin, PluginConfig
from workflow_controller.plugin.context import PluginContext
from workflow_controller.session.events import Event, EventType


def _ctx_with_bus() -> tuple[PluginContext, EventBus]:
    store_router = MagicMock()
    store_router.store = AsyncMock()
    bus = EventBus(store_router=store_router, type_registry=EventTypeRegistry())
    ctx = PluginContext(
        bus=bus,
        type_registry=EventTypeRegistry(),
        hook_server=MagicMock(),
    )
    return ctx, bus


def _cfg() -> PluginConfig:
    return PluginConfig(name="t", version="0", enabled=True)


class _Base(Plugin):
    """Abstract lifecycle only; inherits install (default: _ctx + helpers)."""

    def uninstall(self) -> None:
        pass


class _PostToolPlugin(_Base):
    """Observes TOOL_RESULT with a plugin-owned async handler."""

    def __init__(self, cfg, path):
        super().__init__(cfg, path)
        self.calls: list[Event] = []

    async def _on_tool_result(self, event: Event) -> None:
        self.calls.append(event)

    def _register_observers(self, ctx) -> None:
        ctx.bus.observe(EventType.TOOL_RESULT, self._on_tool_result)


def _event(event_type, session_id="s1", **data) -> Event:
    return Event.create(
        session_id=session_id,
        iteration=0,
        event_type=event_type,
        data=data,
    )


# -- registration (mirrors test_plugin_interceptor_registration) --


def test_post_tool_explicit_register():
    ctx, bus = _ctx_with_bus()
    p = _PostToolPlugin(_cfg(), Path("."))
    p.install(ctx)
    assert getattr(bus, "_observers")[EventType.TOOL_RESULT.value] == [
        getattr(p, "_on_tool_result")
    ]
    assert not getattr(bus, "_observers").get(EventType.NOTIFICATION.value)
    assert not getattr(bus, "_interceptors")  # interceptors untouched


def test_default_install_is_noop():
    """A plugin whose _register_observers is the default no-op registers nothing."""
    ctx, bus = _ctx_with_bus()
    _Base(_cfg(), Path(".")).install(ctx)
    assert not getattr(bus, "_observers")


def test_undefined_handler_is_not_auto_registered():
    """Defining an async handler method but not wiring it registers nothing.

    No central dispatcher, no reflection — only an explicit
    _register_observers override subscribes an observer.
    """
    ctx, bus = _ctx_with_bus()

    class _Silent(_Base):
        async def _on_tool_result(self, event):  # defined but not declared
            ...

    _Silent(_cfg(), Path(".")).install(ctx)
    assert not getattr(bus, "_observers")


def test_install_wires_both_interceptors_and_observers():
    """install dispatches both helper hooks; both are reached."""
    ctx, bus = _ctx_with_bus()

    class _Both(_PostToolPlugin):
        def _register_interceptors(self, ctx) -> None:
            ctx.bus.intercept("stop_requested", self._on_stop_intercept, priority=10)

    p = _Both(_cfg(), Path("."))
    p.install(ctx)  # inherited default → _ctx + _register_interceptors + _register_observers
    assert getattr(bus, "_observers")[EventType.TOOL_RESULT.value] == [
        getattr(p, "_on_tool_result")
    ]
    assert len(getattr(bus, "_interceptors").get("stop_requested", [])) == 1


# -- typed delivery through the bus (deterministic, no background task) --


def test_tool_result_handler_invoked_with_event():
    p = _PostToolPlugin(_cfg(), Path("."))
    asyncio.run(getattr(p, "_on_tool_result")(_event(EventType.TOOL_RESULT, tool_name="Edit")))
    assert len(p.calls) == 1
    assert p.calls[0].data["tool_name"] == "Edit"


async def test_observer_exception_swallowed_by_bus():
    """EventBus._process_observers swallows handler exceptions (best-effort,
    matching the old central dispatcher). A raising handler doesn't crash it.
    """
    ctx, bus = _ctx_with_bus()
    bus.start()
    try:

        class _Boom(_Base):
            async def _on_tool_result(self, event):
                raise RuntimeError("boom")

            def _register_observers(self, ctx):
                ctx.bus.observe(EventType.TOOL_RESULT, self._on_tool_result)

        _Boom(_cfg(), Path(".")).install(ctx)
        await bus.emit(_event(EventType.TOOL_RESULT))
        await asyncio.sleep(0.01)  # let the background processor run
    finally:
        await bus.stop()
