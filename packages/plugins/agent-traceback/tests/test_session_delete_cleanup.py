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

"""Session 删除清理链路单测。

覆盖：
- 删除非默认 cwd session：metadata 双删防复活，且不留孤儿事件行。
- fork_root 清理（事件 data birth_traceback + in-memory 回退）。
- 路径安全、birth_traceback 回读。
- 插件 uninstall detach bus handler。
- reaper TTL 清理 + 活动集保护。
- 重建 session 对称清理 + recent_dirs 同步。
- delete 响应契约。
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from workflow_controller.config.settings import (
    AppConfig,
    HookServerConfig,
    ReviewerConfig,
    SessionConfig,
)
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.session.events import Event

if TYPE_CHECKING:
    from workflow_controller.plugins.agent_traceback.plugin import AgentTracebackPlugin

_HAS_GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _HAS_GIT, reason="git not installed")


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        project_dir=str(tmp_path),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=False),
    )


def _install_plugin(o: Orchestrator, *, extra: dict | None = None) -> AgentTracebackPlugin:
    """构造并 install 一个 agent_traceback 插件到 registry（含 bus wiring）。"""
    from ._plugin_harness import install_plugin

    return install_plugin(o, extra={"max_checkpoints": 5, **(extra or {})})


@pytest.fixture
async def orch(config, tmp_path, monkeypatch):
    """Orchestrator + 已 install 的 agent_traceback 插件。

    把 tempdir 指到测试沙盒，让 fork_root 路径安全校验（须在 tempdir 内）通过。
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    o = Orchestrator(config, default_project_dir=tmp_path / "test.db")
    await o.start()
    monkeypatch.setattr(o, "_activate_session", AsyncMock())
    plugin = _install_plugin(o)
    yield o
    plugin.uninstall()
    await o.shutdown()


@pytest.fixture
async def orch_no_plugin(config, tmp_path, monkeypatch):
    """Orchestrator 不预装插件——供 reaper 测试按需构造独立插件实例。"""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    o = Orchestrator(config, default_project_dir=tmp_path / "test.db")
    await o.start()
    monkeypatch.setattr(o, "_activate_session", AsyncMock())
    yield o
    await o.shutdown()


def _get_plugin(o: Orchestrator) -> AgentTracebackPlugin:
    return next(p for p in o.plugins if p.config.name == "agent_traceback")  # type: ignore[return-value]


def _age(path: Path, days: float) -> None:
    old_ts = os.path.getmtime(path) - days * 86400
    os.utime(path, (old_ts, old_ts))


# ── 删除不复活 / 无孤儿事件 ───────────────────────────────────────────────────


class TestDeleteCleanup:
    """删除非默认 cwd session：default DB（metadata owner）+ 项目 DB（事件）双删，
    防重启复活；bus.emit 在 delete_session 之前，避免孤儿 session_delete 事件行。
    """

    async def test_delete_no_revival_no_orphan_event(self, orch, tmp_path):
        from workflow_controller.api.session_api import SessionAPI

        project_cwd = tmp_path / "myproject"
        project_cwd.mkdir()
        sess = await orch.pool.create(
            SessionConfig(name="worker", role="worker", cwd=str(project_cwd))
        )
        await SessionAPI(orch, None).delete(sess.id)

        # metadata 行被清 → 重启 load_sessions 不再复活
        assert await orch.event_router.get_session(sess.id) is None
        assert orch.pool.get(sess.id) is None
        orch.pool.load_sessions()
        assert orch.pool.get(sess.id) is None
        # 跨库无残留事件（含 session_delete 控制事件）
        assert orch.event_router.get_events_sync(sess.id) == []


# ── fork_root 清理（事件 data birth_traceback + in-memory 回退）──────────────


class TestOnSessionDeleteUsesEventData:
    """事件 data 携带 birth_traceback 时即时清理 fork_root；缺失时回退 in-memory map。"""

    async def test_cleans_fork_root_from_event_data(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        fork_root = tmp_path / "workflow-controller-tb-reloaded"
        fork_root.mkdir()
        (fork_root / "marker").write_text("x")
        fork = await orch.pool.create(
            SessionConfig(name="fork", role="worker", cwd=str(fork_root / "cwd"))
        )
        getattr(plugin, "_fork_roots")[fork.id] = str(fork_root)

        await self._emit_delete(
            orch,
            fork,
            {
                "session_id": fork.id,
                "birth_traceback": {"fork_root": str(fork_root)},
            },
        )

        assert not fork_root.exists()
        assert fork.id not in getattr(plugin, "_fork_roots")

    async def test_falls_back_to_inmemory_map(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        fork_root = tmp_path / "workflow-controller-tb-legacy"
        fork_root.mkdir()
        (fork_root / "marker").write_text("x")
        fork = await orch.pool.create(
            SessionConfig(name="fork", role="worker", cwd=str(fork_root / "cwd"))
        )
        getattr(plugin, "_fork_roots")[fork.id] = str(fork_root)

        await self._emit_delete(orch, fork, {"session_id": fork.id})

        assert not fork_root.exists()
        assert fork.id not in getattr(plugin, "_fork_roots")

    async def _emit_delete(self, orch, fork, data):
        orch.pool.remove(fork.id)
        await orch.bus.emit(
            Event.create(
                session_id=fork.id,
                iteration=0,
                event_type="session_delete",  # type: ignore[arg-type]
                data=data,
            )
        )
        orch.event_router.delete_session(fork.id)


# ── 路径安全 ─────────────────────────────────────────────────────────────────


class TestPathSafety:
    async def test_is_safe_fork_root(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        # orch fixture 已把 tempdir 指到 tmp_path
        assert getattr(plugin, "_is_safe_fork_root")(str(tmp_path / "workflow-controller-tb-valid"))
        assert not getattr(plugin, "_is_safe_fork_root")(str(tmp_path / "evil-dir"))  # 前缀错误
        outside = tmp_path.parent / "workflow-controller-tb-outside"  # tempdir 外
        outside.mkdir(exist_ok=True)
        try:
            assert not getattr(plugin, "_is_safe_fork_root")(str(outside))
        finally:
            shutil.rmtree(outside, ignore_errors=True)


# ── birth_traceback 回读（session 不在 pool 时）─────────────────────────────


class TestReadSessionBirthTraceback:
    """``event_router.read_session_birth_traceback`` 扫 sessions 表读 birth_traceback，
    供 SessionAPI 在 session 不在 pool 时透传进事件 data。
    """

    async def test_reads_birth_traceback_from_persisted_session(self, orch, tmp_path):
        origin = await orch.pool.create(
            SessionConfig(name="origin", role="worker", cwd=str(tmp_path / "o"))
        )
        fork_cwd = tmp_path / "fkcwd" / "cwd"
        fork_cwd.mkdir(parents=True)
        fork = await orch.pool.create(SessionConfig(name="fk", role="worker", cwd=str(fork_cwd)))
        fork.plugin_metadata = {
            "agent_traceback": {
                "birth_traceback": {
                    "origin_session_id": origin.id,
                    "fork_root": "/tmp/workflow-controller-tb-scan",
                }
            }
        }
        await orch.pool.persist(fork)

        bt = orch.event_router.read_session_birth_traceback(fork.id)
        assert bt is not None
        assert bt["origin_session_id"] == origin.id
        assert bt["fork_root"] == "/tmp/workflow-controller-tb-scan"


# ── 插件 uninstall detach bus handler ────────────────────────────────────────


class TestBusHandlerDetach:
    async def test_uninstall_detaches_interceptors(self, orch):
        plugin = _get_plugin(orch)
        key = getattr(orch.bus, "_type_key")("session_delete")
        assert any(
            h == getattr(plugin, "_on_session_delete")
            for _, h in getattr(orch.bus, "_interceptors").get(key, [])
        )

        plugin.uninstall()

        assert all(
            h != getattr(plugin, "_on_session_delete")
            for _, h in getattr(orch.bus, "_interceptors").get(key, [])
        )


# ── reaper：TTL 清理 + 活动集保护 ────────────────────────────────────────────


class TestReaperCleansByTtl:
    """reaper 按 mtime + TTL 清理 ``workflow-controller-tb-*`` tempdir（启动期一次性）。"""

    async def test_reaps_old_tempdir_on_install(self, orch_no_plugin, tmp_path):
        old = tmp_path / "workflow-controller-tb-old"
        old.mkdir()
        (old / "big.db").write_text("x" * 100)
        _age(old, 10)

        plugin = _install_plugin(orch_no_plugin)
        try:
            assert not old.exists()
        finally:
            plugin.uninstall()

    async def test_keeps_recent_tempdir(self, orch_no_plugin, tmp_path):
        recent = tmp_path / "workflow-controller-tb-fresh"
        recent.mkdir()
        (recent / "inuse.db").write_text("x")

        plugin = _install_plugin(orch_no_plugin)
        try:
            assert recent.exists()
        finally:
            plugin.uninstall()


class TestReaperActiveSetProtection:
    """reaper 跳过仍被近期活动 session 引用的 fork_root；IDLE ghost 超 TTL 才回收。"""

    async def test_skips_fork_root_of_live_session(self, orch_no_plugin, tmp_path):
        fork_root = tmp_path / "workflow-controller-tb-live"
        fork_root.mkdir()
        (fork_root / "running.db").write_text("x")
        _age(fork_root, 10)

        sess = await orch_no_plugin.pool.create(
            SessionConfig(name="live-fork", role="worker", cwd=str(fork_root / "cwd"))
        )
        sess.plugin_metadata = {
            "agent_traceback": {"birth_traceback": {"fork_root": str(fork_root)}}
        }
        await orch_no_plugin.pool.persist(sess)

        plugin = _install_plugin(orch_no_plugin)
        try:
            assert fork_root.exists()  # 近期活动 → 即便 mtime 超 TTL 也不删
        finally:
            plugin.uninstall()

    async def test_reaps_fork_root_of_stale_ghost(self, orch_no_plugin, tmp_path):
        fork_root = tmp_path / "workflow-controller-tb-ghost"
        fork_root.mkdir()
        (fork_root / "ghost.db").write_text("x")
        _age(fork_root, 10)

        sess = await orch_no_plugin.pool.create(
            SessionConfig(name="ghost-fork", role="worker", cwd=str(fork_root / "cwd"))
        )
        sess.plugin_metadata = {
            "agent_traceback": {"birth_traceback": {"fork_root": str(fork_root)}}
        }
        sess.last_activity = datetime.now(tz=UTC) - timedelta(days=30)  # 远超默认 7 天 TTL
        await orch_no_plugin.pool.persist(sess)

        plugin = _install_plugin(orch_no_plugin)
        try:
            assert not fork_root.exists()  # 闲置超 TTL 的 ghost → 回收
        finally:
            plugin.uninstall()


# ── 重建 session 的对称清理 ──────────────────────────────────────────────────


def _reconstruct_birth(
    target_cwd: Path,
    *,
    created_git_dir: bool = True,
    created_opencode_dir: bool = True,
) -> dict:
    return {
        "reconstructed_from": "/tmp/cannbot.db",
        "fork_cwd": str(target_cwd),
        "created_git_dir": created_git_dir,
        "created_opencode_dir": created_opencode_dir,
    }


class TestCleanReconstructArtifacts:
    """``_clean_reconstruct_artifacts``：默认清 Workflow Controller 子集，激进 rmtree 整个目录。"""

    async def test_default_cleans_workflow_controller_subset_only(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        target = tmp_path / "recon"
        for sub in (".workflow-controller", ".opencode", ".git"):
            d = target / sub
            d.mkdir(parents=True)
            (d / "x.db").write_text("x")
        (target / "README.md").write_text("user file")

        removed = getattr(plugin, "_clean_reconstruct_artifacts")(
            "sid",
            str(target),
            _reconstruct_birth(target, created_git_dir=True),
        )

        for sub in (".workflow-controller", ".opencode", ".git"):
            assert str(target / sub) in removed
            assert not (target / sub).exists()
        assert (target / "README.md").exists()  # 用户文件保留

    async def test_aggressive_rmtrees_whole_target_cwd(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        target = tmp_path / "recon-agg"
        target.mkdir()
        (target / ".workflow-controller").mkdir()
        (target / "src.py").write_text("user code")

        removed = getattr(plugin, "_clean_reconstruct_artifacts")(
            "sid",
            str(target),
            _reconstruct_birth(target),
            aggressive=True,
        )

        assert str(target) in removed
        assert not target.exists()

    async def test_aggressive_refuses_root_path(self, orch):
        plugin = _get_plugin(orch)
        removed = getattr(plugin, "_clean_reconstruct_artifacts")(
            "sid",
            str(Path("/")),
            _reconstruct_birth(Path("/"), created_git_dir=True),
            aggressive=True,
        )
        assert removed == []  # 拒绝根目录 / 过浅路径

    async def test_old_session_without_created_git_dir_keeps_git(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        target = tmp_path / "recon-old"
        (target / ".workflow-controller").mkdir(parents=True)
        (target / ".git").mkdir()
        (target / ".git" / "config").write_text("x")
        birth = {"reconstructed_from": "/tmp/old.db", "fork_cwd": str(target)}  # 无 created_git_dir

        removed = getattr(plugin, "_clean_reconstruct_artifacts")("sid", str(target), birth)

        assert str(target / ".workflow-controller") in removed
        assert (target / ".git").exists()  # 旧 session 无标记 → 保守不清

    async def test_preexisting_opencode_dir_is_preserved(self, orch, tmp_path):
        plugin = _get_plugin(orch)
        target = tmp_path / "recon-user-opencode"
        (target / ".workflow-controller").mkdir(parents=True)
        (target / ".opencode").mkdir()
        (target / ".opencode" / "agents.md").write_text("user config")

        removed = getattr(plugin, "_clean_reconstruct_artifacts")(
            "sid",
            str(target),
            _reconstruct_birth(
                target,
                created_opencode_dir=False,
            ),
        )

        assert str(target / ".workflow-controller") in removed
        assert str(target / ".opencode") not in removed
        assert (target / ".opencode" / "agents.md").read_text() == "user config"


class TestOnSessionDeleteCleansReconstruct:
    """``_on_session_delete`` 识别重建 session → 清产物 + 同步 recent_dirs。"""

    async def test_cleans_reconstruct_artifacts_on_delete(self, orch, tmp_path):
        target = tmp_path / "recon-del"
        (target / ".workflow-controller").mkdir(parents=True)
        (target / ".git").mkdir()
        (target / "user.py").write_text("x")
        sess = await orch.pool.create(SessionConfig(name="recon", role="worker", cwd=str(target)))
        birth = _reconstruct_birth(target, created_git_dir=True)
        sess.plugin_metadata = {"agent_traceback": {"birth_traceback": birth}}

        await self._emit_delete(orch, sess, birth, clean_mode="workflow_controller")

        assert not (target / ".workflow-controller").exists()
        assert not (target / ".git").exists()
        assert (target / "user.py").exists()

    async def test_syncs_recent_dirs_on_delete(self, orch, tmp_path, monkeypatch):
        from workflow_controller.api import config_api as config_api_module
        from workflow_controller.api.config_api import ConfigAPI

        # 本用例聚焦「删除同步」语义：禁用临时区判定，否则沙盒 tmp_path
        # （位于真实系统临时区下）会被卫生谓词拒绝登记、断言空洞通过。
        monkeypatch.setattr(config_api_module, "_transient_roots", lambda: ())
        target = tmp_path / "recon-rd"
        target.mkdir()
        (target / ".workflow-controller").mkdir()
        orch.config_api = ConfigAPI(orch, tmp_path / "saved.yaml")  # type: ignore[attr-defined]
        orch.config_api.add_recent_dir(str(target))
        sess = await orch.pool.create(SessionConfig(name="recon", role="worker", cwd=str(target)))
        birth = _reconstruct_birth(target)
        sess.plugin_metadata = {"agent_traceback": {"birth_traceback": birth}}

        await self._emit_delete(orch, sess, birth, clean_mode="workflow_controller")

        assert str(target) not in orch.config_api.get_recent_dirs()

    async def test_syncs_recent_dirs_on_fork_delete(self, orch, tmp_path, monkeypatch):
        """fork session 删除也移除 recent_dirs.json 中其 tempdir cwd，不留幽灵条目。"""
        from workflow_controller.api import config_api as config_api_module
        from workflow_controller.api.config_api import ConfigAPI

        # 同上：禁用临时区判定，保持「误登记 → 删除移除」的原语义（新卫生谓词下
        # 此类 tempdir 本就登记不进去，见 test_recent_dirs 的谓词用例）。
        monkeypatch.setattr(config_api_module, "_transient_roots", lambda: ())
        fork_root = tmp_path / "workflow-controller-tb-forkrd"
        (fork_root / "cwd").mkdir(parents=True)
        fork_cwd = str(fork_root / "cwd")
        orch.cfg_dir = tmp_path  # type: ignore[attr-defined]  # recent_dirs.json 落盘到沙盒
        orch.config_api = ConfigAPI(orch, tmp_path / "saved.yaml")  # type: ignore[attr-defined]
        orch.config_api.add_recent_dir(fork_cwd)  # 模拟 fork tempdir 误登记进 json
        sess = await orch.pool.create(SessionConfig(name="fork", role="worker", cwd=fork_cwd))
        birth = {"fork_root": str(fork_root), "fork_cwd": fork_cwd}
        sess.plugin_metadata = {"agent_traceback": {"birth_traceback": birth}}

        await self._emit_delete(orch, sess, birth, clean_mode=None)

        assert fork_cwd not in orch.config_api.get_recent_dirs()
        assert not fork_root.exists()  # rmtree 仍照常执行

    async def _emit_delete(self, orch, sess, birth, *, clean_mode):
        orch.pool.remove(sess.id)
        await orch.bus.emit(
            Event.create(
                session_id=sess.id,
                iteration=0,
                event_type="session_delete",  # type: ignore[arg-type]
                data={
                    "session_id": sess.id,
                    "birth_traceback": birth,
                    "clean_reconstruct": clean_mode,
                },
            )
        )
        orch.event_router.delete_session(sess.id)


# ── delete 响应契约 ──────────────────────────────────────────────────────────


class TestDeleteResponseContract:
    """``SessionAPI.delete`` 返回 ``{ok, deleted_session_ids}``，并清理 fork_root。"""

    async def test_delete_returns_ok_and_deleted_id(self, orch, tmp_path):
        from workflow_controller.api.session_api import SessionAPI

        sess = await orch.pool.create(SessionConfig(name="plain", role="worker", cwd=str(tmp_path)))
        result = await SessionAPI(orch, None).delete(sess.id)
        assert result["ok"] is True
        assert sess.id in result["deleted_session_ids"]

    async def test_delete_fork_cleans_fork_root(self, orch, tmp_path):
        from workflow_controller.api.session_api import SessionAPI

        fork_root = tmp_path / "workflow-controller-tb-resp"
        fork_root.mkdir()
        (fork_root / "big.db").write_text("x" * 100)
        sess = await orch.pool.create(
            SessionConfig(name="fork", role="worker", cwd=str(fork_root / "cwd"))
        )
        sess.plugin_metadata = {
            "agent_traceback": {"birth_traceback": {"fork_root": str(fork_root)}}
        }

        result = await SessionAPI(orch, None).delete(sess.id)

        assert result["ok"] is True
        assert not fork_root.exists()
