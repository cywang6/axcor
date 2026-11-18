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

"""Worktree snapshots and conservative line-attribution propagation."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from workflow_controller.plugins.file_provenance.store import LineAttribution, ProvenanceStore

if TYPE_CHECKING:
    from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome

DEFAULT_IGNORE = (
    ".git",
    ".workflow-controller",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    ".tox",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


@dataclass(frozen=True, slots=True)
class FileMutation:
    path: str
    before: bytes | None
    after: bytes | None
    additions: int
    deletions: int
    conflict: bool = False


class ProvenanceEngine:
    def __init__(
        self,
        worktree: Path,
        store: ProvenanceStore,
        *,
        ignore: list[str] | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.worktree = worktree.resolve()
        self.store = store
        self.ignore = tuple(dict.fromkeys((*DEFAULT_IGNORE, *(ignore or []))))
        self.max_file_bytes = max_file_bytes
        self._prepared_captures: set[str] = set()
        self._state_lock = asyncio.Lock()

    async def before(self, context: FileChangeContext) -> None:
        head_before = await asyncio.to_thread(_git_head, self.worktree)
        await asyncio.to_thread(
            self.store.begin_capture,
            context,
            head_before=head_before,
        )
        try:
            snapshot = await asyncio.to_thread(self.scan)
            await asyncio.to_thread(
                self.store.record_snapshot,
                context.capture_id,
                "before",
                snapshot,
            )
            async with self._state_lock:
                self._prepared_captures.add(context.capture_id)
        except BaseException:
            await asyncio.to_thread(self.store.abandon_capture, context.capture_id)
            raise

    async def after(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> list[FileMutation]:
        async with self._state_lock:
            prepared = context.capture_id in self._prepared_captures
        if not prepared:
            await self._close_missing_snapshot(context, outcome)
            return []

        snapshot, current = await self._load_after_inputs(context, outcome)

        head_after = await asyncio.to_thread(_git_head, self.worktree)
        shared_conflict = await asyncio.to_thread(
            self.store.end_capture_window,
            context.capture_id,
        )
        conflict = (
            shared_conflict
            or outcome.incomplete
            or context.attributes.get("file_change_overlap") == "true"
        )
        return await self._complete_after(context, outcome, snapshot, current, conflict, head_after)

    def scan(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for root, dirnames, filenames in os.walk(self.worktree):
            root_path = Path(root)
            dirnames[:] = self._kept_dirs(root_path, dirnames)
            for name in filenames:
                path = root_path / name
                relative = path.relative_to(self.worktree).as_posix()
                if self._ignored(relative):
                    continue
                content = self._scan_file(path)
                if content is not None:
                    files[relative] = content
        return files

    def _kept_dirs(self, root_path: Path, dirnames: list[str]) -> list[str]:
        """os.walk 本层继续下探的目录名（剔除符号链接与命中忽略规则的目录）。"""
        kept: list[str] = []
        for name in dirnames:
            if not (root_path / name).is_symlink() and not self._ignored(
                (root_path / name).relative_to(self.worktree).as_posix()
            ):
                kept.append(name)
        return kept

    def _scan_file(self, path: Path) -> bytes | None:
        """读单个候选文件内容；符号链接、目录、超过大小上限或读失败返回 None。"""
        try:
            if path.is_symlink() or not path.is_file():
                return None
            if path.stat().st_size > self.max_file_bytes:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def _ignored(self, relative: str) -> bool:
        parts = relative.split("/")
        mandatory = ".git" in parts or ".workflow-controller" in parts
        return mandatory or any(
            pattern in parts or fnmatch.fnmatch(relative, pattern) for pattern in self.ignore
        )

    def _derive(
        self,
        context: FileChangeContext,
        before_files: dict[str, bytes],
        after_files: dict[str, bytes],
        overlap: bool,
    ) -> list[FileMutation]:
        result: list[FileMutation] = []
        deleted = {
            path: content for path, content in before_files.items() if path not in after_files
        }
        added = {path: content for path, content in after_files.items() if path not in before_files}
        renamed_from, ambiguous_added = self._renames(deleted, added)
        for path in sorted(before_files.keys() | after_files.keys()):
            before = before_files.get(path)
            after = after_files.get(path)
            if before == after:
                continue
            result.append(
                self._derive_file(
                    context,
                    path,
                    before,
                    after,
                    deleted,
                    renamed_from,
                    ambiguous_added,
                    overlap,
                )
            )
        for old_path, _old_attributions in renamed_from.values():
            self.store.delete_file_state(old_path)
        return result

    def _renames(
        self,
        deleted: dict[str, bytes],
        added: dict[str, bytes],
    ) -> tuple[dict[str, tuple[str, list[LineAttribution]]], set[str]]:
        renamed: dict[str, tuple[str, list[LineAttribution]]] = {}
        ambiguous: set[str] = set()
        for content in set(deleted.values()) & set(added.values()):
            old_paths = [path for path, value in deleted.items() if value == content]
            new_paths = [path for path, value in added.items() if value == content]
            if len(old_paths) == 1 and len(new_paths) == 1:
                old_path = old_paths[0]
                renamed[new_paths[0]] = (
                    old_path,
                    self.store.load_file_state(old_path, content),
                )
            else:
                ambiguous.update(new_paths)
        return renamed, ambiguous

    async def _close_missing_snapshot(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> None:
        capture = await asyncio.to_thread(self.store.get_capture, context.capture_id)
        if capture is None:
            return
        head_after = await asyncio.to_thread(_git_head, self.worktree)
        await asyncio.to_thread(self.store.end_capture_window, context.capture_id)
        await asyncio.to_thread(
            self.store.record_conflict,
            context.capture_id,
            "missing_before_snapshot",
            f"capture {context.capture_id} has no completed before snapshot",
        )
        await asyncio.to_thread(
            self.store.finish_capture,
            context,
            replace(outcome, incomplete=True),
            head_after=head_after,
        )

    async def _fail_capture(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
        kind: str,
        detail: str,
    ) -> None:
        async with self._state_lock:
            self._prepared_captures.discard(context.capture_id)
        head_after = await asyncio.to_thread(_git_head, self.worktree)
        await asyncio.to_thread(self.store.end_capture_window, context.capture_id)
        await asyncio.to_thread(
            self.store.record_conflict,
            context.capture_id,
            kind,
            f"capture {context.capture_id} {detail}",
        )
        await asyncio.to_thread(
            self.store.finish_capture,
            context,
            replace(outcome, incomplete=True),
            head_after=head_after,
        )

    async def _load_after_inputs(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
    ) -> tuple[dict[str, bytes], dict[str, bytes]]:
        try:
            snapshot = await asyncio.to_thread(
                self.store.load_snapshot,
                context.capture_id,
                "before",
            )
        except BaseException:
            await self._fail_capture(
                context,
                outcome,
                "before_snapshot_read_failed",
                "failed while reading the before snapshot from the evidence store",
            )
            raise

        try:
            current = await asyncio.to_thread(self.scan)
        except BaseException:
            await self._fail_capture(
                context,
                outcome,
                "after_scan_failed",
                "failed while scanning the worktree",
            )
            raise
        return snapshot, current

    async def _complete_after(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
        snapshot: dict[str, bytes],
        current: dict[str, bytes],
        conflict: bool,
        head_after: str,
    ) -> list[FileMutation]:
        try:
            async with self._state_lock:
                mutations = await asyncio.to_thread(
                    self._derive,
                    context,
                    snapshot,
                    current,
                    conflict,
                )
                self._prepared_captures.discard(context.capture_id)
            await asyncio.to_thread(
                self.store.record_snapshot,
                context.capture_id,
                "after",
                current,
            )
            await asyncio.to_thread(
                self.store.finish_capture,
                context,
                outcome,
                head_after=head_after,
            )
            return mutations
        except BaseException:
            async with self._state_lock:
                self._prepared_captures.discard(context.capture_id)
            await asyncio.to_thread(
                self.store.record_conflict,
                context.capture_id,
                "capture_finalize_failed",
                f"capture {context.capture_id} could not finalize attribution",
            )
            await asyncio.to_thread(
                self.store.finish_capture,
                context,
                replace(outcome, incomplete=True),
                head_after=head_after,
            )
            raise

    def _derive_file(
        self,
        context: FileChangeContext,
        path: str,
        before: bytes | None,
        after: bytes | None,
        deleted: dict[str, bytes],
        renamed_from: dict[str, tuple[str, list[LineAttribution]]],
        ambiguous_added: set[str],
        overlap: bool,
    ) -> FileMutation:
        old_lines = _decode_lines(before)
        new_lines = _decode_lines(after)
        old_attributions = self.store.load_file_state(path, before) if before is not None else []
        if path in renamed_from:
            old_path, old_attributions = renamed_from[path]
            old_lines = _decode_lines(deleted[old_path])
        path_conflict = overlap or path in ambiguous_added
        actor = _attribution_for(context, path_conflict)
        attributions, additions, deletions = move_attributions(
            old_lines,
            new_lines,
            old_attributions,
            actor,
        )
        if after is None:
            self.store.delete_file_state(path)
        else:
            self.store.save_file_state(path, after, attributions, context.capture_id)
        conflict = path_conflict or any(
            item.conflict and item.capture_id == context.capture_id for item in attributions
        )
        self.store.record_mutation(
            context.capture_id,
            path,
            before=before,
            after=after,
            additions=additions,
            deletions=deletions,
            conflict=conflict,
        )
        return FileMutation(path, before, after, additions, deletions, conflict)


def move_attributions(
    before: list[str],
    after: list[str],
    old: list[LineAttribution],
    new_actor: LineAttribution,
) -> tuple[list[LineAttribution], int, int]:
    if len(before) != len(old):
        old = [LineAttribution("unknown") for _ in before]
    attributions = [new_actor for _ in after]
    additions = 0
    deletions = 0
    candidates: dict[str, list[LineAttribution]] = {}
    for index, content in enumerate(before):
        candidates.setdefault(content, []).append(old[index])
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        a=before,
        b=after,
        autojunk=False,
    ).get_opcodes():
        if tag == "equal":
            for source, target in zip(range(i1, i2), range(j1, j2), strict=True):
                possible = candidates.get(before[source])
                attributions[target] = (
                    old[source]
                    if possible is not None and len(set(possible)) == 1
                    else replace(new_actor, actor_kind="unknown", actor_id=None, conflict=True)
                )
        elif tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
        else:
            additions += j2 - j1
            deletions += i2 - i1
    return attributions, additions, deletions


def _attribution_for(
    context: FileChangeContext,
    conflict: bool,
) -> LineAttribution:
    return LineAttribution(
        actor_kind="unknown" if conflict else context.actor_kind,
        actor_id=None if conflict else _actor_id(context),
        capture_id=context.capture_id,
        session_id=context.workflow_controller_session_id,
        runtime=context.runtime,
        model=context.model,
        agent_name=context.agent_name,
        native_agent_id=context.native_agent_id,
        conflict=conflict,
    )


def _actor_id(context: FileChangeContext) -> str:
    if context.actor_kind == "automation":
        return "automation"
    raw = (
        f"{context.runtime}:"
        f"{context.workflow_controller_session_id or context.native_session_id or context.capture_id}:"
        f"{context.native_agent_id or ''}"
    )
    session_id = "s_" + hashlib.sha256(raw.encode()).hexdigest()[:14]
    trace_id = "t_" + hashlib.sha256(context.capture_id.encode()).hexdigest()[:14]
    return f"{session_id}::{trace_id}"


def _decode_lines(content: bytes | None) -> list[str]:
    if content is None:
        return []
    try:
        return content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []


def _git_head(worktree: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        result = subprocess.run(
            [git, "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
