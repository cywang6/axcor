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

from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from workflow_controller.hooks.server import HookServer


class TestHookServer:
    async def test_post_tool_use_preserves_id_in_tool_call(self):
        received_events: list[dict[str, Any]] = []

        async def on_event(data: dict[str, Any]):
            received_events.append(data)

        server = HookServer(port=0, on_event=on_event)
        await server.start()
        async with TestClient(TestServer(server.app)) as client:
            for path, tool_id in [
                ("/hooks/post-tool-use", "call-ok"),
                ("/hooks/post-tool-use-failure", "call-error"),
            ]:
                response = await client.post(
                    path,
                    json={
                        "session_id": "sid",
                        "tool_name": "Read",
                        "tool_use_id": tool_id,
                        "tool_input": {"file_path": "a.py"},
                    },
                )
                assert response.status == 200
        await server.stop_server()

        calls = [event["data"] for event in received_events if event["event_type"] == "tool_call"]
        assert [call["tool_use_id"] for call in calls] == ["call-ok", "call-error"]
        assert [call["is_error"] for call in calls] == [False, True]

    async def test_stop_allows_by_default(self):
        server = HookServer(port=0)
        await server.start()

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/hooks/stop",
                json={"session_id": "test-123"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {}  # no on_stop_request handler → empty response

        await server.stop_server()

    async def test_file_change_routes_only_invoke_capture_callbacks(self):
        received_events: list[dict[str, Any]] = []
        received_callbacks: list[tuple[str, str]] = []

        async def on_event(data: dict[str, Any]):
            received_events.append(data)

        async def on_pre(session_id: str, data: dict[str, Any]):
            received_callbacks.append(("pre", session_id))

        async def on_post(session_id: str, data: dict[str, Any]):
            received_callbacks.append(("post", session_id))

        server = HookServer(
            port=0,
            on_event=on_event,
            on_pre_tool_use_request=on_pre,
            on_post_tool_use_request=on_post,
        )
        await server.start()

        async with TestClient(TestServer(server.app)) as client:
            pre = await client.post(
                "/hooks/file-changes/pre",
                json={"session_id": "opencode-1", "tool_name": "NotebookEdit"},
            )
            post = await client.post(
                "/hooks/file-changes/post",
                json={"session_id": "opencode-1", "tool_name": "NotebookEdit"},
            )
            assert pre.status == 200
            assert post.status == 200

        await server.stop_server()
        assert received_callbacks == [("pre", "opencode-1"), ("post", "opencode-1")]
        assert received_events == []

    async def test_stop_can_block(self):
        async def on_stop(session_id, data):
            return {"decision": "block", "reason": "Not done yet"}

        server = HookServer(port=0, on_stop_request=on_stop)
        await server.start()

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/hooks/stop",
                json={"session_id": "test-123"},
            )
            data = await resp.json()
            assert data["decision"] == "block"

        await server.stop_server()

    async def test_notification_endpoint(self):
        received_events: list[dict[str, Any]] = []

        async def on_event(data: dict[str, Any]):
            received_events.append(data)

        server = HookServer(port=0, on_event=on_event)
        await server.start()

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/hooks/notification",
                json={"session_id": "test-123", "message": "hello"},
            )
            assert resp.status == 200

        await server.stop_server()
        assert len(received_events) == 1
