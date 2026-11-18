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

"""JSONL sidecar exposing file provenance capture to external runtimes."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from workflow_controller.logging_config import emit_stdout
from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.engine import ProvenanceEngine
from workflow_controller.plugins.file_provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 4 * 1024 * 1024


class ProvenanceSidecar:
    """Serve one fixed project root without starting the Workflow Controller control plane."""

    def __init__(
        self,
        project: Path,
        *,
        ignore: list[str] | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.project = project.expanduser().resolve()
        if not self.project.is_dir():
            raise ValueError(f"project is not a directory: {self.project}")
        store = ProvenanceStore(self.project)
        self.engine = ProvenanceEngine(
            self.project,
            store,
            ignore=ignore,
            max_file_bytes=max_file_bytes,
        )
        self._active: dict[str, FileChangeContext] = {}
        self._active_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def serve(self) -> None:
        tasks: set[asyncio.Task[None]] = set()
        try:
            while line := await asyncio.to_thread(sys.stdin.buffer.readline):
                if len(line) > _MAX_REQUEST_BYTES:
                    await self._write(None, False, error="request exceeds 4 MiB")
                    continue
                request = await self._parse(line)
                if request is None:
                    continue
                if request[1] == "shutdown":
                    await self._shutdown_from_request(tasks, request)
                    return
                task = asyncio.create_task(self._dispatch(*request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._close_active("sidecar stopped before tool completion")

    async def _shutdown_from_request(
        self,
        tasks: set[asyncio.Task[None]],
        request: tuple[Any, str, dict[str, Any]],
    ) -> None:
        """shutdown 请求：等在途任务结束后派发 shutdown（随后服务循环退出）。"""
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._dispatch(*request)

    async def _parse(self, line: bytes) -> tuple[Any, str, dict[str, Any]] | None:
        request_id: Any = None
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("request must be an object")
            request_id = value.get("id")
            method = value.get("method")
            params = value.get("params", {})
            if not isinstance(method, str) or not method:
                raise ValueError("method must be a non-empty string")
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            return request_id, method, params
        except ValueError as exc:
            await self._write(request_id, False, error=str(exc))
            return None

    async def _dispatch(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            if method == "ping":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "backendVersion": _package_version(),
                    "project": str(self.project),
                }
            elif method == "before":
                result = await self._before(params)
            elif method == "after":
                result = await self._after(params)
            elif method == "shutdown":
                await self._close_active("sidecar shutdown before tool completion")
                result = {"closed": True}
            else:
                raise ValueError(f"unknown method: {method}")
        except Exception as exc:
            logger.exception("provenance sidecar request failed: %s", method)
            await self._write(request_id, False, error=str(exc))
        else:
            await self._write(request_id, True, result=result)

    async def _before(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("context")
        if not isinstance(raw, dict):
            raise ValueError("before.context must be an object")
        context = FileChangeContext.from_dict(raw)
        self._validate_cwd(context.cwd)
        async with self._active_lock:
            if context.capture_id in self._active:
                raise ValueError(f"capture already active: {context.capture_id}")
        await self.engine.before(context)
        async with self._active_lock:
            self._active[context.capture_id] = context
        return {"captureId": context.capture_id}

    async def _after(self, params: dict[str, Any]) -> dict[str, Any]:
        capture_id = str(params.get("capture_id") or "")
        raw = params.get("outcome")
        if not capture_id or not isinstance(raw, dict):
            raise ValueError("after requires capture_id and outcome")
        # Validate the payload before mutating active state so callers can retry
        # an after request when the first payload is malformed.
        outcome = FileChangeOutcome.from_dict(raw)
        async with self._active_lock:
            context = self._active.pop(capture_id, None)
        if context is None:
            return {"captureId": capture_id, "closed": False}
        try:
            mutations = await self.engine.after(context, outcome)
        except BaseException:
            async with self._active_lock:
                self._active.setdefault(capture_id, context)
            raise
        return {"captureId": capture_id, "closed": True, "mutations": len(mutations)}

    def _validate_cwd(self, cwd: Path) -> None:
        resolved = cwd.expanduser().resolve()
        if resolved != self.project and self.project not in resolved.parents:
            raise ValueError(f"capture cwd is outside project: {resolved}")

    async def _close_active(self, reason: str) -> None:
        async with self._active_lock:
            active = tuple(self._active.values())
            self._active.clear()
        for context in active:
            outcome = FileChangeOutcome(
                succeeded=False,
                is_error=True,
                tool_output={"reason": reason},
                path_hints=(),
                finished_at=datetime.now(tz=UTC),
                incomplete=True,
            )
            try:
                await self.engine.after(context, outcome)
            except Exception:
                logger.exception("failed to close capture %s", context.capture_id)

    async def _write(
        self,
        request_id: Any,
        ok: bool,
        *,
        result: Any = None,
        error: str = "",
    ) -> None:
        value = {"id": request_id, "ok": ok}
        value["result" if ok else "error"] = result if ok else error
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            emit_stdout(line)


def run_sidecar(
    project: Path,
    *,
    ignore: list[str] | None = None,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> None:
    asyncio.run(
        ProvenanceSidecar(
            project,
            ignore=ignore,
            max_file_bytes=max_file_bytes,
        ).serve()
    )


def _package_version() -> str:
    try:
        return importlib.metadata.version("workflow-controller")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"
