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

"""File provenance plugin lifecycle and capture persistence wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugin.sdk import Plugin
from workflow_controller.plugins.file_provenance.audit_export import build_audit_evidence
from workflow_controller.plugins.file_provenance.engine import ProvenanceEngine
from workflow_controller.plugins.file_provenance.git_notes import CommitFinalizer, git_root
from workflow_controller.plugins.file_provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from workflow_controller.event.event_bus import InterceptResult
    from workflow_controller.plugin.base import PluginConfig
    from workflow_controller.plugin.context import PluginContext, PluginServices
    from workflow_controller.plugin.file_changes import FileChangeCapture
    from workflow_controller.session.events import Event

logger = logging.getLogger(__name__)


class FileProvenancePlugin(Plugin):
    """Subscribe to file-change windows and persist their lifecycle evidence."""

    def __init__(self, config: PluginConfig, config_path: Path) -> None:
        super().__init__(config, config_path)
        self._capture: FileChangeCapture | None = None
        self._subscription: int | None = None
        self._engines: dict[Path, ProvenanceEngine] = {}
        self._finalizers: dict[Path, CommitFinalizer] = {}
        self._known_projects: set[Path] = set()
        self._known_worktrees: set[Path] = set()
        self._capture_engines: dict[str, ProvenanceEngine] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._services: PluginServices | None = None
        self._lock = Lock()

    def install(self, ctx: PluginContext) -> None:
        super().install(ctx)
        self._services = ctx.services
        work_dir = Path(ctx.work_dir).expanduser().resolve()
        if work_dir.is_dir():
            self._known_projects.add(work_dir)
        with suppress(ValueError):
            self._known_worktrees.add(_find_worktree(work_dir))
        self._capture = ctx.file_changes
        if self._capture is None:
            logger.warning("file provenance capture capability is unavailable")
            return
        self._subscription = self._capture.subscribe(
            self._before_capture,
            self._after_capture,
        )
        logger.info("file provenance plugin installed")

    def uninstall(self) -> None:
        if self._capture is not None and self._subscription is not None:
            self._capture.unsubscribe(self._subscription)
        self._subscription = None
        ctx = getattr(self, "_ctx", None)
        if ctx is not None:
            ctx.bus.detach_interceptor("session_delete", self._on_session_delete)
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        # Registry 会为已经开始的 capture 保留原 after callback；相关 engine 映射
        # 必须保留到 callback 闭合，不能在 reload/uninstall 时提前清空。
        logger.info("file provenance plugin uninstalled")

    def store_for(self, project_dir: str | Path) -> ProvenanceStore | None:
        project = Path(project_dir).expanduser().resolve()
        if not project.is_dir():
            return None
        with self._lock:
            known = project in self._known_projects
        if not known:
            return None
        worktree = git_root(project) or project
        return self._engine_for(worktree).store

    async def claim_human(
        self,
        project_dir: str | Path,
        path: str,
        start_line: int,
        end_line: int,
        author: str,
        *,
        commit: str = "",
        expected_content_hash: str,
    ) -> int:
        if commit:
            raise ValueError("historical commit attribution cannot be claimed")
        store = await asyncio.to_thread(self.store_for, project_dir)
        if store is None:
            raise ValueError("project is not tracked by file provenance")
        claimed = await asyncio.to_thread(
            store.claim_human,
            path,
            start_line,
            end_line,
            author,
            expected_content_hash,
        )
        with self._lock:
            finalizer = self._finalizers.get(store.worktree)
        if finalizer is not None:
            head_hash = await asyncio.to_thread(finalizer.head_content_hash, path)
            if head_hash == expected_content_hash:
                await finalizer.refinalize_head()
        return claimed

    async def export_audit_evidence(
        self,
        project_dir: str | Path,
        session_id: str,
    ) -> dict[str, Any]:
        store = await asyncio.to_thread(self.store_for, project_dir)
        if store is None:
            raise ValueError("project is not tracked by file provenance")
        if self._services is None:
            raise RuntimeError("plugin services are unavailable")
        session = await asyncio.to_thread(self._services.session_metadata, session_id)
        if session is None:
            raise KeyError(session_id)
        session_project = session.get("project_dir")
        if not session_project:
            raise KeyError(session_id)
        session_project_dir = Path(str(session_project)).expanduser().resolve()
        session_worktree = git_root(session_project_dir) or session_project_dir
        if session_worktree != store.worktree:
            raise KeyError(session_id)
        events = await asyncio.to_thread(self._services.session_events, session_id)
        captures = await asyncio.to_thread(store.list_captures_for_session, session_id)
        with self._lock:
            engine = self._engines.get(store.worktree)
        ignore = engine.ignore if engine is not None else ()
        max_file_bytes = engine.max_file_bytes if engine is not None else 0
        return await asyncio.to_thread(
            build_audit_evidence,
            session,
            events,
            captures,
            store.read_snapshot,
            ignore=ignore,
            max_file_bytes=max_file_bytes,
        )

    def get_web_router(self) -> Any:
        from workflow_controller.plugins.file_provenance.web_api import build_router

        return build_router()

    async def _before_capture(self, context: FileChangeContext) -> None:
        task = asyncio.create_task(
            self._begin_capture(context),
            name=f"file-provenance-before-{context.capture_id}",
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _wait_for_task(task)
            except Exception:
                logger.exception("provenance cancelled setup failed: %s", context.capture_id)
            else:
                cleanup = FileChangeOutcome(
                    succeeded=False,
                    is_error=True,
                    tool_output={"reason": "pre-tool capture cancelled"},
                    path_hints=(),
                    finished_at=datetime.now(tz=UTC),
                    incomplete=True,
                )
                cleanup_task = asyncio.create_task(
                    self._complete_capture(context, cleanup),
                    name=f"file-provenance-cancel-{context.capture_id}",
                )
                try:
                    await _wait_for_task(cleanup_task)
                except Exception:
                    logger.exception("provenance cancelled cleanup failed: %s", context.capture_id)
            raise
        except Exception:
            logger.exception("provenance before capture failed: %s", context.capture_id)

    async def _after_capture(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> None:
        task = asyncio.create_task(
            self._complete_capture(context, outcome),
            name=f"file-provenance-after-{context.capture_id}",
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _wait_for_task(task)
            except Exception:
                logger.exception("provenance cancelled finalization failed: %s", context.capture_id)
            raise
        except Exception:
            logger.exception("provenance after capture failed: %s", context.capture_id)

    async def _begin_capture(self, context: FileChangeContext) -> None:
        engine = await asyncio.to_thread(self._engine_for, context.cwd)
        self._ensure_poll_task()
        await engine.before(context)
        with self._lock:
            self._capture_engines[context.capture_id] = engine

    async def _complete_capture(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> None:
        with self._lock:
            engine = self._capture_engines.get(context.capture_id)
        if engine is None:
            return
        try:
            mutations = await engine.after(context, outcome)
            if mutations:
                with self._lock:
                    finalizer = self._finalizers.get(engine.worktree)
                if finalizer is not None:
                    await finalizer.finalize_head()
        finally:
            with self._lock:
                self._capture_engines.pop(context.capture_id, None)

    def _engine_for(self, cwd: Path) -> ProvenanceEngine:
        project = cwd.expanduser().resolve()
        worktree = git_root(cwd) or project
        with self._lock:
            self._known_projects.add(worktree)
            self._known_worktrees.add(worktree)
            engine = self._engines.get(worktree)
        if engine is not None:
            return engine

        extra = self.config.extra or {}
        store = ProvenanceStore(worktree)
        engine = ProvenanceEngine(
            worktree,
            store,
            ignore=[str(value) for value in extra.get("ignore", [])],
            max_file_bytes=int(extra.get("max_file_bytes", 2 * 1024 * 1024)),
        )
        finalizer = None
        if git_root(worktree) is not None:
            finalizer = CommitFinalizer(worktree, store)
            if store.get_state("last_commit") is None:
                head = finalizer.current_head()
                if head:
                    store.set_state("last_commit", head)
        with self._lock:
            cached = self._engines.setdefault(worktree, engine)
            if cached is engine and finalizer is not None:
                self._finalizers[worktree] = finalizer
        return cached

    def _register_interceptors(self, ctx: PluginContext) -> None:
        ctx.bus.intercept("session_delete", self._on_session_delete, priority=10)

    async def _on_session_delete(self, event: Event) -> InterceptResult | None:
        with self._lock:
            stores = tuple(engine.store for engine in self._engines.values())
        results = await asyncio.gather(
            *(asyncio.to_thread(store.redact_session, event.session_id) for store in stores),
            return_exceptions=True,
        )
        for store, result in zip(stores, results, strict=True):
            if isinstance(result, Exception):
                logger.error("provenance redaction failed for %s: %s", store.worktree, result)
        return None

    def _ensure_poll_task(self) -> None:
        with self._lock:
            has_finalizers = bool(self._finalizers)
        if has_finalizers and (self._poll_task is None or self._poll_task.done()):
            self._poll_task = asyncio.create_task(
                self._poll_commits(),
                name="file-provenance-commit-poller",
            )

    async def _poll_round(self, finalizers: tuple[CommitFinalizer, ...]) -> None:
        """跑一轮 commit 落账：并行 finalize 并逐个记录失败。"""
        results = await asyncio.gather(
            *(item.finalize_head() for item in finalizers),
            return_exceptions=True,
        )
        for finalizer, result in zip(finalizers, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "provenance commit finalization failed for %s: %s",
                    finalizer.worktree,
                    result,
                )

    async def _poll_commits(self) -> None:
        interval = max(float(self._cfg("poll_interval_seconds", 2.0)), 0.25)
        try:
            while True:
                with self._lock:
                    finalizers = tuple(self._finalizers.values())
                await self._poll_round(finalizers)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


async def _wait_for_task(task: asyncio.Task[None]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


def _find_worktree(cwd: Path) -> Path:
    resolved = cwd.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"capture cwd is not a directory: {resolved}")
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved
