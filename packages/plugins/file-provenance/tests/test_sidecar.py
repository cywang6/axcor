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
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome
from workflow_controller.plugins.file_provenance.sidecar import PROTOCOL_VERSION, ProvenanceSidecar
from workflow_controller.plugins.file_provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path


def _context(project: Path, capture_id: str = "capture-1") -> FileChangeContext:
    return FileChangeContext(
        capture_id=capture_id,
        actor_kind="agent",
        workflow_controller_session_id=None,
        native_session_id="open-session",
        parent_native_session_id=None,
        runtime="opencode",
        model="provider/model",
        agent_name="build",
        native_agent_id="build",
        tool_name="write",
        tool_use_id=f"call-{capture_id}",
        cwd=project,
        tool_input={"file_path": "example.py"},
        started_at=datetime.now(tz=UTC),
        attributes={"prompt": "update example.py"},
    )


def _outcome(*, incomplete: bool = False) -> FileChangeOutcome:
    return FileChangeOutcome(
        succeeded=not incomplete,
        is_error=incomplete,
        tool_output={"status": "done"},
        path_hints=(),
        finished_at=datetime.now(tz=UTC),
        incomplete=incomplete,
    )


async def test_sidecar_records_mutation_without_git_notes(tmp_path: Path) -> None:
    target = tmp_path / "example.py"
    target.write_text("before\n")
    sidecar = ProvenanceSidecar(tmp_path)
    context = _context(tmp_path)

    await getattr(sidecar, "_before")({"context": context.to_dict()})
    target.write_text("after\n")
    result = await getattr(sidecar, "_after")(
        {
            "capture_id": context.capture_id,
            "outcome": _outcome().to_dict(),
        }
    )

    capture = ProvenanceStore(tmp_path).get_capture(context.capture_id)
    assert result == {"captureId": context.capture_id, "closed": True, "mutations": 1}
    assert capture is not None
    assert capture["native_session_id"] == "open-session"
    assert capture["runtime"] == "opencode"
    assert capture["attributes"]["prompt"] == "update example.py"
    assert not (tmp_path / ".git" / "refs" / "notes" / "ai").exists()


async def test_sidecar_rejects_cwd_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    sidecar = ProvenanceSidecar(project)

    with pytest.raises(ValueError, match="outside project"):
        await getattr(sidecar, "_before")({"context": _context(outside).to_dict()})


async def test_sidecar_duplicate_after_is_idempotent(tmp_path: Path) -> None:
    sidecar = ProvenanceSidecar(tmp_path)
    context = _context(tmp_path)
    await getattr(sidecar, "_before")({"context": context.to_dict()})

    first = await getattr(sidecar, "_after")(
        {
            "capture_id": context.capture_id,
            "outcome": _outcome().to_dict(),
        }
    )
    second = await getattr(sidecar, "_after")(
        {
            "capture_id": context.capture_id,
            "outcome": _outcome().to_dict(),
        }
    )

    assert first["closed"] is True
    assert second["closed"] is False


async def test_sidecar_malformed_after_can_be_retried(tmp_path: Path) -> None:
    sidecar = ProvenanceSidecar(tmp_path)
    context = _context(tmp_path)
    await getattr(sidecar, "_before")({"context": context.to_dict()})

    malformed = _outcome().to_dict()
    del malformed["finished_at"]
    with pytest.raises(KeyError):
        await getattr(sidecar, "_after")({"capture_id": context.capture_id, "outcome": malformed})

    result = await getattr(sidecar, "_after")(
        {
            "capture_id": context.capture_id,
            "outcome": _outcome().to_dict(),
        }
    )
    assert result["closed"] is True


async def test_sidecar_shutdown_marks_active_capture_incomplete(tmp_path: Path) -> None:
    sidecar = ProvenanceSidecar(tmp_path)
    context = _context(tmp_path)
    await getattr(sidecar, "_before")({"context": context.to_dict()})

    await getattr(sidecar, "_close_active")("test shutdown")

    capture = ProvenanceStore(tmp_path).get_capture(context.capture_id)
    assert capture is not None
    assert capture["incomplete"] is True
    assert capture["conflicted"] is True


async def test_overlapping_sidecar_captures_are_conflicted(tmp_path: Path) -> None:
    sidecar = ProvenanceSidecar(tmp_path)
    first = _context(tmp_path, "capture-1")
    second = _context(tmp_path, "capture-2")
    await getattr(sidecar, "_before")({"context": first.to_dict()})
    await getattr(sidecar, "_before")({"context": second.to_dict()})

    await getattr(sidecar, "_after")(
        {"capture_id": first.capture_id, "outcome": _outcome().to_dict()}
    )
    await getattr(sidecar, "_after")(
        {"capture_id": second.capture_id, "outcome": _outcome().to_dict()}
    )

    store = ProvenanceStore(tmp_path)
    assert store.get_capture(first.capture_id)["conflicted"] is True
    assert store.get_capture(second.capture_id)["conflicted"] is True


async def test_cli_sidecar_ping_and_shutdown(tmp_path: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "workflow_controller",
        "provenance",
        "serve",
        "--project",
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(b"not-json\n")
    await proc.stdin.drain()
    malformed = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
    assert malformed["id"] is None
    assert malformed["ok"] is False

    proc.stdin.write(b'{"id":1,"method":"ping","params":{}}\n')
    await proc.stdin.drain()
    ping = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
    assert ping["ok"] is True
    assert ping["result"]["protocolVersion"] == PROTOCOL_VERSION

    proc.stdin.write(b'{"id":2,"method":"shutdown","params":{}}\n')
    await proc.stdin.drain()
    shutdown = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
    assert shutdown == {"id": 2, "ok": True, "result": {"closed": True}}
    assert await asyncio.wait_for(proc.wait(), 5) == 0
