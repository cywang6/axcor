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

"""Global session instruction overlay (opencode-native).

Tests the B1 feature from docs/config-overlay-expansion-design.md §5 (B1):
global instructions appended to ALL agents' system prompts via the opencode
``instructions`` config section. Complementary to the per-type subagent hint
(docs/subagent-prompt-injection-design.md): hints are per-type + prepended;
instructions reach every agent in one registration + appended.

Three surfaces (mirrors test_subagent_hint.py structure):

- ``workflow_controller.driver.opencode_gate`` — ``build_config_content`` writes the combined
  instructions to a temp file and sets ``cfg["instructions"] = [path]``;
  ``_wrap_session_instruction`` wraps the text in a delimited block.
- ``Orchestrator`` — the per-session ``_session_instructions`` registry
  (register / clear / get) + the project-level ``_project_instructions`` mirror,
  wired into ``DriverHooks`` so the opencode driver reads live instructions at
  query() time.
- ``SessionAPI`` — register / clear / get, with an auditable
  ``PROMPT_OVERLAY_REGISTERED`` / ``PROMPT_OVERLAY_CLEARED`` event per mutation
  (emitted via ``bus.emit``; the legacy ``SESSION_INSTRUCTION`` event is removed).

The overlay lives only in ``OPENCODE_CONFIG_CONTENT`` (env var) + an in-memory
registry + a temp file; the disk ``.md`` is never touched (reversible,
session-scoped).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.api.session_api import SessionAPI
from workflow_controller.config.settings import AppConfig, HookServerConfig, ReviewerConfig
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.driver.opencode_gate import (
    _INSTRUCTION_FOOTER,
    _INSTRUCTION_HEADER,
    _wrap_session_instruction,
    build_config_content,
)
from workflow_controller.session.events import EventType

from ._hook_handler_stubs import (
    bare_echo_opencode_query,
    captured_echo_opencode_query,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── _wrap_session_instruction ───────────────────────────────────────────


class TestWrapInstruction:
    @staticmethod
    def test_wraps_in_fixed_block():
        out = _wrap_session_instruction("target arch is arch32")
        assert out.startswith(_INSTRUCTION_HEADER)
        assert out.endswith(_INSTRUCTION_FOOTER)
        assert "target arch is arch32" in out

    @staticmethod
    def test_header_states_all_agents_and_priority():
        # the wrapper must tell agents this is a session-scoped user supplement
        # that applies to all agents and has priority over workflow defaults
        out = _wrap_session_instruction("x")
        assert "所有 agent" in out
        assert "优先级高于工作流默认" in out

    @staticmethod
    def test_multiline_instruction_preserved():
        text = "line1\nline2\nline3"
        out = _wrap_session_instruction(text)
        assert "line1\nline2\nline3" in out


# ── build_config_content: instructions overlay ──────────────────────────


class TestBuildConfigContentInstructions:
    @staticmethod
    def test_instructions_writes_temp_file_and_refs_path():
        cfg = json.loads(
            build_config_content(
                instructions=["target arch is arch32"],
                session_id="sess-abc",
            )
        )
        assert "instructions" in cfg
        paths = cfg["instructions"]
        assert len(paths) == 1
        # stable per-session path
        assert "sess-abc" in paths[0]
        # file exists and contains the wrapped instruction
        assert os.path.exists(paths[0])
        with open(paths[0], encoding="utf-8") as f:
            content = f.read()
        assert _INSTRUCTION_HEADER in content
        assert "target arch is arch32" in content
        assert _INSTRUCTION_FOOTER in content

    @staticmethod
    def test_multiple_instructions_combined_into_one_file():
        cfg = json.loads(
            build_config_content(
                instructions=["INST-ONE", "INST-TWO"],
                session_id="sess-combo",
            )
        )
        path = cfg["instructions"][0]
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # both instructions land, in registration order
        assert content.index("INST-ONE") < content.index("INST-TWO")

    @staticmethod
    def test_empty_instructions_skipped():
        cfg = json.loads(build_config_content(instructions=[""], session_id="s"))
        assert "instructions" not in cfg

    @staticmethod
    def test_none_instructions_skipped():
        cfg = json.loads(build_config_content(instructions=None, session_id="s"))
        assert "instructions" not in cfg

    @staticmethod
    def test_no_session_id_uses_default_path():
        cfg = json.loads(build_config_content(instructions=["some text"]))
        assert "instructions" in cfg
        assert "default" in cfg["instructions"][0]

    @staticmethod
    def test_instructions_merges_with_gate_and_worker():
        # gate + worker system_prompt + instructions must coexist
        cfg = json.loads(
            build_config_content(
                gate_url="http://127.0.0.1:9/mcp",
                system_prompt="be disciplined",
                instructions=["global rule"],
                session_id="sess-merge",
            )
        )
        assert cfg["mcp"]["workflow_controller_gate"]["url"] == "http://127.0.0.1:9/mcp"
        assert cfg["agent"]["workflow-controller-worker"]["prompt"] == "be disciplined"
        assert "instructions" in cfg
        with open(cfg["instructions"][0], encoding="utf-8") as f:
            assert "global rule" in f.read()

    @staticmethod
    def test_instructions_and_subagent_hints_coexist(tmp_path):
        # instructions (global) + subagent hints (per-type) are complementary
        from .test_subagent_hint import (
            _agents_dir_with_researcher,
        )

        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["per-type hint"]},
                agents_dir=agents_dir,
                instructions=["global instruction"],
                session_id="sess-coexist",
            )
        )
        # per-type hint lands in the agent prompt (prepended)
        assert "per-type hint" in cfg["agent"]["researcher"]["prompt"]
        # global instruction lands in the instructions file (appended to all)
        with open(cfg["instructions"][0], encoding="utf-8") as f:
            assert "global instruction" in f.read()

    @staticmethod
    def test_existing_paths_unchanged_when_no_instructions():
        # the new param defaults to None → build_config_content behaves exactly
        # as before (regression guard)
        cfg = json.loads(
            build_config_content(gate_url="http://127.0.0.1:9/mcp", system_prompt="sp")
        )
        assert cfg["mcp"]["workflow_controller_gate"]["url"] == "http://127.0.0.1:9/mcp"
        assert cfg["agent"]["workflow-controller-worker"]["prompt"] == "sp"
        assert cfg["compaction"]["auto"] is False
        assert "instructions" not in cfg


# ── Orchestrator instruction registry ───────────────────────────────────


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_dir=str(tmp_path),
        harmony_output_path=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )


class TestOrchestratorInstructionRegistry:
    @staticmethod
    def test_register_and_get(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("sess-1", "fix the build")
        assert orch.get_session_instructions("sess-1") == ["fix the build"]

    @staticmethod
    def test_get_unknown_session_returns_empty(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        assert orch.get_session_instructions("nope") == []

    @staticmethod
    def test_get_returns_copy_not_internal(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "x")
        snapshot = orch.get_session_instructions("s")
        snapshot.append("mutated")
        assert orch.get_session_instructions("s") == ["x"]

    @staticmethod
    def test_register_appends(base_config, tmp_path):
        # multiple instructions accumulate, not overwrite
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "v1")
        orch.register_session_instruction("s", "v2")
        assert orch.get_session_instructions("s") == ["v1", "v2"]

    @staticmethod
    def test_clear_all(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "1")
        orch.register_session_instruction("s", "2")
        n = orch.clear_session_instruction("s")
        assert n == 2
        assert orch.get_session_instructions("s") == []

    @staticmethod
    def test_clear_missing_returns_zero(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        assert orch.clear_session_instruction("nope") == 0

    @staticmethod
    def test_clear_empties_session_key(base_config, tmp_path):
        # clearing the last instruction must drop the session key
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "1")
        orch.clear_session_instruction("s")
        assert "s" not in getattr(orch.overlays, "_session_instructions")

    @staticmethod
    def test_clear_one_by_index(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "first")
        orch.register_session_instruction("s", "second")
        n = orch.clear_session_instruction("s", instruction_index=0)
        assert n == 1
        assert orch.get_session_instructions("s") == ["second"]

    @staticmethod
    def test_clear_by_index_out_of_range_returns_zero(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "only")
        assert orch.clear_session_instruction("s", instruction_index=5) == 0
        assert orch.get_session_instructions("s") == ["only"]

    @staticmethod
    def test_clear_by_index_empties_drops_session(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_session_instruction("s", "only")
        orch.clear_session_instruction("s", instruction_index=0)
        assert "s" not in getattr(orch.overlays, "_session_instructions")


class TestMakeDriverHooksWiresInstructionGetter:
    """make_driver_hooks must wire a get_session_instructions callable that
    reads the live registry (so iteration-boundary registrations land next round).
    """

    @staticmethod
    def test_hook_callable_reads_live_registry(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        hooks = orch.make_driver_hooks("sess-1")
        assert hooks.get_overlays is not None
        # empty before register
        assert hooks.get_overlays()["instructions"] == []
        # register after building hooks → callable picks it up (lazy, not snapshot)
        orch.register_session_instruction("sess-1", "live update")
        assert hooks.get_overlays()["instructions"] == ["live update"]
        # clear → callable reflects it immediately
        orch.clear_session_instruction("sess-1")
        assert hooks.get_overlays()["instructions"] == []

    @staticmethod
    def test_hook_callable_is_per_session(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        h1 = orch.make_driver_hooks("s1")
        h2 = orch.make_driver_hooks("s2")
        orch.register_session_instruction("s1", "for s1")
        orch.register_session_instruction("s2", "for s2")
        assert h1.get_overlays()["instructions"] == ["for s1"]
        assert h2.get_overlays()["instructions"] == ["for s2"]


# ── project-level instruction registry ───────────────────────────────────


class TestProjectInstructionRegistry:
    @staticmethod
    def test_register_and_get(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_project_instruction(str(tmp_path), "global rule")
        assert orch.get_project_instructions(str(tmp_path)) == ["global rule"]

    @staticmethod
    def test_get_unknown_project_returns_empty(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        assert orch.get_project_instructions("/nonexistent") == []

    @staticmethod
    def test_register_appends(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_project_instruction(str(tmp_path), "v1")
        orch.register_project_instruction(str(tmp_path), "v2")
        assert orch.get_project_instructions(str(tmp_path)) == ["v1", "v2"]

    @staticmethod
    def test_clear_all(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_project_instruction(str(tmp_path), "1")
        orch.register_project_instruction(str(tmp_path), "2")
        n = orch.clear_project_instruction(str(tmp_path))
        assert n == 2
        assert orch.get_project_instructions(str(tmp_path)) == []

    @staticmethod
    def test_clear_by_index(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_project_instruction(str(tmp_path), "first")
        orch.register_project_instruction(str(tmp_path), "second")
        n = orch.clear_project_instruction(str(tmp_path), instruction_index=0)
        assert n == 1
        assert orch.get_project_instructions(str(tmp_path)) == ["second"]


class TestTransferProjectInstructions:
    @staticmethod
    def test_transfers_into_session(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_project_instruction(str(tmp_path), "pre-session rule")
        # transfer copies into the session bucket (deep-copied)
        orch.overlays.transfer_project_overlays("sess-new", str(tmp_path))
        assert orch.get_session_instructions("sess-new") == ["pre-session rule"]
        # project bucket stays for the next start
        assert orch.get_project_instructions(str(tmp_path)) == ["pre-session rule"]

    @staticmethod
    def test_no_project_instructions_is_noop(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.overlays.transfer_project_overlays("sess-new", str(tmp_path))
        assert orch.get_session_instructions("sess-new") == []


# ── SessionAPI instruction methods + audit event ────────────────────────


def _mock_orch_for_api() -> tuple[MagicMock, MagicMock]:
    """Minimal mock: orchestrator with register/clear/get + bus.emit.

    SessionAPI emits overlay audit events via ``orch.bus.emit`` (bus.emit
    internally stores), so the audit path is asserted against ``bus.emit``
    rather than ``event_router.store`` directly.
    """
    orch = MagicMock()
    orch.register_session_instruction = MagicMock()
    orch.clear_session_instruction = MagicMock(return_value=1)
    orch.get_session_instructions = MagicMock(return_value=["x"])
    bus = MagicMock()
    bus.emit = AsyncMock()
    orch.bus = bus
    return orch, bus


class TestSessionAPIInstructions:
    @staticmethod
    def test_get_delegates_to_orchestrator():
        orch, _ = _mock_orch_for_api()
        out = SessionAPI(orch, None).get_session_instructions("s1")
        orch.get_session_instructions.assert_called_once_with("s1")
        assert out == ["x"]

    async def test_register_stores_and_emits_event(self):
        orch, bus = _mock_orch_for_api()
        await SessionAPI(orch, None).register_session_instruction("s1", "disable dep X")
        orch.register_session_instruction.assert_called_once_with("s1", "disable dep X")
        bus.emit.assert_awaited_once()
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_REGISTERED
        assert ev.data == {
            "scope": "global",
            "content": "disable dep X",
        }

    async def test_clear_returns_count_and_emits_event(self):
        orch, bus = _mock_orch_for_api()
        out = await SessionAPI(orch, None).clear_session_instruction("s1")
        assert out == {"cleared": 1}
        orch.clear_session_instruction.assert_called_once_with("s1", None)
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert ev.data["scope"] == "global"
        assert ev.data["index"] is None
        assert ev.data["cleared"] == 1

    async def test_clear_one_by_index_emits_event(self):
        orch, bus = _mock_orch_for_api()
        orch.clear_session_instruction = MagicMock(return_value=1)
        out = await SessionAPI(orch, None).clear_session_instruction("s1", 0)
        assert out == {"cleared": 1}
        orch.clear_session_instruction.assert_called_once_with("s1", 0)
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert ev.data == {
            "scope": "global",
            "index": 0,
            "cleared": 1,
        }

    async def test_clear_all_passes_none(self):
        orch, bus = _mock_orch_for_api()
        orch.clear_session_instruction = MagicMock(return_value=2)
        out = await SessionAPI(orch, None).clear_session_instruction("s1")
        assert out == {"cleared": 2}
        orch.clear_session_instruction.assert_called_once_with("s1", None)
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert ev.data["scope"] == "global"
        assert ev.data["index"] is None
        assert ev.data["cleared"] == 2


# ── opencode driver: instructions flow into OPENCODE_CONFIG_CONTENT ──────


class TestDriverReadsInstructions:
    """The opencode driver reads the live instruction registry via the hooks
    callable and passes it to build_config_content. Verified by patching
    build_config_content to capture the call (no real opencode subprocess).
    """

    async def test_query_passes_instructions(self, tmp_path, monkeypatch):
        captured = await captured_echo_opencode_query(
            monkeypatch,
            tmp_path,
            overlays={"subagent_hints": {}, "instructions": ["global rule"]},
        )

        assert captured.get("instructions") == ["global rule"]
        assert captured.get("session_id") == "s1"
        assert captured.get("system_prompt") == "SP"

    async def test_query_no_instructions_passes_none(self, tmp_path, monkeypatch):
        # hooks with a getter that returns empty → no overlay, but system_prompt
        # still triggers build_config_content
        captured = await captured_echo_opencode_query(
            monkeypatch, tmp_path, overlays={"subagent_hints": {}, "instructions": []}
        )

        assert captured.get("instructions") is None

    async def test_query_no_hooks_no_config_content(self, tmp_path, monkeypatch):
        # no hooks → no gate, no instructions; and no system_prompt →
        # build_config_content isn't called at all
        assert await bare_echo_opencode_query(monkeypatch, tmp_path) == []
