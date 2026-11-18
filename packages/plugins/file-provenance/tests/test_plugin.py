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
import subprocess
from datetime import datetime
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from workflow_controller.core.file_changes import FileChangeRegistry
from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugin.context import PluginContext
from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.plugin import FileProvenancePlugin
from workflow_controller.plugins.file_provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path


def _context(cwd: Path, capture_id: str = "capture-1") -> FileChangeContext:
    args = [
        capture_id,
        "agent",
        "session-1",
        None,
        None,
        "claude_sdk",
        "test-model",
        None,
        "Write",
        f"tool-{capture_id}",
        cwd,
        {"file_path": "file.txt"},
        datetime(2026, 8, 21, 10, 0, 0),
    ]
    return FileChangeContext(*args)


def _outcome() -> FileChangeOutcome:
    args = [True, False, {"status": "done"}, (), datetime(2026, 8, 21, 10, 0, 1)]
    return FileChangeOutcome(*args)


def _plugin(tmp_path: Path) -> FileProvenancePlugin:
    config = PluginConfig("file_provenance", "0.1.0", enabled=True)
    return FileProvenancePlugin(config, tmp_path / "config.yaml")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _context_for_plugin(
    registry: FileChangeRegistry | None,
    tmp_path: Path,
) -> PluginContext:
    return PluginContext(
        bus=MagicMock(),
        type_registry=MagicMock(),
        hook_server=MagicMock(),
        file_changes=registry,
        work_dir=tmp_path,
    )


def test_install_and_uninstall_manage_capture_subscription(tmp_path) -> None:
    registry = FileChangeRegistry()
    plugin = _plugin(tmp_path)

    plugin.install(_context_for_plugin(registry, tmp_path))
    assert registry.has_subscribers is True

    plugin.uninstall()
    assert registry.has_subscribers is False


async def test_plugin_persists_capture_at_nearest_worktree(tmp_path) -> None:
    project = tmp_path / "project"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    _git(project, "init")
    _git(
        project,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    head = _git(project, "rev-parse", "HEAD")
    registry = FileChangeRegistry()
    plugin = _plugin(tmp_path)
    plugin.install(_context_for_plugin(registry, tmp_path))
    capture = _context(cwd)

    await registry.before(capture)
    await registry.after(capture, _outcome())

    store = ProvenanceStore(project)
    value = store.get_capture(capture.capture_id)
    assert value is not None
    assert value["finished_at"] == _outcome().finished_at.isoformat()
    assert value["conflicted"] is False
    assert store.get_state("last_commit") == head
    assert not (cwd / ".workflow-controller").exists()
    plugin.uninstall()


async def test_uninstall_keeps_inflight_capture_closable(tmp_path) -> None:
    registry = FileChangeRegistry()
    plugin = _plugin(tmp_path)
    plugin.install(_context_for_plugin(registry, tmp_path))
    capture = _context(tmp_path)
    await registry.before(capture)

    plugin.uninstall()
    await registry.after(capture, _outcome())

    value = ProvenanceStore(tmp_path).get_capture(capture.capture_id)
    assert value is not None
    assert value["finished_at"] is not None


async def test_cancelled_before_handler_closes_started_capture(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _plugin(tmp_path)
    entered = ThreadEvent()
    release = ThreadEvent()
    original = getattr(plugin, "_begin_capture")

    async def blocked_begin(context: FileChangeContext) -> None:
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        await original(context)

    monkeypatch.setattr(plugin, "_begin_capture", blocked_begin)
    capture = _context(tmp_path)
    task = asyncio.create_task(getattr(plugin, "_before_capture")(capture))
    assert await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    value = ProvenanceStore(tmp_path).get_capture(capture.capture_id)
    assert value is not None
    assert value["incomplete"] is True
    assert value["finished_at"] is not None
