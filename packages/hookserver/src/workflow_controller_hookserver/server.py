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

"""独立发行的 Hook Server（最小面）：与仓内 ``workflow_controller.hooks.server``
刻意保持结构差异（本文件表驱动装配，仓内版保持显式展开），两边禁止互相 import。
"""

from __future__ import annotations

__all__ = ["HookServer"]

from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class HookServer:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 18765,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stop_request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
        | None = None,
        on_pre_tool_use_request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> None:
        self.port = port
        self.host = host
        self.on_event = on_event
        self.on_stop_request = on_stop_request
        self.on_pre_tool_use_request = on_pre_tool_use_request
        self.app = web.Application()
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    def register_route(self, method: str, path: str, handler: Any) -> None:
        self.app.router.add_route(method, path, handler)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop_server(self) -> None:
        if self._runner:
            await self._runner.shutdown()
            await self._runner.cleanup()
            self._runner = None

    def _setup_routes(self) -> None:
        routes = (
            ("/hooks/post-tool-use", self._handle_post_tool_use),
            ("/hooks/pre-tool-use", self._handle_pre_tool_use),
            ("/hooks/stop", self._handle_stop),
            ("/hooks/notification", self._handle_notification),
            ("/hooks/session-start", self._handle_session_start),
            ("/hooks/session-end", self._handle_session_end),
        )
        for path, handler in routes:
            self.app.router.add_post(path, handler)

    async def _handle_post_tool_use(self, request: web.Request) -> web.Response:
        data = await request.json()
        await self._emit_event(data, "tool_result")
        return web.json_response({"status": "ok"})

    async def _handle_pre_tool_use(self, request: web.Request) -> web.Response:
        """pre-tool-use 决策口：回调可改写响应，缺省放行。"""
        data = await request.json()
        await self._emit_event(data, "tool_use")
        decision = (
            await self.on_pre_tool_use_request(data.get("session_id", "unknown"), data)
            if self.on_pre_tool_use_request
            else None
        )
        return web.json_response(decision or {"status": "ok"})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        data = await request.json()
        session_id = data.get("session_id", "unknown")

        if self.on_stop_request:
            decision = await self.on_stop_request(session_id, data)
            if decision:
                return web.json_response(decision)

        return web.json_response({"decision": "allow"})

    async def _handle_notification(self, request: web.Request) -> web.Response:
        data = await request.json()
        await self._emit_event(data, "system_message")
        return web.json_response({"status": "ok"})

    async def _handle_session_start(self, request: web.Request) -> web.Response:
        data = await request.json()
        await self._emit_event(data, "system_message")
        return web.json_response({"status": "ok"})

    async def _handle_session_end(self, request: web.Request) -> web.Response:
        data = await request.json()
        await self._emit_event(data, "system_message")
        return web.json_response({"status": "ok"})

    async def _emit_event(self, data: dict[str, Any], event_type: str) -> None:
        if self.on_event is None:
            return
        session_id = data.get("session_id", data.get("session", "unknown"))
        tool_name = data.get("tool_name", "")
        await self.on_event(
            {
                "session_id": session_id,
                "event_type": event_type,
                "iteration": 0,
                "data": {"source": "http_hook", "tool_name": tool_name, **data},
            }
        )
