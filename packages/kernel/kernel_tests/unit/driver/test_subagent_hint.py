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

"""subagent system-prompt hint overlay (opencode-native).

Tests three surfaces of the non-invasive subagent-prompt-injection design
(docs/subagent-prompt-injection-design.md §4.1):

- ``workflow_controller.driver.opencode_gate`` — ``build_config_content`` merges hinted
  subagents into the ``agent`` map (hint prepended to the on-disk system
  prompt, original fields preserved); ``_read_opencode_agent_prompt`` parses
  ``.opencode/agents/{type}.md`` (frontmatter + body); ``wrap_subagent_hint``
  (runtime-agnostic, in ``workflow_controller.core._subagent``) wraps the user text.
- ``Orchestrator`` — the per-session ``_subagent_hints`` registry (register /
  clear / get), mirroring SubagentTracker's in-flight dict; wired into ``DriverHooks`` so
  the opencode driver reads live hints at query() time.
- ``SessionAPI`` — register / clear / get, with an auditable
  ``PROMPT_OVERLAY_REGISTERED`` / ``PROMPT_OVERLAY_CLEARED`` event per mutation
  (emitted via ``bus.emit``; the legacy ``SUBAGENT_HINT`` event is removed).

The overlay lives only in ``OPENCODE_CONFIG_CONTENT`` (env var) + an in-memory
registry; the disk ``.md`` is never touched (reversible, session-scoped).

A subagent_type's hints are a **list of strings** (one or more per type) —
multiple hints accumulate rather than overwriting, and are combined into one
block when baked into the system prompt. Clear supports an optional 0-based
``hint_index`` to drop a single hint.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from workflow_controller.api.session_api import SessionAPI
from workflow_controller.config.settings import AppConfig, HookServerConfig, ReviewerConfig
from workflow_controller.core._subagent import _HINT_FOOTER, _HINT_HEADER, wrap_subagent_hint
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.driver.opencode_gate import (
    _read_opencode_agent_prompt,
    build_config_content,
)
from workflow_controller.session.events import EventType

from ._hook_handler_stubs import (
    bare_echo_opencode_query,
    captured_echo_opencode_query,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── wrap_subagent_hint ───────────────────────────────────────────────────


class TestWrapHint:
    @staticmethod
    def test_wraps_in_fixed_block():
        out = wrap_subagent_hint("fix the tiling key")
        assert out.startswith(_HINT_HEADER)
        assert out.endswith(_HINT_FOOTER)
        assert "fix the tiling key" in out

    @staticmethod
    def test_header_states_priority_over_default():
        # the wrapper must tell the subagent this is a session-scoped user
        # supplement whose priority exceeds the workflow default
        out = wrap_subagent_hint("x")
        assert "优先级高于工作流默认" in out
        assert "本次会话" in out

    @staticmethod
    def test_multiline_hint_preserved():
        hint = "line1\nline2\nline3"
        out = wrap_subagent_hint(hint)
        assert "line1\nline2\nline3" in out


# ── _read_opencode_agent_prompt ──────────────────────────────────────────


def _write_agent_md(
    dir_: Path, name: str, body: str, extra_fm: dict[str, Any] | None = None
) -> Path:
    """Write a minimal opencode agent .md (frontmatter + body)."""
    fm: dict[str, Any] = {"name": name}
    if extra_fm:
        fm.update(extra_fm)
    front = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n"
    p = dir_ / f"{name}.md"
    p.write_text(front + body, encoding="utf-8")
    return p


class TestReadOpencodeAgentPrompt:
    @staticmethod
    def test_parses_frontmatter_and_body(tmp_path):
        _write_agent_md(
            tmp_path,
            "researcher",
            "# researcher\nyou research.",
            extra_fm={"description": "调研", "mode": "subagent"},
        )
        parsed = _read_opencode_agent_prompt(tmp_path / "researcher.md")
        assert parsed is not None
        fields, body = parsed
        # name is the agent key, not a value field → stripped
        assert "name" not in fields
        assert fields["description"] == "调研"
        assert fields["mode"] == "subagent"
        assert body == "# researcher\nyou research."

    @staticmethod
    def test_missing_file_returns_none(tmp_path):
        assert _read_opencode_agent_prompt(tmp_path / "nope.md") is None

    @staticmethod
    def test_no_frontmatter_treats_as_body_only(tmp_path):
        p = tmp_path / "bare.md"
        p.write_text("just a prompt body, no frontmatter", encoding="utf-8")
        parsed = _read_opencode_agent_prompt(p)
        assert parsed is not None
        fields, body = parsed
        assert fields == {}
        assert body == "just a prompt body, no frontmatter"

    @staticmethod
    def test_malformed_frontmatter_returns_none(tmp_path):
        p = tmp_path / "bad.md"
        # opening --- but no closing delimiter
        p.write_text("---\nname: x\nthis has no close", encoding="utf-8")
        assert _read_opencode_agent_prompt(p) is None

    @staticmethod
    def test_invalid_yaml_returns_none(tmp_path):
        p = tmp_path / "badyaml.md"
        p.write_text("---\n: : :\n---\nbody", encoding="utf-8")
        assert _read_opencode_agent_prompt(p) is None

    @staticmethod
    def test_body_with_internal_hr_kept_intact(tmp_path):
        # body 内部的 "---" 不能误判为 frontmatter 结束分隔
        # （按前两个分隔切分，其余保留在 body）
        p = tmp_path / "fancy.md"
        p.write_text("---\nname: x\n---\n# prompt\n\n---\n\nmore", encoding="utf-8")
        parsed = _read_opencode_agent_prompt(p)
        assert parsed is not None
        _, body = parsed
        assert body == "# prompt\n\n---\n\nmore"


# ── build_config_content: subagent hint overlay ───────────────────────────


def _agents_dir_with_researcher(tmp_path: Path) -> Path:
    """Seed a .opencode/agents dir with a researcher.md (mirrors the example)."""
    agents = tmp_path / ".opencode" / "agents"
    agents.mkdir(parents=True)
    _write_agent_md(
        agents,
        "researcher",
        "# researcher subagent\n你是调研 subagent。",
        extra_fm={"description": "调研", "mode": "subagent"},
    )
    return agents


class TestBuildConfigContentSubagentHint:
    @staticmethod
    def test_hint_prepended_to_subagent_prompt(tmp_path):
        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["ADD_CUSTOM tiling key missing"]},
                agents_dir=agents_dir,
            )
        )
        agent = cfg["agent"]["researcher"]
        # hint wrapper comes first, then the original body
        assert agent["prompt"].startswith(_HINT_HEADER)
        assert "ADD_CUSTOM tiling key missing" in agent["prompt"]
        # original system prompt preserved (appended after the hint block)
        assert "你是调研 subagent。" in agent["prompt"]
        # original frontmatter fields preserved
        assert agent["description"] == "调研"
        assert agent["mode"] == "subagent"
        # name NOT leaked into the value (it's the agent key)
        assert "name" not in agent

    @staticmethod
    def test_hint_order_hint_before_original(tmp_path):
        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["HINT_MARKER"]},
                agents_dir=agents_dir,
            )
        )
        prompt = cfg["agent"]["researcher"]["prompt"]
        # HINT_MARKER must appear before the original body text
        assert prompt.index("HINT_MARKER") < prompt.index("你是调研 subagent")
        # and the footer (closing the hint block) appears between them
        assert prompt.index(_HINT_FOOTER) < prompt.index("你是调研 subagent")

    @staticmethod
    def test_no_disk_def_skips_agent(tmp_path):
        # agent_type with no on-disk .md → not fabricated into the agent map
        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"nonexistent": ["hint for nobody"]},
                agents_dir=agents_dir,
            )
        )
        assert "nonexistent" not in cfg.get("agent", {})
        # only the real researcher lands
        assert "researcher" not in cfg.get("agent", {})  # researcher had no hint here

    @staticmethod
    def test_no_agents_dir_skips_overlay():
        # subagent_hints given but agents_dir None → overlay is a no-op
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["x"]},
                agents_dir=None,
            )
        )
        assert "agent" not in cfg

    @staticmethod
    def test_empty_hint_skipped(tmp_path):
        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": [""]},
                agents_dir=agents_dir,
            )
        )
        # empty hint → researcher not overlaid (no agent section at all)
        assert "agent" not in cfg

    @staticmethod
    def test_hint_merges_with_gate_and_worker(tmp_path):
        # gate + restricted reviewer + worker system_prompt + subagent hint
        # must all coexist in one config without overwriting each other
        agents_dir = _agents_dir_with_researcher(tmp_path)
        restricted = {
            "agent": {"workflow-controller-restricted": {"permission": {"write": "deny"}}}
        }
        cfg = json.loads(
            build_config_content(
                gate_url="http://127.0.0.1:9/mcp",
                restricted_agent=restricted,
                system_prompt="be disciplined",
                subagent_hints={"researcher": ["extra care"]},
                agents_dir=agents_dir,
            )
        )
        assert cfg["mcp"]["workflow_controller_gate"]["url"] == "http://127.0.0.1:9/mcp"
        assert cfg["agent"]["workflow-controller-worker"]["prompt"] == "be disciplined"
        assert cfg["agent"]["workflow-controller-restricted"]["permission"]["write"] == "deny"
        assert "extra care" in cfg["agent"]["researcher"]["prompt"]

    @staticmethod
    def test_multiple_hints_each_overlaid(tmp_path):
        agents = tmp_path / ".opencode" / "agents"
        agents.mkdir(parents=True)
        _write_agent_md(agents, "researcher", "# researcher\nr-body")
        _write_agent_md(agents, "tester", "# tester\nt-body")
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["R-HINT"], "tester": ["T-HINT"]},
                agents_dir=agents,
            )
        )
        assert "R-HINT" in cfg["agent"]["researcher"]["prompt"]
        assert "r-body" in cfg["agent"]["researcher"]["prompt"]
        assert "T-HINT" in cfg["agent"]["tester"]["prompt"]
        assert "t-body" in cfg["agent"]["tester"]["prompt"]

    @staticmethod
    def test_multiple_hints_same_type_combined(tmp_path):
        # multiple hints for the SAME subagent_type accumulate into one list
        # and are combined (blank-line-joined) into a single hint block.
        agents_dir = _agents_dir_with_researcher(tmp_path)
        cfg = json.loads(
            build_config_content(
                subagent_hints={"researcher": ["HINT-ONE", "HINT-TWO"]},
                agents_dir=agents_dir,
            )
        )
        prompt = cfg["agent"]["researcher"]["prompt"]
        # both hints land, in registration order, before the original body
        assert prompt.index("HINT-ONE") < prompt.index("HINT-TWO")
        assert prompt.index("HINT-TWO") < prompt.index("你是调研 subagent")
        # joined into one block inside the wrapper
        assert "HINT-ONE\n\nHINT-TWO" in prompt
        # original body still preserved after the block
        assert "你是调研 subagent。" in prompt

    @staticmethod
    def test_existing_paths_unchanged_when_no_hints():
        # the new params default to None → build_config_content behaves exactly
        # as before (regression guard for the gate-only / system_prompt-only paths)
        cfg = json.loads(
            build_config_content(gate_url="http://127.0.0.1:9/mcp", system_prompt="sp")
        )
        assert cfg["mcp"]["workflow_controller_gate"]["url"] == "http://127.0.0.1:9/mcp"
        assert cfg["agent"]["workflow-controller-worker"]["prompt"] == "sp"
        assert cfg["compaction"]["auto"] is False


# ── Orchestrator hint registry ────────────────────────────────────────────


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_dir=str(tmp_path),
        harmony_output_path=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )


class TestOrchestratorHintRegistry:
    @staticmethod
    def test_register_and_get(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("sess-1", "researcher", "fix X")
        assert orch.get_subagent_hints("sess-1") == {"researcher": ["fix X"]}

    @staticmethod
    def test_get_unknown_session_returns_empty(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        assert orch.get_subagent_hints("nope") == {}

    @staticmethod
    def test_get_returns_copy_not_internal(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "a", "x")
        snapshot = orch.get_subagent_hints("s")
        # mutate both the outer dict and the inner list — neither may leak
        snapshot["a"].append("mutated")
        snapshot["b"] = ["sneaked"]
        assert orch.get_subagent_hints("s") == {"a": ["x"]}

    @staticmethod
    def test_register_appends_same_type(base_config, tmp_path):
        # multiple hints for one subagent_type accumulate, not overwrite
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "v1")
        orch.register_subagent_hint("s", "researcher", "v2")
        assert orch.get_subagent_hints("s") == {"researcher": ["v1", "v2"]}

    @staticmethod
    def test_multiple_types_per_session(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "r")
        orch.register_subagent_hint("s", "tester", "t")
        assert orch.get_subagent_hints("s") == {"researcher": ["r"], "tester": ["t"]}

    @staticmethod
    def test_clear_one_type(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "r")
        orch.register_subagent_hint("s", "tester", "t")
        n = orch.clear_subagent_hint("s", "researcher")
        assert n == 1
        assert orch.get_subagent_hints("s") == {"tester": ["t"]}

    @staticmethod
    def test_clear_all(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "a", "1")
        orch.register_subagent_hint("s", "b", "2")
        orch.register_subagent_hint("s", "b", "3")  # two hints on one type
        n = orch.clear_subagent_hint("s", None)
        # cleared count sums individual hint messages across all types
        assert n == 3
        assert orch.get_subagent_hints("s") == {}

    @staticmethod
    def test_clear_missing_returns_zero(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        assert orch.clear_subagent_hint("s", "nope") == 0
        assert orch.clear_subagent_hint("s", None) == 0
        # index form on a missing type also returns 0
        assert orch.clear_subagent_hint("s", "nope", hint_index=0) == 0

    @staticmethod
    def test_clear_empties_session_key(base_config, tmp_path):
        # clearing the last hint must drop the session key so the registry
        # doesn't accumulate empty dicts
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "a", "1")
        orch.clear_subagent_hint("s", "a")
        assert "s" not in getattr(orch.overlays, "_session_hints")

    @staticmethod
    def test_clear_one_hint_by_index(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "first")
        orch.register_subagent_hint("s", "researcher", "second")
        n = orch.clear_subagent_hint("s", "researcher", hint_index=0)
        assert n == 1
        assert orch.get_subagent_hints("s") == {"researcher": ["second"]}

    @staticmethod
    def test_clear_by_index_out_of_range_returns_zero(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "only")
        assert orch.clear_subagent_hint("s", "researcher", hint_index=5) == 0
        # untouched
        assert orch.get_subagent_hints("s") == {"researcher": ["only"]}

    @staticmethod
    def test_clear_by_index_empties_bucket_drops_session(base_config, tmp_path):
        # dropping the last hint of a type via index must also drop the empty
        # bucket and the session key
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        orch.register_subagent_hint("s", "researcher", "only")
        orch.clear_subagent_hint("s", "researcher", hint_index=0)
        assert "s" not in getattr(orch.overlays, "_session_hints")


class TestMakeDriverHooksWiresHintGetter:
    """make_driver_hooks must wire a get_subagent_hints callable that reads the
    live registry (so iteration-boundary registrations land next round).
    """

    @staticmethod
    def test_hook_callable_reads_live_registry(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        hooks = orch.make_driver_hooks("sess-1")
        assert hooks.get_overlays is not None
        # empty before register
        assert hooks.get_overlays()["subagent_hints"] == {}
        # register after building hooks → callable picks it up (lazy, not snapshot)
        orch.register_subagent_hint("sess-1", "researcher", "live update")
        assert hooks.get_overlays()["subagent_hints"] == {"researcher": ["live update"]}
        # clear → callable reflects it immediately
        orch.clear_subagent_hint("sess-1", "researcher")
        assert hooks.get_overlays()["subagent_hints"] == {}

    @staticmethod
    def test_hook_callable_is_per_session(base_config, tmp_path):
        orch = Orchestrator(base_config, default_project_dir=tmp_path / "db")
        h1 = orch.make_driver_hooks("s1")
        h2 = orch.make_driver_hooks("s2")
        orch.register_subagent_hint("s1", "researcher", "for s1")
        orch.register_subagent_hint("s2", "tester", "for s2")
        assert h1.get_overlays()["subagent_hints"] == {"researcher": ["for s1"]}
        assert h2.get_overlays()["subagent_hints"] == {"tester": ["for s2"]}


# ── SessionAPI hint methods + audit event ────────────────────────────────


def _mock_orch_for_api() -> tuple[MagicMock, MagicMock]:
    """Minimal mock: orchestrator with register/clear/get + bus.emit.

    SessionAPI emits overlay audit events via ``orch.bus.emit`` (bus.emit
    internally stores), so the audit path is asserted against ``bus.emit``
    rather than ``event_router.store`` directly.
    """
    orch = MagicMock()
    orch.register_subagent_hint = MagicMock()
    orch.clear_subagent_hint = MagicMock(return_value=1)
    orch.get_subagent_hints = MagicMock(return_value={"researcher": ["x"]})
    bus = MagicMock()
    bus.emit = AsyncMock()
    orch.bus = bus
    return orch, bus


class TestSessionAPIHints:
    @staticmethod
    def test_get_delegates_to_orchestrator():
        orch, _ = _mock_orch_for_api()
        out = SessionAPI(orch, None).get_subagent_hints("s1")
        orch.get_subagent_hints.assert_called_once_with("s1")
        assert out == {"researcher": ["x"]}

    async def test_register_stores_and_emits_event(self):
        orch, bus = _mock_orch_for_api()
        await SessionAPI(orch, None).register_subagent_hint("s1", "researcher", "fix tiling")
        orch.register_subagent_hint.assert_called_once_with("s1", "researcher", "fix tiling")
        bus.emit.assert_awaited_once()
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_REGISTERED
        assert ev.data == {
            "scope": "per_subagent_type",
            "target": "researcher",
            "content": "fix tiling",
        }

    async def test_clear_returns_count_and_emits_event(self):
        orch, bus = _mock_orch_for_api()
        out = await SessionAPI(orch, None).clear_subagent_hint("s1", "researcher")
        assert out == {"cleared": 1}
        # SessionAPI forwards hint_index (defaults to None) to the orchestrator
        orch.clear_subagent_hint.assert_called_once_with("s1", "researcher", None)
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert ev.data["scope"] == "per_subagent_type"
        assert ev.data["target"] == "researcher"
        assert ev.data["index"] is None
        assert ev.data["cleared"] == 1

    async def test_clear_one_by_index_emits_event(self):
        orch, bus = _mock_orch_for_api()
        orch.clear_subagent_hint = MagicMock(return_value=1)
        out = await SessionAPI(orch, None).clear_subagent_hint("s1", "researcher", 0)
        assert out == {"cleared": 1}
        orch.clear_subagent_hint.assert_called_once_with("s1", "researcher", 0)
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert ev.data == {
            "scope": "per_subagent_type",
            "target": "researcher",
            "index": 0,
            "cleared": 1,
        }

    async def test_clear_all_passes_none(self):
        orch, bus = _mock_orch_for_api()
        orch.clear_subagent_hint = MagicMock(return_value=2)
        out = await SessionAPI(orch, None).clear_subagent_hint("s1", None)
        assert out == {"cleared": 2}
        orch.clear_subagent_hint.assert_called_once_with("s1", None, None)
        # Clear-all has no single subagent_type → target omitted from data
        ev = bus.emit.await_args.args[0]
        assert ev.event_type == EventType.PROMPT_OVERLAY_CLEARED
        assert "target" not in ev.data
        assert ev.data["cleared"] == 2


# ── opencode driver: hints flow into OPENCODE_CONFIG_CONTENT ─────────────


class TestDriverReadsHints:
    """The opencode driver reads the live hint registry via the hooks callable
    and passes it (+ agents_dir) to build_config_content. Verified by patching
    build_config_content to capture the call (no real opencode subprocess).
    """

    async def test_query_passes_hints_and_agents_dir(self, tmp_path, monkeypatch):
        # seed a .opencode/agents/researcher.md so the overlay has a disk def
        agents_dir = tmp_path / ".opencode" / "agents"
        agents_dir.mkdir(parents=True)
        _write_agent_md(agents_dir, "researcher", "# researcher\nbody")

        captured = await captured_echo_opencode_query(
            monkeypatch,
            tmp_path,
            overlays={
                "subagent_hints": {"researcher": ["live hint"]},
                "instructions": [],
            },
        )

        assert captured.get("subagent_hints") == {"researcher": ["live hint"]}
        assert captured.get("system_prompt") == "SP"
        assert captured["agents_dir"] == tmp_path / ".opencode" / "agents"

    async def test_query_no_hints_passes_none(self, tmp_path, monkeypatch):
        # hooks with a getter that returns empty → no overlay, but system_prompt
        # still triggers build_config_content
        captured = await captured_echo_opencode_query(
            monkeypatch, tmp_path, overlays={"subagent_hints": {}, "instructions": []}
        )

        assert captured.get("subagent_hints") is None

    async def test_query_no_hooks_no_config_content(self, tmp_path, monkeypatch):
        # no hooks → no gate, no hints; and no system_prompt → build_config_-
        # content isn't called at all (the gate/restricted/system_prompt/hints
        # condition is all False)
        assert await bare_echo_opencode_query(monkeypatch, tmp_path) == []
