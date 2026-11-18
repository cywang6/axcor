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

"""Finalize captured attribution into authorship/3.0.0 Git notes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from workflow_controller.plugins.file_provenance.store import LineAttribution

if TYPE_CHECKING:
    from workflow_controller.plugins.file_provenance.store import ProvenanceStore

NOTES_REF = "refs/notes/ai"
SCHEMA = "authorship/3.0.0"
_REPO_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_SESSION_KEY_RE = re.compile(r"s_[0-9a-f]{14}::t_[0-9a-f]{14}")
_HUMAN_KEY_RE = re.compile(r"h_[0-9a-f]{14}")
_LEGACY_KEY_RE = re.compile(r"(?:[0-9a-f]{7}|[0-9a-f]{16})")


def git_output(cwd: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise RuntimeError("git command failed") from exc
        return ""
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip() if result.returncode == 0 else ""


def git_root(cwd: Path) -> Path | None:
    value = git_output(cwd, "rev-parse", "--show-toplevel", check=False)
    return Path(value).resolve() if value else None


def git_common_dir(cwd: Path) -> Path | None:
    value = git_output(cwd, "rev-parse", "--git-common-dir", check=False)
    if not value:
        return None
    path = Path(value)
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()


class CommitFinalizer:
    def __init__(self, worktree: Path, store: ProvenanceStore) -> None:
        self.worktree = worktree.resolve()
        self.store = store

    def current_head(self) -> str:
        return git_output(self.worktree, "rev-parse", "HEAD", check=False)

    def head_content_hash(self, path: str) -> str | None:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=self.worktree,
            capture_output=True,
            check=False,
            timeout=10,
        )
        return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None

    async def finalize_head(self) -> str | None:
        head, last, common = await asyncio.to_thread(self._head_state)
        if not head or head == last or common is None:
            return None
        loop_key = id(asyncio.get_running_loop())
        lock = _REPO_LOCKS.setdefault((loop_key, str(common)), asyncio.Lock())
        async with lock:
            commits = await asyncio.to_thread(self._commits_to_finalize, last, head)
            for commit in commits:
                await asyncio.to_thread(self._finalize, commit, common)
        return head

    async def refinalize_head(self) -> str | None:
        head, last, common = await asyncio.to_thread(self._head_state)
        if not head or head != last or common is None:
            return None
        loop_key = id(asyncio.get_running_loop())
        lock = _REPO_LOCKS.setdefault((loop_key, str(common)), asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._finalize, head, common)
        return head

    def _head_state(self) -> tuple[str, str | None, Path | None]:
        return (
            self.current_head(),
            self.store.get_state("last_commit"),
            git_common_dir(self.worktree),
        )

    def _commits_to_finalize(self, last: str | None, head: str) -> list[str]:
        if not last:
            return [head]
        try:
            git_output(self.worktree, "merge-base", "--is-ancestor", last, head)
            start = last
        except RuntimeError:
            start = git_output(self.worktree, "merge-base", last, head, check=False)
            self.store.record_conflict(
                None,
                "history_rewritten",
                f"provenance commit cursor {last} is not an ancestor of {head}",
            )
            if not start:
                return [head]
        values = git_output(
            self.worktree,
            "rev-list",
            "--reverse",
            "--first-parent",
            f"{start}..{head}",
            check=False,
        ).splitlines()
        return values or [head]

    def _finalize(self, commit: str, common: Path) -> None:
        lock_path = common / "workflow-controller-file-provenance.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                note, parent, metrics = self._build_note(commit)
                if note is not None:
                    _write_note(self.worktree, commit, note)
                if metrics is None:
                    self.store.mark_pending_committed(commit, parent)
                else:
                    self.store.finalize_commit(commit, parent, *metrics)
                self.store.set_state("last_commit", commit)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _build_note(
        self,
        commit: str,
    ) -> tuple[str | None, str, tuple[int, int, int] | None]:
        parent = _first_parent(self.worktree, commit)
        diff = git_output(
            self.worktree,
            "-c",
            "core.quotePath=false",
            "diff",
            "--unified=0",
            "--no-color",
            parent or _empty_tree(self.worktree),
            commit,
            "--",
        )
        added = _added_lines(diff)
        eligible_capture_ids = self.store.capture_ids_for_commit(commit, parent)
        gathered = self._gather_line_attributions(added, commit, parent, eligible_capture_ids)
        existing = self._existing_blocks(commit)
        if existing is None:
            return None, parent, None
        blocks, metadata = existing
        occupied = _occupied(blocks)
        for path, actors in sorted(gathered.by_path.items()):
            for actor, lines in sorted(actors.items()):
                free = [line for line in lines if line not in occupied.get(path, set())]
                if free:
                    blocks.setdefault(path, []).append(f"  {actor} {_ranges(free)}")
        metrics = _note_metrics(blocks, added, gathered.local_agent_lines)
        if not blocks:
            return None, parent, metrics
        note = _format_note(blocks, metadata, parent, gathered.sessions, gathered.humans)
        return note, parent, metrics

    def _gather_line_attributions(
        self,
        added: dict[str, list[int]],
        commit: str,
        parent: str,
        eligible_capture_ids: set[str],
    ) -> _GatheredAttributions:
        by_path: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        local_agent_lines: dict[tuple[str, int], str] = {}
        sessions: dict[str, dict[str, Any]] = {}
        humans: dict[str, dict[str, str]] = {}
        for path, line_numbers in added.items():
            blame_lines = self._commit_blame(path, commit, parent)
            if blame_lines:
                self.store.record_commit_lines(
                    commit,
                    path,
                    [str(line["content"]) for line in blame_lines],
                    [LineAttribution.from_dict(line) for line in blame_lines],
                )
            state = {int(line["line_no"]): line for line in blame_lines}
            for line_no in line_numbers:
                item = state.get(line_no)
                if item is None:
                    continue
                actor_id = _blame_actor_id(item, eligible_capture_ids)
                if actor_id is None:
                    continue
                by_path[path][actor_id].append(line_no)
                if item["actor_kind"] == "agent":
                    local_agent_lines[(path, line_no)] = actor_id
                    session_key = actor_id.split("::", 1)[0]
                    sessions.setdefault(
                        session_key,
                        _agent_session_metadata(item, actor_id),
                    )
                else:
                    humans.setdefault(
                        actor_id,
                        {"author": str(item.get("agent_name") or "unknown")},
                    )
        return _GatheredAttributions(by_path, local_agent_lines, sessions, humans)

    def _existing_blocks(
        self,
        commit: str,
    ) -> tuple[dict[str, list[str]], dict[str, Any]] | None:
        existing = _read_note(self.worktree, commit)
        existing_attestations, metadata, valid = _parse_note(existing)
        if existing and not valid:
            self.store.record_conflict(
                None,
                "invalid_existing_note",
                f"commit {commit} has an invalid {NOTES_REF} entry; left unchanged",
            )
            return None
        blocks, blocks_valid = _attestation_blocks(existing_attestations, metadata)
        if not blocks_valid:
            self.store.record_conflict(
                None,
                "invalid_existing_note",
                f"commit {commit} has invalid attestation ranges; left unchanged",
            )
            return None
        return blocks, metadata

    def _commit_blame(
        self,
        path: str,
        commit: str,
        parent: str,
    ) -> list[dict[str, Any]]:
        raw = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=self.worktree,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if raw.returncode != 0:
            return []
        try:
            commit_lines = raw.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return []
        source_lines, source_attributions = self.store.commit_source(path, commit, parent)
        unknown = LineAttribution("unknown").to_dict()
        result = [
            {"line_no": index + 1, "content": line, **unknown}
            for index, line in enumerate(commit_lines)
        ]
        candidates: dict[str, list[LineAttribution]] = defaultdict(list)
        for content, attribution in zip(
            source_lines,
            source_attributions,
            strict=True,
        ):
            candidates[content].append(attribution)
        matcher = SequenceMatcher(a=source_lines, b=commit_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                continue
            for source, target in zip(range(i1, i2), range(j1, j2), strict=True):
                if len(set(candidates[source_lines[source]])) == 1:
                    result[target] = {
                        "line_no": target + 1,
                        "content": commit_lines[target],
                        **source_attributions[source].to_dict(),
                    }
        return result


def _first_parent(cwd: Path, commit: str) -> str:
    values = git_output(cwd, "rev-list", "--parents", "-n", "1", commit).split()
    return values[1] if len(values) > 1 else ""


def _empty_tree(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "mktree"],
        cwd=cwd,
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "git mktree failed")
    return result.stdout.strip()


def _read_note(cwd: Path, commit: str) -> str:
    return git_output(cwd, "notes", f"--ref={NOTES_REF}", "show", commit, check=False)


def _write_note(cwd: Path, commit: str, note: str) -> None:
    result = subprocess.run(
        ["git", "notes", f"--ref={NOTES_REF}", "add", "-f", "-F", "-", commit],
        cwd=cwd,
        input=note,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git notes add failed")


def _parse_note(note: str) -> tuple[list[str], dict[str, Any], bool]:
    if not note or "\n---\n" not in note:
        return [], {}, not note
    attestations, raw_metadata = note.split("\n---\n", 1)
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return [], {}, False
    if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA:
        return [], {}, False
    if not isinstance(metadata.get("base_commit_sha"), str):
        return [], {}, False
    if not isinstance(metadata.get("prompts"), dict):
        return [], {}, False
    if any(
        key in metadata and not isinstance(metadata[key], dict) for key in ("sessions", "humans")
    ):
        return [], {}, False
    if "git_ai_version" in metadata and not isinstance(metadata["git_ai_version"], str):
        return [], {}, False
    return attestations.splitlines(), metadata, True


def _read_attestation_header(line: str, blocks: dict[str, list[str]]) -> str:
    """块头行：解码路径并登记空 entry；空路径属非法输入，按 ValueError 上抛。"""
    path = str(json.loads(line)) if line.startswith('"') else line
    if not path:
        raise ValueError("empty attestation path")
    blocks.setdefault(path, [])
    return path


def _parsed_coverage(ranges: str) -> set[int] | None:
    """把 "start-end,..." 区间串解析为行号集合；区间非法或未递增时返回 None。"""
    covered: set[int] = set()
    previous = 0
    for part in ranges.split(","):
        bounds = [int(value) for value in part.split("-")]
        bad_shape = len(bounds) not in {1, 2} or bounds[0] < 1
        out_of_order = bounds[-1] < bounds[0] or bounds[0] <= previous
        if bad_shape or out_of_order:
            return None
        previous = bounds[-1]
        covered.update(range(bounds[0], bounds[-1] + 1))
    return covered


def _attestation_blocks(
    lines: list[str],
    metadata: dict[str, Any],
) -> tuple[dict[str, list[str]], bool]:
    blocks: dict[str, list[str]] = {}
    occupied: dict[str, set[int]] = defaultdict(set)
    path = ""
    try:
        for line in lines:
            if not line.strip():
                continue
            if not line.startswith("  "):
                path = _read_attestation_header(line, blocks)
                continue
            if not path or line.startswith("   "):
                return {}, False
            actor, ranges = line[2:].split(" ", 1)
            if not _actor_has_metadata(actor, metadata):
                return {}, False
            covered = _parsed_coverage(ranges)
            if covered is None or occupied[path] & covered:
                return {}, False
            occupied[path].update(covered)
            blocks[path].append(line)
    except (TypeError, ValueError):
        return {}, False
    return blocks, True


def _actor_has_metadata(actor: str, metadata: dict[str, Any]) -> bool:
    if _SESSION_KEY_RE.fullmatch(actor):
        session = actor.split("::", 1)[0]
        return session in metadata.get("sessions", {})
    if _HUMAN_KEY_RE.fullmatch(actor):
        return actor in metadata.get("humans", {})
    if _LEGACY_KEY_RE.fullmatch(actor):
        return actor in metadata.get("prompts", {})
    return False


def _occupied(blocks: dict[str, list[str]]) -> dict[str, set[int]]:
    result: dict[str, set[int]] = defaultdict(set)
    for path, entries in blocks.items():
        for line in entries:
            _, ranges = line.strip().split(" ", 1)
            for part in ranges.split(","):
                bounds = [int(value) for value in part.split("-")]
                result[path].update(range(bounds[0], bounds[-1] + 1))
    return result


def _added_actor_positions(
    blocks: dict[str, list[str]],
    added: dict[str, list[int]],
) -> dict[tuple[str, int], str]:
    result: dict[tuple[str, int], str] = {}
    added_sets = {path: set(lines) for path, lines in added.items()}
    for path, entries in blocks.items():
        for line in entries:
            actor, ranges = line.strip().split(" ", 1)
            covered: set[int] = set()
            for part in ranges.split(","):
                bounds = [int(value) for value in part.split("-")]
                covered.update(range(bounds[0], bounds[-1] + 1))
            for line_no in covered & added_sets.get(path, set()):
                result.setdefault((path, line_no), actor)
    return result


def _format_path(path: str) -> str:
    needs_quotes = '"' in path or any(char.isspace() for char in path)
    return json.dumps(path, ensure_ascii=False) if needs_quotes else path


class _DiffScanState:
    """diff 逐行扫描的可变状态（头段与体段解析器共享）。"""

    __slots__ = ("in_hunk", "next_line", "old_header", "path")

    def __init__(self) -> None:
        self.path = ""
        self.next_line = 0
        self.in_hunk = False
        self.old_header = False


def _scan_diff_header(state: _DiffScanState, line: str) -> bool:
    """diff 头段行（diff --git / --- / +++ / @@）；命中并更新状态时返回 True。"""
    if line.startswith("diff --git "):
        state.path = ""
        state.next_line = 0
        state.in_hunk = False
        state.old_header = False
        return True
    if not state.in_hunk and line.startswith("--- "):
        state.old_header = True
        return True
    if not state.in_hunk and state.old_header and line.startswith("+++ "):
        state.path = line[6:] if line.startswith("+++ b/") else ""
        state.old_header = False
        return True
    if line.startswith("@@"):
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        state.next_line = int(match.group(1)) if match else 0
        state.in_hunk = True
        return True
    return False


def _scan_hunk_body(state: _DiffScanState, line: str, result: defaultdict[str, list[int]]) -> None:
    """hunk 体段行：新增行记入 result，按新侧行号推进计数。"""
    if not state.in_hunk:
        return
    if line.startswith("\\"):
        return
    if line.startswith("+"):
        if state.path and state.next_line:
            result[state.path].append(state.next_line)
        state.next_line += 1
    elif not line.startswith("-") and state.next_line:
        state.next_line += 1


def _added_lines(diff: str) -> dict[str, list[int]]:
    result: defaultdict[str, list[int]] = defaultdict(list)
    state = _DiffScanState()
    for line in diff.splitlines():
        if not _scan_diff_header(state, line):
            _scan_hunk_body(state, line, result)
    return result


def _ranges(lines: list[int]) -> str:
    values = sorted(set(lines))
    result: list[str] = []
    start = end = values[0]
    for value in values[1:]:
        if value == end + 1:
            end = value
        else:
            result.append(str(start) if start == end else f"{start}-{end}")
            start = end = value
    result.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(result)


class _GatheredAttributions(NamedTuple):
    by_path: dict[str, dict[str, list[int]]]
    local_agent_lines: dict[tuple[str, int], str]
    sessions: dict[str, dict[str, Any]]
    humans: dict[str, dict[str, str]]


def _blame_actor_id(item: dict[str, Any], eligible_capture_ids: set[str]) -> str | None:
    if item.get("actor_kind") not in {"agent", "human"}:
        return None
    actor_id = item.get("actor_id")
    if not isinstance(actor_id, str) or not actor_id:
        return None
    if item["actor_kind"] == "agent" and item.get("capture_id") not in eligible_capture_ids:
        return None
    return actor_id


def _agent_session_metadata(item: dict[str, Any], actor_id: str) -> dict[str, Any]:
    return {
        "agent_id": {
            "tool": item.get("runtime") or "workflow_controller",
            "id": item.get("session_id") or actor_id,
            "model": item.get("model") or "unknown",
        },
        "custom_attributes": {
            "workflow_controller_agent": item.get("agent_name") or "",
            "workflow_controller_native_agent_id": item.get("native_agent_id") or "",
        },
    }


def _note_metrics(
    blocks: dict[str, list[str]],
    added: dict[str, list[int]],
    local_agent_lines: dict[tuple[str, int], str],
) -> tuple[int, int, int]:
    final_actors = _added_actor_positions(blocks, added)
    ai_lines = {
        position
        for position, actor in final_actors.items()
        if _SESSION_KEY_RE.fullmatch(actor) or _LEGACY_KEY_RE.fullmatch(actor)
    }
    accepted_lines = sum(
        final_actors.get(position) == actor for position, actor in local_agent_lines.items()
    )
    return (
        len(ai_lines),
        sum(len(lines) for lines in added.values()),
        accepted_lines,
    )


def _format_note(
    blocks: dict[str, list[str]],
    metadata: dict[str, Any],
    parent: str,
    sessions: dict[str, dict[str, Any]],
    humans: dict[str, dict[str, str]],
) -> str:
    metadata.setdefault("schema_version", SCHEMA)
    metadata.setdefault("base_commit_sha", parent)
    metadata.setdefault("prompts", {})
    session_metadata = metadata.setdefault("sessions", {})
    human_metadata = metadata.setdefault("humans", {})
    for key, value in sessions.items():
        session_metadata.setdefault(key, value)
    for key, value in humans.items():
        human_metadata.setdefault(key, value)
    metadata.setdefault("git_ai_version", "workflow-controller/0.5.0")
    attestations = [
        line for path, entries in sorted(blocks.items()) for line in (_format_path(path), *entries)
    ]
    return (
        "\n".join(attestations)
        + "\n---\n"
        + json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
