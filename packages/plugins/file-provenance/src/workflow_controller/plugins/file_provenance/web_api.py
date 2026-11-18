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

"""Browser API for file provenance evidence and attribution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from workflow_controller.plugin.sdk import PluginRouter, plugin_router

if TYPE_CHECKING:
    from workflow_controller.plugins.file_provenance.plugin import FileProvenancePlugin
    from workflow_controller.plugins.file_provenance.store import ProvenanceStore


class HumanClaim(BaseModel):
    project_dir: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    author: str = Field(min_length=3)
    commit: str = ""
    expected_content_hash: str = Field(min_length=64, max_length=64)


def build_router() -> APIRouter:
    owner = plugin_router("file_provenance", prefix="/api/provenance")
    router = owner.router

    @router.get("/files")
    async def files(request: Request, project_dir: str, commit: str = "") -> dict[str, Any]:
        return await _files(owner, request, project_dir, commit)

    @router.get("/commits")
    async def commits(
        request: Request,
        project_dir: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await _commits(owner, request, project_dir, limit)

    @router.get("/blame")
    async def blame(
        request: Request,
        project_dir: str,
        path: str,
        commit: str = "",
    ) -> dict[str, Any]:
        return await _blame(owner, request, project_dir, path, commit)

    @router.get("/metrics")
    async def metrics(request: Request, project_dir: str, commit: str = "") -> dict[str, Any]:
        return await _metrics(owner, request, project_dir, commit)

    @router.get("/captures/{capture_id}")
    async def capture(capture_id: str, request: Request, project_dir: str) -> dict[str, Any]:
        return await _capture(owner, request, capture_id, project_dir)

    @router.get("/captures/{capture_id}/file")
    async def capture_file(
        capture_id: str,
        request: Request,
        project_dir: str,
        path: str,
        stage: Literal["before", "after"] = "after",
    ) -> dict[str, Any]:
        return await _capture_file(owner, request, capture_id, project_dir, path, stage)

    @router.get("/sessions/{session_id}/audit-evidence")
    async def audit_evidence(
        session_id: str,
        request: Request,
        project_dir: str,
    ) -> JSONResponse:
        return await _audit_evidence(owner, request, session_id, project_dir)

    @router.post("/claim-human")
    async def claim_human(claim: HumanClaim, request: Request) -> dict[str, Any]:
        return await _claim_human(owner, claim, request)

    return router


_CLAIM_CONFLICTS = (
    "file provenance changed",
    "working tree file changed",
)


def _empty_metrics(commit: str) -> dict[str, Any]:
    return {
        "commit_sha": commit,
        "ai_lines": 0,
        "total_added_lines": 0,
        "generated_lines": 0,
        "accepted_lines": 0,
        "ai_share": 0.0,
        "acceptance_rate": 0.0,
    }


def _plugin(owner: PluginRouter, request: Request) -> FileProvenancePlugin:
    plugin = owner.get_plugin(request)
    if plugin is None or not plugin.config.enabled:
        raise HTTPException(status_code=503, detail="file provenance plugin is disabled")
    return cast("FileProvenancePlugin", plugin)


async def _store(owner: PluginRouter, request: Request, project_dir: str) -> ProvenanceStore:
    target = await asyncio.to_thread(_plugin(owner, request).store_for, project_dir)
    if target is None:
        raise HTTPException(status_code=403, detail="project is not tracked by file provenance")
    return target


async def _files(
    owner: PluginRouter,
    request: Request,
    project_dir: str,
    commit: str,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    return {"files": await asyncio.to_thread(target.list_files, commit)}


async def _commits(
    owner: PluginRouter,
    request: Request,
    project_dir: str,
    limit: int,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    try:
        values = await asyncio.to_thread(target.list_commits, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"commits": values}


async def _blame(
    owner: PluginRouter,
    request: Request,
    project_dir: str,
    path: str,
    commit: str,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    try:
        lines = await asyncio.to_thread(target.blame, path, commit)
        content_hash = (
            None if commit else await asyncio.to_thread(target.current_content_hash, path)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "path": path,
        "commit": commit,
        "content_hash": content_hash,
        "lines": lines,
    }


async def _metrics(
    owner: PluginRouter,
    request: Request,
    project_dir: str,
    commit: str,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    row = await asyncio.to_thread(target.get_commit_metrics, commit)
    if row is None:
        if commit:
            raise HTTPException(status_code=404, detail="commit metrics not found")
        return _empty_metrics(commit)
    total = int(row["total_added_lines"])
    generated = int(row["generated_lines"])
    return {
        **row,
        "ai_share": int(row["ai_lines"]) / total if total else 0.0,
        "acceptance_rate": int(row["accepted_lines"]) / generated if generated else 0.0,
    }


async def _capture(
    owner: PluginRouter,
    request: Request,
    capture_id: str,
    project_dir: str,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    value = await asyncio.to_thread(target.get_capture, capture_id)
    if value is None:
        raise HTTPException(status_code=404, detail="capture not found")
    return value


async def _capture_file(
    owner: PluginRouter,
    request: Request,
    capture_id: str,
    project_dir: str,
    path: str,
    stage: str,
) -> dict[str, Any]:
    target = await _store(owner, request, project_dir)
    value = await asyncio.to_thread(
        target.get_capture_file,
        capture_id,
        stage,
        path,
    )
    if value is None:
        raise HTTPException(status_code=404, detail="capture file not found")
    return value


async def _audit_evidence(
    owner: PluginRouter,
    request: Request,
    session_id: str,
    project_dir: str,
) -> JSONResponse:
    plugin = _plugin(owner, request)
    await _store(owner, request, project_dir)
    try:
        value = await plugin.export_audit_evidence(project_dir, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        value,
        headers={
            "Content-Disposition": f'attachment; filename="workflow-controller-audit-{session_id}.json"',
            "Cache-Control": "no-store",
        },
    )


async def _claim_human(
    owner: PluginRouter,
    claim: HumanClaim,
    request: Request,
) -> dict[str, Any]:
    plugin = _plugin(owner, request)
    await _store(owner, request, claim.project_dir)
    try:
        claimed = await plugin.claim_human(
            claim.project_dir,
            claim.path,
            claim.start_line,
            claim.end_line,
            claim.author,
            commit=claim.commit,
            expected_content_hash=claim.expected_content_hash,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 409 if detail.startswith(_CLAIM_CONFLICTS) else 422
        raise HTTPException(status_code=status, detail=detail) from exc
    return {"ok": True, "claimed_lines": claimed}
