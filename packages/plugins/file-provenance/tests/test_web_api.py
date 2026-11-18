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
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from workflow_controller.core.file_changes import FileChangeRegistry
from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugin.context import PluginContext, PluginServices
from workflow_controller.plugins.file_provenance.plugin import FileProvenancePlugin
from workflow_controller.plugins.file_provenance.store import LineAttribution, ProvenanceStore

if TYPE_CHECKING:
    from pathlib import Path


def _plugin(
    project: Path,
    *,
    services: PluginServices | None = None,
) -> tuple[FileProvenancePlugin, ProvenanceStore]:
    plugin = FileProvenancePlugin(
        PluginConfig("file_provenance", "0.1.0", enabled=True),
        project / "config.yaml",
    )
    plugin.install(
        PluginContext(
            bus=MagicMock(),
            type_registry=MagicMock(),
            hook_server=MagicMock(),
            file_changes=FileChangeRegistry(),
            work_dir=project,
            services=services or PluginServices(),
        )
    )
    store = plugin.store_for(project)
    assert store is not None
    return plugin, store


async def test_audit_evidence_download_headers_and_missing_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    services = PluginServices(
        get_session_metadata=lambda session_id: (
            {
                "id": session_id,
                "runtime": "opencode",
                "initial_prompt": "task",
                "project_dir": str(project),
            }
            if session_id == "session-1"
            else None
        ),
        get_session_events=lambda _session_id: [],
    )
    plugin, _store = _plugin(project, services=services)
    async with AsyncClient(
        transport=ASGITransport(app=_app(plugin)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/provenance/sessions/session-1/audit-evidence",
            params={"project_dir": str(project)},
        )
        missing = await client.get(
            "/api/provenance/sessions/missing/audit-evidence",
            params={"project_dir": str(project)},
        )
    plugin.uninstall()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "workflow-controller-audit-session-1.json" in response.headers["content-disposition"]
    assert response.json()["version"] == 2
    assert missing.status_code == 404


def _app(plugin: FileProvenancePlugin) -> FastAPI:
    app = FastAPI()
    app.state.api = SimpleNamespace(
        plugin_by_name=lambda name: plugin if name == plugin.config.name else None
    )
    app.include_router(plugin.get_web_router())
    return app


def _human_claim_body(
    project: Path,
    digest: str,
    *,
    author: str = "Test User <test@example.com>",
) -> dict:
    """claim-human 端点请求体（默认带合法作者签名）。"""
    return {
        "project_dir": str(project),
        "path": "a.txt",
        "start_line": 1,
        "end_line": 1,
        "author": author,
        "expected_content_hash": digest,
    }


async def test_unknown_project_and_missing_evidence_use_http_statuses(tmp_path: Path) -> None:
    known = tmp_path / "known"
    unknown = tmp_path / "unknown"
    known.mkdir()
    unknown.mkdir()
    plugin, _store = _plugin(known)
    async with AsyncClient(
        transport=ASGITransport(app=_app(plugin)),
        base_url="http://test",
    ) as client:
        forbidden = await client.get(
            "/api/provenance/files",
            params={"project_dir": str(unknown)},
        )
        missing = await client.get(
            "/api/provenance/captures/missing",
            params={"project_dir": str(known)},
        )
        invalid_stage = await client.get(
            "/api/provenance/captures/missing/file",
            params={"project_dir": str(known), "path": "a.txt", "stage": "middle"},
        )
    plugin.uninstall()
    assert forbidden.status_code == 403
    assert not (unknown / ".workflow-controller").exists()
    assert missing.status_code == 404
    assert invalid_stage.status_code == 422


async def _query_and_claim_responses(
    client: AsyncClient,
    project: Path,
    digest: str,
) -> dict:
    """依次调用查询与 claim-human 端点，返回各响应（中途改文件制造 stale）。"""
    dir_param = {"project_dir": str(project)}
    responses = {
        "files": await client.get("/api/provenance/files", params=dir_param),
        "blame": await client.get("/api/provenance/blame", params={**dir_param, "path": "a.txt"}),
        "commits": await client.get("/api/provenance/commits", params=dir_param),
        "metrics": await client.get(
            "/api/provenance/metrics", params={**dir_param, "commit": "commit-1"}
        ),
        "missing_metrics": await client.get(
            "/api/provenance/metrics", params={**dir_param, "commit": "missing"}
        ),
        "claimed": await client.post(
            "/api/provenance/claim-human", json=_human_claim_body(project, digest)
        ),
        "invalid_author": await client.post(
            "/api/provenance/claim-human",
            json=_human_claim_body(project, digest, author="invalid"),
        ),
        "duplicate": await client.post(
            "/api/provenance/claim-human", json=_human_claim_body(project, digest)
        ),
    }
    (project / "a.txt").write_text("changed\n")
    responses["stale"] = await client.post(
        "/api/provenance/claim-human", json=_human_claim_body(project, digest)
    )
    return responses


async def test_query_and_human_claim_endpoints(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    content = b"unclaimed\n"
    (project / "a.txt").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    plugin, store = _plugin(project)
    store.save_file_state("a.txt", content, [LineAttribution("unknown")], None)
    store.record_commit_lines("commit-1", "a.txt", ["unclaimed"], [LineAttribution("unknown")])
    store.finalize_commit("commit-1", "parent", 0, 1, 0)
    async with AsyncClient(
        transport=ASGITransport(app=_app(plugin)),
        base_url="http://test",
    ) as client:
        resp = await _query_and_claim_responses(client, project, digest)
    plugin.uninstall()
    assert resp["files"].json() == {"files": ["a.txt"]}
    assert resp["blame"].json()["content_hash"] == digest
    assert resp["commits"].json()["commits"][0]["commit_sha"] == "commit-1"
    assert resp["metrics"].json()["total_added_lines"] == 1
    assert resp["missing_metrics"].status_code == 404
    assert resp["claimed"].json() == {"ok": True, "claimed_lines": 1}
    assert resp["invalid_author"].status_code == 422
    assert resp["duplicate"].status_code == 422
    assert resp["stale"].status_code == 409
    assert store.blame("a.txt")[0]["actor_kind"] == "human"
