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

import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.engine import ProvenanceEngine
from workflow_controller.plugins.file_provenance.git_notes import CommitFinalizer, _added_lines
from workflow_controller.plugins.file_provenance.store import LineAttribution, ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, input=input_text, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path) -> str:
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "code.py").write_text("base\n")
    _git(path, "add", "code.py")
    _git(path, "commit", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


def _context(path: Path, capture_id: str) -> FileChangeContext:
    return FileChangeContext(
        capture_id,
        "agent",
        "session-1",
        "native-session",
        None,
        "opencode",
        "test-model",
        "worker",
        "write",
        f"tool-{capture_id}",
        path,
        {"file_path": "code.py"},
        datetime.now(tz=UTC),
        {},
        "agent-1",
    )


def _outcome(*, incomplete: bool = False) -> FileChangeOutcome:
    return FileChangeOutcome(not incomplete, incomplete, {}, (), datetime.now(tz=UTC), incomplete)


async def _capture_line(
    path: Path,
    engine: ProvenanceEngine,
    capture_id: str,
    line: str,
) -> None:
    context = _context(path, capture_id)
    await engine.before(context)
    target = path / "code.py"
    target.write_text(target.read_text() + line + "\n")
    await engine.after(context, _outcome())


def _read_note(path: Path, commit: str) -> str:
    return _git(path, "notes", "--ref=refs/notes/ai", "show", commit)


def _setup(path: Path) -> tuple[str, ProvenanceStore, ProvenanceEngine]:
    parent = _init_repo(path)
    store = ProvenanceStore(path)
    store.set_state("last_commit", parent)
    return parent, store, ProvenanceEngine(path, store)


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "code.py")
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _add_note(path: Path, commit: str, note: str) -> None:
    _git(path, "notes", "--ref=refs/notes/ai", "add", "-F", "-", commit, input_text=note)


@pytest.mark.parametrize(
    "hunk",
    [
        "@@ -1 +1,2 @@\n-a\n\\ No newline at end of file\n+a\n+appended\n",
        "@@ -0,0 +1,2 @@\n+++ b/not-a-header\n+tail\n",
    ],
)
def test_added_lines_handles_diff_markers_as_content(hunk: str) -> None:
    diff = f"diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n{hunk}"

    assert _added_lines(diff) == {"code.py": [1, 2]}


async def test_commit_note_and_metrics_are_idempotent(tmp_path: Path) -> None:
    parent, store, engine = _setup(tmp_path)
    await _capture_line(tmp_path, engine, "capture-1", "agent line")
    head = _commit(tmp_path, "agent edit")
    finalizer = CommitFinalizer(tmp_path, store)
    await finalizer.finalize_head()
    note = _read_note(tmp_path, head)
    attestations, raw_metadata = note.split("\n---\n", 1)
    metadata = json.loads(raw_metadata)
    assert "code.py" in attestations
    assert "  s_" in attestations and "::t_" in attestations
    assert metadata["schema_version"] == "authorship/3.0.0"
    assert metadata["base_commit_sha"] == parent
    assert metadata["sessions"]
    assert store.get_capture("capture-1")["committed_sha"] == head
    store.set_state("last_commit", parent)
    await finalizer.finalize_head()
    with sqlite3.connect(store.db_path) as conn:
        metric = conn.execute(
            "SELECT ai_lines,total_added_lines,generated_lines,accepted_lines "
            "FROM commits WHERE commit_sha=?",
            (head,),
        ).fetchone()
        saved_lines = conn.execute(
            "SELECT COUNT(*) FROM commit_lines WHERE commit_sha=?",
            (head,),
        ).fetchone()[0]
    assert metric == (1, 1, 1, 1)
    assert saved_lines == 2


async def test_existing_note_ranges_take_priority(tmp_path: Path) -> None:
    parent, store, engine = _setup(tmp_path)
    await _capture_line(tmp_path, engine, "capture-2", "agent line")
    head = _commit(tmp_path, "mixed edit")
    human = "h_0123456789abcd"
    existing = (
        "code.py\n  "
        + human
        + " 1\n---\n"
        + json.dumps(
            {
                "schema_version": "authorship/3.0.0",
                "base_commit_sha": parent,
                "prompts": {},
                "humans": {human: {"author": "Test User <test@example.com>"}},
            }
        )
    )
    _add_note(tmp_path, head, existing)
    await CommitFinalizer(tmp_path, store).finalize_head()
    attestations = _read_note(tmp_path, head).split("\n---\n", 1)[0]
    assert attestations.splitlines().count("code.py") == 1
    assert f"  {human} 1" in attestations
    assert "::t_" in attestations


async def test_invalid_existing_note_is_preserved_and_consumed(tmp_path: Path) -> None:
    _parent, store, engine = _setup(tmp_path)
    await _capture_line(tmp_path, engine, "capture-3", "agent line")
    head = _commit(tmp_path, "edit")
    _add_note(tmp_path, head, "not an authorship note")
    await CommitFinalizer(tmp_path, store).finalize_head()
    assert _read_note(tmp_path, head) == "not an authorship note"
    assert store.get_capture("capture-3")["committed_sha"] == head
    assert store.get_state("last_commit") == head
    with sqlite3.connect(store.db_path) as conn:
        conflict = conn.execute(
            "SELECT kind FROM conflicts WHERE capture_id IS NULL",
        ).fetchone()
    assert conflict == ("invalid_existing_note",)


async def test_multiple_commits_use_their_capture_boundary(tmp_path: Path) -> None:
    _base, store, engine = _setup(tmp_path)
    commits: list[str] = []
    for capture_id, line in (("first", "one"), ("second", "two")):
        await _capture_line(tmp_path, engine, capture_id, line)
        commits.append(_commit(tmp_path, line))

    await CommitFinalizer(tmp_path, store).finalize_head()
    assert store.get_capture("first")["committed_sha"] == commits[0]
    assert store.get_capture("second")["committed_sha"] == commits[1]
    assert "::t_" in _read_note(tmp_path, commits[0])
    assert "::t_" in _read_note(tmp_path, commits[1])


async def test_history_rewrite_records_conflict_and_recovers_cursor(tmp_path: Path) -> None:
    base, store, _engine = _setup(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("base\nold branch\n")
    _commit(tmp_path, "old branch")
    finalizer = CommitFinalizer(tmp_path, store)
    await finalizer.finalize_head()

    _git(tmp_path, "reset", "--hard", base)
    target.write_text("base\nnew branch\n")
    new_head = _commit(tmp_path, "new branch")
    await finalizer.finalize_head()
    assert store.get_state("last_commit") == new_head
    with sqlite3.connect(store.db_path) as conn:
        conflict = conn.execute(
            "SELECT 1 FROM conflicts WHERE capture_id IS NULL AND kind='history_rewritten'"
        ).fetchone()
    assert conflict is not None


async def test_refinalize_does_not_advance_a_lagging_cursor(tmp_path: Path) -> None:
    base, store, _engine = _setup(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("base\nfirst\n")
    first = _commit(tmp_path, "first")
    target.write_text("base\nfirst\nsecond\n")
    second = _commit(tmp_path, "second")
    finalizer = CommitFinalizer(tmp_path, store)

    assert await finalizer.refinalize_head() is None
    assert store.get_state("last_commit") == base
    assert store.get_commit_metrics(first) is None
    assert store.get_commit_metrics(second) is None

    assert await finalizer.finalize_head() == second
    assert store.get_commit_metrics(first) is not None
    assert store.get_commit_metrics(second) is not None


async def test_human_claim_can_refinalize_current_commit(tmp_path: Path) -> None:
    parent = _init_repo(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("base\nhuman line\n")
    _git(tmp_path, "add", "code.py")
    _git(tmp_path, "commit", "-m", "human edit")
    head = _git(tmp_path, "rev-parse", "HEAD")
    content = target.read_bytes()
    store = ProvenanceStore(tmp_path)
    store.set_state("last_commit", parent)
    store.save_file_state(
        "code.py",
        content,
        [LineAttribution("unknown"), LineAttribution("unknown")],
        None,
    )
    finalizer = CommitFinalizer(tmp_path, store)
    await finalizer.finalize_head()
    store.claim_human(
        "code.py",
        2,
        2,
        "Test User <test@example.com>",
        hashlib.sha256(content).hexdigest(),
    )

    await finalizer.refinalize_head()
    note = _read_note(tmp_path, head)
    assert "  h_" in note
    assert " 2\n---\n" in note
