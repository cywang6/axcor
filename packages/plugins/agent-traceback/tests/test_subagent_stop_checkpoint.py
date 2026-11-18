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

"""集成测试：orchestrator._subagents.on_start → 插件 on_subagent_start → 文件态快照 +
checkpoints 表（设计 agent-traceback-checkpoint §3/§5）。

不启真实 driver / reviewer，只验证 _subagents.on_start 末尾经插件钩子打快照 + checkpoint
存储链路：project_dir 下有文件 → 调 _subagents.on_start → 快照仓有 commit、EventStore 有
checkpoint 行（status=ok, git_ref, parent 链）。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest

from workflow_controller.config.settings import (
    AppConfig,
    HookServerConfig,
    ReviewerConfig,
    SessionConfig,
)
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.plugins.agent_traceback.snapshot_engine import _run_sync

if TYPE_CHECKING:
    from pathlib import Path

    from workflow_controller.plugins.agent_traceback.plugin import AgentTracebackPlugin


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not installed")


def _install_plugin(
    orch: Orchestrator,
    *,
    enabled: bool = True,
    timeout: float = 10.0,
) -> AgentTracebackPlugin:
    """构造并 install agent_traceback 插件（services 经生产装配路径接线）。

    install 把 SUBAGENT / session_delete / session_complete handler 接上 bus——
    快照经 SUBAGENT interceptor 在 _subagents.on_start 的 emit 内触发。
    """
    from ._plugin_harness import install_plugin

    return install_plugin(
        orch,
        enabled=enabled,
        extra={"snapshot_timeout_seconds": timeout},
    )


def _list_cps(orch: Orchestrator, session_id: str):
    """List checkpoints via the plugin's own CheckpointStore (core no longer
    carries checkpoint CRUD).
    """
    plugin = next(p for p in orch.plugins if p.config.name == "agent_traceback")
    return getattr(plugin, "_cp_store_for_session")(session_id).list_sync(session_id)


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        project_dir=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )


@pytest.fixture
async def orchestrator(config, tmp_path):
    orch = Orchestrator(config, default_project_dir=tmp_path / "test.db")
    await orch.start()
    _install_plugin(orch, enabled=True, timeout=10.0)
    yield orch
    await orch.shutdown()


async def _make_session(orch: Orchestrator, tmp_path: Path, iteration: int = 3):
    """Create a worker session in the pool with cwd=tmp_path."""
    cfg = SessionConfig(name="worker", role="worker", cwd=str(tmp_path))
    session = await orch.pool.create(cfg)
    session.current_iteration = iteration
    return session


async def _start_and_flush(
    orch: Orchestrator,
    session_id: str,
    agent_id: str,
    agent_type: str,
    *,
    parent: str | None = None,
    rounds: int = 10,
) -> None:
    """触发 _subagents.on_start 并等待桥接处理器冲刷 SUBAGENT 事件。"""
    payload: dict[str, str] = {"agent_id": agent_id, "agent_type": agent_type}
    if parent is not None:
        payload["parent_agent_id"] = parent
    await getattr(orch, "_subagents").on_start(session_id, payload)
    for _ in range(rounds):
        await asyncio.sleep(0.01)


class TestSubagentStartCheckpoint:
    async def test_start_creates_checkpoint_and_commit(
        self,
        orchestrator,
        tmp_path,
    ):
        session = await _make_session(orchestrator, tmp_path)
        # Seed a file in the project dir (cwd) so the snapshot has content.
        (tmp_path / "spec.md").write_text("# spec v1\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "k.c").write_text("int main(){}\n")

        await getattr(orchestrator, "_subagents").on_start(
            session.id,
            {
                "agent_id": "ses_sub_aaa",
                "agent_type": "developer",
            },
        )
        # Let the bridge processor flush the SUBAGENT event (best-effort).
        for _ in range(20):
            await asyncio.sleep(0.01)

        cps = _list_cps(orchestrator, session.id)
        assert len(cps) == 1
        cp = cps[0]
        assert cp.status == "ok"
        assert cp.git_ref and len(cp.git_ref) == 40
        assert cp.agent_id == "ses_sub_aaa"
        assert cp.agent_type == "developer"
        assert cp.iteration == 3
        assert cp.trigger == "subagent_start"
        assert cp.file_count >= 2  # spec.md + src/k.c
        assert cp.byte_size > 0
        assert cp.parent_checkpoint_id is None
        # event_rowid is None at start time (no stop event exists yet).
        assert cp.event_rowid is None

        # Snapshot repo has a commit with the trailer.
        plugin = next(p for p in orchestrator.plugins if p.config.name == "agent_traceback")
        eng = getattr(plugin, "_engines")[str(tmp_path)]
        log = _run_sync(["git", "log", "--format=%B", "-1"], cwd=eng.snapshot_dir)
        assert "workflow-controller snapshot" in log.stdout
        assert f"session: {session.id}" in log.stdout
        assert "iteration: 3" in log.stdout
        assert "agent_id: ses_sub_aaa" in log.stdout

    async def test_chained_checkpoints_link_parent(
        self,
        orchestrator,
        tmp_path,
    ):
        session = await _make_session(orchestrator, tmp_path, iteration=1)
        (tmp_path / "f.txt").write_text("v1")

        await _start_and_flush(orchestrator, session.id, "ses_a", "architect")
        # Mutate so the second snapshot is a distinct commit.
        (tmp_path / "f.txt").write_text("v2-different-content")
        await _start_and_flush(orchestrator, session.id, "ses_b", "developer")

        cps = _list_cps(orchestrator, session.id)
        assert len(cps) == 2
        assert cps[0].parent_checkpoint_id is None
        assert cps[1].parent_checkpoint_id == cps[0].checkpoint_id
        assert cps[1].git_ref != cps[0].git_ref

    async def test_nested_subagent_start_skips_checkpoint(
        self,
        orchestrator,
        tmp_path,
    ):
        """孙 agent（parent_agent_id 非空）start 不打检查点——检查点语义 =
        主 agent 派发边界（重放截断点在主 session 的 task part），孙的派发
        part 在子会话里，主 session 无锚。直接子代不受影响。
        """
        session = await _make_session(orchestrator, tmp_path, iteration=2)
        (tmp_path / "f.txt").write_text("v1")

        # 孙 agent start（opencode 轮询器携带 parent_agent_id）
        await _start_and_flush(
            orchestrator,
            session.id,
            "ses_grand",
            "explore",
            parent="ses_child",
            rounds=20,
        )
        assert _list_cps(orchestrator, session.id) == []

        # 直接子代 start（无 parent / 空串）照常打
        await _start_and_flush(
            orchestrator,
            session.id,
            "ses_child",
            "developer",
            parent="",
            rounds=20,
        )
        cps = _list_cps(orchestrator, session.id)
        assert len(cps) == 1
        assert cps[0].agent_id == "ses_child"

    async def test_disabled_traceback_skips_snapshot(
        self,
        tmp_path,
    ):
        cfg = AppConfig(
            project_dir=str(tmp_path),
            hook_server=HookServerConfig(enabled=False),
            reviewer=ReviewerConfig(enabled=False),
        )
        orch = Orchestrator(cfg, default_project_dir=tmp_path / "test.db")
        await orch.start()
        plugin = _install_plugin(orch, enabled=False)
        try:
            session = await _make_session(orch, tmp_path)
            (tmp_path / "f.txt").write_text("x")
            await getattr(orch, "_subagents").on_start(
                session.id,
                {
                    "agent_id": "ses_a",
                    "agent_type": "t",
                },
            )
            for _ in range(10):
                await asyncio.sleep(0.01)
            # No checkpoint stored when traceback disabled.
            cps = _list_cps(orch, session.id)
            assert cps == []
            # traceback 关闭（配置 enabled 为 False）时插件跳过快照
            assert getattr(plugin, "_is_enabled")() is False
        finally:
            await orch.shutdown()

    async def test_excludes_workflow_controller_dir_from_snapshot(
        self,
        orchestrator,
        tmp_path,
    ):
        session = await _make_session(orchestrator, tmp_path)
        (tmp_path / "keep.txt").write_text("k")
        # .workflow-controller/ is excluded from the mirror (recursion guard). Use a non-db
        # filename so we don't clobber the real events.db the router initializes.
        (tmp_path / ".workflow-controller").mkdir()
        (tmp_path / ".workflow-controller" / "junk.txt").write_text("ignored")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("git")

        await _start_and_flush(orchestrator, session.id, "ses_a", "t")
        cps = _list_cps(orchestrator, session.id)
        assert len(cps) == 1
        assert cps[0].file_count == 1  # only keep.txt
        # The mirror working dir must NOT contain .workflow-controller/junk.txt or .git/config.
        plugin = next(p for p in orchestrator.plugins if p.config.name == "agent_traceback")
        eng = getattr(plugin, "_engines")[str(tmp_path)]
        assert not (eng.working_dir / ".workflow-controller" / "junk.txt").exists()
        assert not (eng.working_dir / ".git" / "config").exists()

    async def test_snapshot_failure_does_not_break_start(
        self,
        orchestrator,
        tmp_path,
        monkeypatch,
    ):
        """If snapshot() raises, _subagents.on_start must still complete (best-effort)
        and the wake-retry path must not be blocked.
        """
        session = await _make_session(orchestrator, tmp_path)
        (tmp_path / "f.txt").write_text("x")

        plugin = next(p for p in orchestrator.plugins if p.config.name == "agent_traceback")

        async def _boom(self, **kw):
            raise RuntimeError("boom")

        # Patch the lazily-created engine's snapshot to raise.
        original_resolve = getattr(plugin, "_resolve_engine")

        def _patched_resolve(cwd):
            eng = original_resolve(cwd)
            eng.snapshot = _boom.__get__(eng)  # type: ignore[method-assign]
            return eng

        monkeypatch.setattr(plugin, "_resolve_engine", _patched_resolve)

        # Must not raise.
        await _start_and_flush(orchestrator, session.id, "ses_a", "t")
        # No checkpoint stored (snapshot failed before store_checkpoint).
        cps = _list_cps(orchestrator, session.id)
        assert cps == []
