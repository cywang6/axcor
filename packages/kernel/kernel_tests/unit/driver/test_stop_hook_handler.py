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

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from workflow_controller.core.hook_decision import HookDecision
from workflow_controller.core.orchestrator import Orchestrator

from ._hook_handler_stubs import hook_orch_test_config

if TYPE_CHECKING:
    from pathlib import Path

    from workflow_controller.config.settings import AppConfig


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    return hook_orch_test_config(tmp_path)


@pytest.fixture
async def orch(base_config: AppConfig, tmp_path: Path):
    orchestrator = Orchestrator(base_config, default_project_dir=tmp_path / "stop.db")
    await orchestrator.start()
    yield orchestrator
    await orchestrator.shutdown()


def _payload(
    cli_session_id: str = "cli-uuid-1",
    last_message: str = "spec 阶段完成",
) -> dict[str, Any]:
    return {
        "session_id": cli_session_id,
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "Stop",
        "last_assistant_message": last_message,
    }


def _patch_reviewer(orch: Orchestrator, decision: HookDecision) -> list[str]:
    """Replace decide_stop with a stub that returns ``decision`` and logs calls."""
    call_log: list[str] = []

    async def stub(context: str) -> HookDecision:
        call_log.append(context)
        return decision

    orch.reviewer.decide_stop = stub  # type: ignore[method-assign]
    return call_log


class TestStopHookBasicGating:
    async def test_returns_none_when_reviewer_disabled(
        self,
        base_config: AppConfig,
        tmp_path: Path,
    ) -> None:
        base_config.reviewer.enabled = False
        orchestrator = Orchestrator(base_config, default_project_dir=tmp_path / "x.db")
        await orchestrator.start()
        try:
            assert orchestrator.reviewer is None
            result = await getattr(orchestrator, "_handle_stop_request")("cli-1", _payload())
            assert result is None
        finally:
            await orchestrator.shutdown()

    async def test_returns_none_when_last_message_empty(self, orch) -> None:
        getattr(orch, "_cli_to_workflow_controller_session")["cli-1"] = "ouro-1"
        result = await getattr(orch, "_handle_stop_request")(
            "cli-1",
            _payload(cli_session_id="cli-1", last_message=""),
        )
        assert result is None

    async def test_returns_none_when_cli_session_unknown(self, orch) -> None:
        # decide_stop would be invoked then result dropped because no ouro mapping
        _patch_reviewer(orch, HookDecision("let_exit", "", "n/a"))
        result = await getattr(orch, "_handle_stop_request")("cli-unknown", _payload())
        assert result is None

    async def test_returns_none_in_manual_mode(self, monkeypatch, orch) -> None:
        getattr(orch, "_cli_to_workflow_controller_session")["cli-1"] = "ouro-1"
        monkeypatch.setattr(orch, "_reviewer_mode", "manual")
        call_log = _patch_reviewer(orch, HookDecision("block_with_feedback", "x", ""))
        result = await getattr(orch, "_handle_stop_request")("cli-1", _payload())
        assert result is None
        assert call_log == [], "manual mode must not invoke decide_stop"


def _worker_session() -> MagicMock:
    """A mock session whose role passes the orchestrator's worker gate.

    The gate in _handle_stop_request returns early for any role other than
    "worker"/"". MagicMock(name=...) only sets the repr name, NOT .config.role,
    so role must be assigned explicitly.
    """
    s = MagicMock(name="worker")
    s.config.role = "worker"
    return s


class TestBoundaryResolve:
    """_resolve_boundary_pending：iteration-boundary gate 放行后的 staged 收尾——
    pending → per-stage reviewer；stages_complete → 确定性完成；无 → 放行。
    """

    async def test_pending_runs_stage_review(self, orch) -> None:
        sess = _worker_session()
        sess.id = "br-1"
        sess.last_worker_text = "spec done"
        sess.pending_stage_review = {
            "done": {"name": "spec", "artifact": "spec.md"},
            "next": {"name": "plan", "artifact": "plan.md"},
            "done_index": 0,
        }

        async def stub_review(context, stage, next_stage):
            return HookDecision(action="block_with_feedback", feedback="", rationale="ok")

        orch.reviewer.review_stage = stub_review  # type: ignore[method-assign]

        res = await orch.resolve_boundary_pending(sess, 1)

        assert res is not None and res.action == "block"
        assert sess.pending_stage_review is None  # 消费

    async def test_stages_complete_auto_marks_let_exit(self, monkeypatch, orch) -> None:
        sess = _worker_session()
        sess.id = "br-2"
        sess.pending_stage_review = None
        sess.stages_complete = True

        engine = MagicMock()
        monkeypatch.setattr(orch, "_single_loop", engine)
        monkeypatch.setattr(engine, "_current_session", sess)

        res = await orch.resolve_boundary_pending(sess, 1)

        assert res is None  # 放行（let_exit flag 让 loop COMPLETED）
        assert sess.stages_complete is False  # 一次性消费
        engine.mark_reviewer_let_exit.assert_called_once()  # auto → 标完成

    async def test_stages_complete_manual_no_mark(self, monkeypatch, orch) -> None:
        monkeypatch.setattr(orch, "_reviewer_mode", "manual")
        sess = _worker_session()
        sess.id = "br-3"
        sess.pending_stage_review = None
        sess.stages_complete = True

        engine = MagicMock()
        monkeypatch.setattr(orch, "_single_loop", engine)
        monkeypatch.setattr(engine, "_current_session", sess)

        res = await orch.resolve_boundary_pending(sess, 1)

        assert res is None
        assert sess.stages_complete is False
        engine.mark_reviewer_let_exit.assert_not_called()  # manual → 落 manual-prompt

    async def test_neither_returns_none(self, orch) -> None:
        sess = _worker_session()
        sess.id = "br-4"
        sess.pending_stage_review = None
        sess.stages_complete = False

        res = await orch.resolve_boundary_pending(sess, 1)
        assert res is None
