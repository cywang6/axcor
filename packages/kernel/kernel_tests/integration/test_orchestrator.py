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

from unittest.mock import AsyncMock

import pytest

from workflow_controller.config.settings import AppConfig, HookServerConfig, SessionConfig
from workflow_controller.core.evaluator import PassThroughEvaluator
from workflow_controller.core.orchestrator import Orchestrator


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        project_dir=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        sessions=[
            SessionConfig(
                name="worker",
                role="worker",
                cwd=str(tmp_path),
                initial_prompt="test",
            ),
        ],
    )


@pytest.fixture
def orchestrator(config, tmp_path):
    return Orchestrator(config, default_project_dir=tmp_path / "test.db")


class TestOrchestrator:
    async def test_start_with_hook_server_disabled(self, monkeypatch, orchestrator):
        from workflow_controller.hooks.server import HookServer

        start_hook_server = AsyncMock(side_effect=OSError("port in use"))
        monkeypatch.setattr(HookServer, "start", start_hook_server)
        await orchestrator.start()
        start_hook_server.assert_not_awaited()
        assert not orchestrator.hook_server_active
        assert orchestrator.pool.get_by_name("worker") is None
        await orchestrator.start_session("worker")
        session = orchestrator.pool.get_by_name("worker")
        assert session is not None
        assert session.name == "worker"
        await orchestrator.shutdown()

    async def test_hook_bind_failure_aborts_startup(self, monkeypatch, orchestrator):
        from workflow_controller.hooks.server import HookServer, HookServerStartupError

        orchestrator.config.hook_server = HookServerConfig(enabled=True, host="127.0.0.1")
        start_hook_server = AsyncMock(side_effect=OSError("Address already in use"))
        start_reviewer = AsyncMock()
        monkeypatch.setattr(HookServer, "start", start_hook_server)
        monkeypatch.setattr(orchestrator, "_maybe_start_reviewer", start_reviewer)

        with pytest.raises(
            HookServerStartupError, match=r"Hook Server.*127\.0\.0\.1:18765.*Address already in use"
        ):
            await orchestrator.start()

        start_hook_server.assert_awaited_once()
        start_reviewer.assert_not_awaited()
        assert not orchestrator.hook_server_active
        assert not orchestrator.plugin_registry.plugins

    async def test_register_session_captures_task_started(self, orchestrator):
        """sdk 路径的 TaskStartedMessage（subtype=task_started）也要存 sdk_session_id，
        否则 claude_sdk 永远拿不到 id，resume 无 id 可用。
        """
        from workflow_controller.session.events import Event, EventType

        await orchestrator.start()
        session = await orchestrator.pool.create(
            SessionConfig(name="worker", role="worker", cwd=".")
        )
        event = Event.create(
            session_id=session.id,
            iteration=1,
            event_type=EventType.ITERATION_START,
            data={"type": "system", "subtype": "task_started", "session_id": "sdk-xyz"},
        )
        await getattr(orchestrator, "_register_session")(event)
        assert session.sdk_session_id == "sdk-xyz"
        await orchestrator.shutdown()

    async def test_register_evaluator(self, orchestrator):
        evaluator = PassThroughEvaluator()
        orchestrator.register_evaluator("passthrough", evaluator)
        assert "passthrough" in getattr(orchestrator, "_evaluators")
        await orchestrator.shutdown()

    async def test_resolve_evaluator_returns_passthrough_when_no_loop(self, orchestrator):
        config = SessionConfig(name="test", role="worker")
        result = getattr(orchestrator, "_resolve_evaluator")(config)
        assert isinstance(result, PassThroughEvaluator)

    async def test_shutdown_cleans_up(self, orchestrator):
        await orchestrator.start()
        await orchestrator.shutdown()

    async def test_get_session_config_raises_for_unknown(self, orchestrator):
        with pytest.raises(ValueError, match="No session config"):
            getattr(orchestrator, "_get_session_config")("nonexistent")


class TestOrchestratorReviewer:
    async def test_reviewer_started_when_enabled(self, tmp_path):
        from workflow_controller.config.settings import ReviewerConfig

        config = AppConfig(
            project_dir=str(tmp_path),
            harmony_output_path=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
            reviewer=ReviewerConfig(enabled=True),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        assert orch.reviewer is not None
        await orch.shutdown()

    async def test_reviewer_skipped_when_disabled(self, tmp_path):
        from workflow_controller.config.settings import ReviewerConfig

        config = AppConfig(
            project_dir=str(tmp_path),
            harmony_output_path=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
            reviewer=ReviewerConfig(enabled=False),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        assert orch.reviewer is None
        await orch.shutdown()

    async def test_reviewer_skipped_via_env_var(self, tmp_path, monkeypatch):
        from workflow_controller.config.settings import ReviewerConfig

        monkeypatch.setenv("WORKFLOW_CONTROLLER_REVIEWER_DISABLED", "1")
        config = AppConfig(
            project_dir=str(tmp_path),
            harmony_output_path=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
            reviewer=ReviewerConfig(enabled=True),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        assert orch.reviewer is None
        await orch.shutdown()


class TestOrchestratorCliSessionMapping:
    async def test_init_event_records_cli_to_workflow_controller_mapping(self, tmp_path):
        from workflow_controller.session.events import Event, EventType

        config = AppConfig(
            project_dir=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        try:
            event = Event.create(
                session_id="ouro-abc123",
                iteration=1,
                event_type=EventType.ITERATION_START,
                data={
                    "subtype": "init",
                    "session_id": "cli-uuid-xyz",
                },
            )
            await orch.bus.emit(event)
            for _ in range(20):
                import asyncio

                await asyncio.sleep(0.01)
                if orch.resolve_cli_session_id("cli-uuid-xyz") == "ouro-abc123":
                    break

            assert orch.resolve_cli_session_id("cli-uuid-xyz") == "ouro-abc123"
        finally:
            await orch.shutdown()

    async def test_non_init_iteration_start_maps_when_carries_session_id(self, tmp_path):
        """resume 前置: ITERATION_START（sdk 的 task_started subtype）也要映射
        cli→Workflow Controller session —— sdk 路径不发 init subtype，靠 task_started 等子类带 id，
        统一进 ITERATION_START 类型由 _register_session 观察。
        """
        from workflow_controller.session.events import Event, EventType

        config = AppConfig(
            project_dir=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        try:
            event = Event.create(
                session_id="ouro-1",
                iteration=1,
                event_type=EventType.ITERATION_START,
                data={
                    "subtype": "task_started",
                    "session_id": "cli-x",
                },
            )
            await orch.bus.emit(event)
            import asyncio

            for _ in range(10):
                await asyncio.sleep(0.01)

            assert orch.resolve_cli_session_id("cli-x") == "ouro-1"
        finally:
            await orch.shutdown()

    async def test_resolve_unknown_returns_none(self, tmp_path):
        config = AppConfig(
            project_dir=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
        )
        orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
        await orch.start()
        try:
            assert orch.resolve_cli_session_id("nonexistent") is None
        finally:
            await orch.shutdown()
