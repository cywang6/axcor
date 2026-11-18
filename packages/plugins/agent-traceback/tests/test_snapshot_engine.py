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

"""SnapshotEngine 单测（设计 agent-traceback-checkpoint-design §3）。

Hermetic：每个测试用 tmp_path 造一个独立 project_dir + 独立快照仓，git 操作走真实
git（系统已装）。不碰真实 opencode.db / events.db。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest

from workflow_controller.plugins.agent_traceback.snapshot_engine import (
    DEFAULT_IGNORE_PATTERNS,
    SnapshotEngine,
    _match_ignore,
    _python_sync_mirror,
    _rsync_mirror,
)

if TYPE_CHECKING:
    from pathlib import Path


def _has_git() -> bool:
    return shutil.which("git") is not None


def _has_rsync() -> bool:
    return shutil.which("rsync") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not installed")


# ── ignore pattern matching ─────────────────────────────────────────────


class TestMatchIgnore:
    @staticmethod
    def test_top_level_dir():
        assert _match_ignore(".workflow-controller/events.db", DEFAULT_IGNORE_PATTERNS)
        assert _match_ignore(".git/config", DEFAULT_IGNORE_PATTERNS)

    @staticmethod
    def test_nested_dir():
        assert _match_ignore("src/node_modules/pkg/index.js", DEFAULT_IGNORE_PATTERNS)
        assert _match_ignore("a/b/__pycache__/x.pyc", DEFAULT_IGNORE_PATTERNS)

    @staticmethod
    def test_pyc_glob():
        assert _match_ignore("src/mod/file.pyc", DEFAULT_IGNORE_PATTERNS)

    @staticmethod
    def test_not_ignored():
        assert not _match_ignore("src/main.py", DEFAULT_IGNORE_PATTERNS)
        assert not _match_ignore("docs/readme.md", DEFAULT_IGNORE_PATTERNS)


# ── Python tree-sync mirror ─────────────────────────────────────────────


class TestPythonSyncMirror:
    @staticmethod
    def _setup(tmp_path: Path) -> tuple[Path, Path]:
        src = tmp_path / "project"
        dest = tmp_path / "snapshots" / "working"
        src.mkdir()
        dest.mkdir(parents=True)
        return src, dest

    def test_copies_files(self, tmp_path: Path):
        src, dest = self._setup(tmp_path)
        (src / "a.txt").write_text("hello")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_text("world")
        fc, bs = _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        assert fc == 2
        assert bs == len("hello") + len("world")
        assert (dest / "a.txt").read_text() == "hello"
        assert (dest / "sub" / "b.txt").read_text() == "world"

    def test_skips_ignored(self, tmp_path: Path):
        src, dest = self._setup(tmp_path)
        (src / "a.txt").write_text("keep")
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("ignore")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_text("ignore")
        fc, _ = _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        assert fc == 1
        assert (dest / "a.txt").read_text() == "keep"
        assert not (dest / ".git").exists()

    def test_deletes_removed_files(self, tmp_path: Path):
        src, dest = self._setup(tmp_path)
        (src / "a.txt").write_text("keep")
        _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        # remove a.txt from src, add b.txt
        (src / "a.txt").unlink()
        (src / "b.txt").write_text("new")
        fc, _ = _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        assert fc == 1
        assert not (dest / "a.txt").exists()
        assert (dest / "b.txt").read_text() == "new"

    def test_incremental_skips_unchanged(self, tmp_path: Path):
        src, dest = self._setup(tmp_path)
        (src / "a.txt").write_text("same")
        _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        # capture mtime; second sync should skip copy (same size+mtime)
        st_before = (dest / "a.txt").stat()
        _python_sync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        st_after = (dest / "a.txt").stat()
        assert int(st_before.st_mtime) == int(st_after.st_mtime)

    def test_delete_respects_ignore_patterns(self, tmp_path: Path):
        # --exclude 项不受 --delete 影响（rsync 语义），保留目录不被镜像趟误删
        src, dest = self._setup(tmp_path)
        (src / "a.txt").write_text("k")
        reserved = dest / ".workflow-controller-inner-git"
        reserved.mkdir()
        (reserved / "HEAD").write_text("ref: refs/heads/main\n")
        fc, _ = _python_sync_mirror(
            src,
            dest,
            [*DEFAULT_IGNORE_PATTERNS, ".workflow-controller-inner-git"],
        )
        assert fc == 1
        assert (reserved / "HEAD").exists()


def _default_snapshot(eng: SnapshotEngine, checkpoint_id: str, **kw):
    """常用形态快照：session s / agent a / type t / iteration 0（kw 覆盖）。"""
    params: dict = {
        "session_id": "s",
        "checkpoint_id": checkpoint_id,
        "agent_id": "a",
        "agent_type": "t",
        "iteration": 0,
    }
    params.update(kw)
    return asyncio.run(eng.snapshot(**params))


# ── SnapshotEngine end-to-end ───────────────────────────────────────────


class TestSnapshotEngine:
    @staticmethod
    def _engine(tmp_path: Path, **kw) -> SnapshotEngine:
        project = tmp_path / "project"
        project.mkdir()
        snap_dir = tmp_path / "snap"
        return SnapshotEngine(
            project_dir=project,
            snapshot_dir=snap_dir,
            snapshot_timeout_seconds=10.0,  # tests: generous; timeout has its own test
            **kw,
        )

    def test_snapshot_ok_writes_commit_and_trailer(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / "spec.md").write_text("# spec\n")
        (project / "src").mkdir()
        (project / "src" / "k.c").write_text("int main(){}\n")
        (project / ".workflow-controller").mkdir()  # must be excluded (recursion guard)
        (project / ".workflow-controller" / "events.db").write_text("db")

        result = asyncio.run(
            eng.snapshot(
                session_id="sess_abc",
                checkpoint_id="cp_001",
                agent_id="ses_sub1",
                agent_type="developer",
                iteration=3,
                trigger="subagent_stop",
                event_rowid=42,
                parent_checkpoint_id=None,
            )
        )
        assert result.status == "ok"
        assert result.git_ref and len(result.git_ref) == 40  # full sha
        assert result.file_count == 2  # spec.md + k.c (events.db excluded)
        assert result.byte_size > 0

        # trailer in git log
        from workflow_controller.plugins.agent_traceback.snapshot_engine import _run_sync

        r = _run_sync(["git", "log", "--format=%B", "-1"], cwd=eng.snapshot_dir)
        assert "workflow-controller snapshot" in r.stdout
        assert "session: sess_abc" in r.stdout
        assert "checkpoint: cp_001" in r.stdout
        assert "iteration: 3" in r.stdout
        assert "agent_id: ses_sub1" in r.stdout
        assert "trigger: subagent_stop" in r.stdout
        assert "event_rowid: 42" in r.stdout

    def test_snapshot_excludes_workflow_controller_and_git(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / "keep.txt").write_text("k")
        (project / ".git").mkdir()
        (project / ".git" / "config").write_text("ignore")
        (project / ".workflow-controller").mkdir()
        (project / ".workflow-controller" / "x").write_text("ignore")
        result = _default_snapshot(eng, "cp_001")
        assert result.status == "ok"
        assert result.file_count == 1

    def test_snapshot_no_change_yields_distinct_commit(self, tmp_path: Path):
        """Each checkpoint is a distinct git ref (own trailer), even with no file
        changes — ``--allow-empty`` records the checkpoint moment faithfully.
        """
        eng = self._engine(tmp_path)
        (eng.project_dir / "f.txt").write_text("v1")
        r1 = _default_snapshot(eng, "cp_001")
        assert r1.status == "ok"
        # second snapshot with no file changes → still a distinct commit (own trailer)
        r2 = _default_snapshot(eng, "cp_002")
        assert r2.status == "ok"
        assert r2.git_ref != r1.git_ref
        # both refs restore to the same file content
        d1, d2 = tmp_path / "f1", tmp_path / "f2"
        assert eng.restore(r1.git_ref, d1).ok
        assert eng.restore(r2.git_ref, d2).ok
        assert (d1 / "f.txt").read_text() == "v1"
        assert (d2 / "f.txt").read_text() == "v1"

    def test_snapshot_chains_parent_ref(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        (eng.project_dir / "f.txt").write_text("v1")
        r1 = _default_snapshot(eng, "cp_001")
        (eng.project_dir / "f.txt").write_text("v2-changed")
        r2 = _default_snapshot(
            eng,
            "cp_002",
            agent_id="b",
            iteration=1,
            parent_checkpoint_id="cp_001",
        )
        assert r2.status == "ok"
        assert r2.git_ref != r1.git_ref  # new commit (changed file)

    def test_disabled_returns_skipped(self, tmp_path: Path):
        eng = self._engine(tmp_path, enabled=False)
        (eng.project_dir / "f.txt").write_text("x")
        result = _default_snapshot(eng, "cp_001")
        assert result.status == "skipped"
        assert result.git_ref is None

    def test_restore_extracts_ref_to_dest(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / "spec.md").write_text("v1")
        (project / "src").mkdir()
        (project / "src" / "k.c").write_text("v1")
        r1 = _default_snapshot(eng, "cp_001")
        # mutate files, snapshot again
        (project / "spec.md").write_text("v2")
        (project / "src" / "k.c").write_text("v2")
        (project / "extra.txt").write_text("new")
        r2 = _default_snapshot(eng, "cp_002", agent_id="b", iteration=1)
        assert r1.git_ref != r2.git_ref

        # restore r1 → dest1 should have "v1" content (and no extra.txt)
        dest1 = tmp_path / "fork1"
        assert eng.restore(r1.git_ref, dest1).ok
        assert (dest1 / "spec.md").read_text() == "v1"
        assert (dest1 / "src" / "k.c").read_text() == "v1"
        assert not (dest1 / "extra.txt").exists()

        # restore r2 → dest2 should have v2 + extra.txt
        dest2 = tmp_path / "fork2"
        assert eng.restore(r2.git_ref, dest2).ok
        assert (dest2 / "spec.md").read_text() == "v2"
        assert (dest2 / "extra.txt").read_text() == "new"

    def test_restore_empty_checkpoint_succeeds(self, tmp_path: Path):
        """空检查点（working/ 无文件，如首个 subagent_start 前无 write）的
        commit 不含 working tree 条目（git 不跟踪空目录）。restore 须视为成功
        恢复出空文件态，而非失败——让首个检查点可被 fork 重放。

        回归：``git archive <ref> working`` 在此场景会以 ``pathspec did not
        match`` 失败；restore 已加 ``git ls-tree`` 前置兜底。
        """
        eng = self._engine(tmp_path)
        project = eng.project_dir
        # project 目录无任何项目文件（只有 .git/ 被排除）→ 快照 working/ 为空
        r = _default_snapshot(eng, "cp_empty")
        assert r.status == "ok"
        assert r.git_ref
        # restore 空检查点 → 成功 + dest 为空目录（无任何文件）
        dest = tmp_path / "fork_empty"
        assert eng.restore(r.git_ref, dest).ok is True
        assert dest.is_dir()
        assert not list(dest.iterdir()), "empty checkpoint should restore to empty dir"

        # 后续有文件后仍可正常 restore（确保兜底分支不影响正常路径）
        (project / "app.py").write_text("v1")
        r2 = _default_snapshot(eng, "cp_002", agent_id="b", iteration=1)
        dest2 = tmp_path / "fork2"
        assert eng.restore(r2.git_ref, dest2).ok
        assert (dest2 / "app.py").read_text() == "v1"

    def test_snapshot_captures_project_gitignored_files(self, tmp_path: Path):
        """Regression: the snapshot repo is independent of the project's
        .gitignore — a product file the project gitignores (e.g. greet.py /
        .opencode/agents/coder.md) MUST still be captured and restored, else a
        traceback fork_cwd loses product files + subagent definitions. rsync
        already filtered by the engine's ignore_patterns; `git add --force`
        ensures the project .gitignore doesn't leak into the snapshot repo.
        """
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / ".gitignore").write_text("greet.py\nreport.md\n.opencode/\n")
        (project / "greet.py").write_text('def greet(name): return "hi"\n')
        (project / "workflow-controller.yaml").write_text("runtime: opencode\n")
        (project / ".opencode").mkdir()
        (project / ".opencode" / "agents").mkdir()
        (project / ".opencode" / "agents" / "coder.md").write_text("coder def\n")
        r = _default_snapshot(eng, "cp_001")
        assert r.status == "ok"
        # restore must include the gitignored product file + agent def
        dest = tmp_path / "fork"
        assert eng.restore(r.git_ref, dest).ok
        assert (dest / "greet.py").read_text() == 'def greet(name): return "hi"\n'
        assert (dest / ".opencode" / "agents" / "coder.md").read_text() == "coder def\n"
        assert (dest / "workflow-controller.yaml").read_text() == "runtime: opencode\n"

    def test_timeout_returns_timeout_status(self, tmp_path: Path, monkeypatch):
        """Force snapshot_timeout very low + a slow mirror → timeout status."""
        eng = self._engine(tmp_path)
        eng.snapshot_timeout = 0.001  # 1ms — anything will time out

        async def _slow_mirror(*a, **kw):
            await asyncio.sleep(0.5)
            return 0, 0

        monkeypatch.setattr(eng, "_sync_mirror", _slow_mirror)

        result = _default_snapshot(eng, "cp_001")
        assert result.status == "timeout"
        assert result.git_ref is None

    def test_list_files_lists_checkpoint_file_state(self, tmp_path: Path):
        """list_files previews the on-disk state at a checkpoint's git_ref
        (top-level + subdirectory) without restoring the whole tree.
        """
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / "spec.md").write_text("# spec\n")
        (project / "src").mkdir()
        (project / "src" / "k.c").write_text("int main(){}\n")
        (project / "src" / "sub").mkdir()
        (project / "src" / "sub" / "deep.py").write_text("x = 1\n")
        r = _default_snapshot(eng, "cp_001")
        assert r.status == "ok" and r.git_ref
        # top-level: dirs first then files
        top = eng.list_files(r.git_ref)
        names = [(e["name"], e["is_dir"]) for e in top]
        assert ("src", True) in names
        assert ("spec.md", False) in names
        # src/ subdirectory
        src = eng.list_files(r.git_ref, "src")
        assert {e["name"] for e in src} == {"k.c", "sub"}
        assert next(e for e in src if e["name"] == "sub")["is_dir"] is True
        # src/sub/ subdirectory
        sub = eng.list_files(r.git_ref, "src/sub")
        assert [e["name"] for e in sub] == ["deep.py"]
        assert sub[0]["path"] == "src/sub/deep.py"

    def test_read_file_reads_checkpoint_file_content(self, tmp_path: Path):
        """read_file returns a checkpoint's file content at git_ref."""
        eng = self._engine(tmp_path)
        project = eng.project_dir
        (project / "spec.md").write_text("# greet spec\n")
        (project / "src").mkdir()
        (project / "src" / "k.c").write_text("int main(){}\n")
        r = _default_snapshot(eng, "cp_001")
        assert r.status == "ok" and r.git_ref
        spec = eng.read_file(r.git_ref, "spec.md")
        assert spec is not None
        assert spec["content"] == "# greet spec\n"
        assert spec["truncated"] is False
        assert spec["name"] == "spec.md"
        kc = eng.read_file(r.git_ref, "src/k.c")
        assert kc is not None and kc["content"] == "int main(){}\n"
        # missing file → None
        assert eng.read_file(r.git_ref, "nope.py") is None


@pytest.mark.skipif(not _has_rsync(), reason="rsync not installed")
class TestRsyncMirror:
    @staticmethod
    def test_rsync_path_copies_and_excludes(tmp_path: Path):
        src = tmp_path / "project"
        dest = tmp_path / "working"
        src.mkdir()
        (src / "keep.txt").write_text("k")
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("ignore")
        dest.mkdir()
        res = _rsync_mirror(src, dest, DEFAULT_IGNORE_PATTERNS)
        assert res is not None
        fc, _bs = res
        assert fc == 1
        assert (dest / "keep.txt").read_text() == "k"
        assert not (dest / ".git" / "config").exists()


# ── 内部 git 状态捕获（改名镜像 .workflow-controller-inner-git） ──────────────────────


def _git_env() -> dict:
    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@local",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@local",
        }
    )
    return env


def _init_repo(project: Path) -> None:
    import subprocess as sp

    sp.run(["git", "init"], cwd=str(project), capture_output=True, env=_git_env())
    (project / "spec.md").write_text("v1\n")
    sp.run(["git", "add", "-A"], cwd=str(project), capture_output=True, env=_git_env())
    sp.run(
        ["git", "commit", "-m", "c1"],
        cwd=str(project),
        capture_output=True,
        env=_git_env(),
    )


def _rev_parse(cwd: Path, ref: str = "HEAD") -> str:
    import subprocess as sp

    r = sp.run(
        ["git", "rev-parse", ref],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _tree_paths(engine: SnapshotEngine, git_ref: str) -> set[str]:
    """commit 树里 working/ 下的全部路径名。"""
    from workflow_controller.plugins.agent_traceback.snapshot_engine import _run_sync

    r = _run_sync(
        ["git", "ls-tree", "-r", "--name-only", git_ref],
        cwd=engine.snapshot_dir,
    )
    assert r.returncode == 0, r.stderr
    return set(r.stdout.splitlines())


class TestInnerGitCapture:
    @staticmethod
    def _engine(tmp_path: Path, **kw) -> SnapshotEngine:
        project = tmp_path / "project"
        project.mkdir()
        return SnapshotEngine(
            project_dir=project,
            snapshot_dir=tmp_path / "snap",
            snapshot_timeout_seconds=10.0,
            **kw,
        )

    @staticmethod
    def _snapshot(eng: SnapshotEngine, cp_id: str = "cp_001"):
        return asyncio.run(
            eng.snapshot(
                session_id="s",
                checkpoint_id=cp_id,
                agent_id="a",
                agent_type="t",
                iteration=0,
            )
        )

    def test_git_repo_snapshot_captures_inner_git(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        r = self._snapshot(eng)
        assert r.status == "ok"
        assert r.inner_git == "captured"
        paths = _tree_paths(eng, r.git_ref)
        assert "working/.workflow-controller-inner-git/HEAD" in paths
        assert "working/.workflow-controller-inner-git/config" in paths
        assert "working/spec.md" in paths

    def test_no_git_repo_marks_absent(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        (eng.project_dir / "a.txt").write_text("x")
        r = self._snapshot(eng)
        assert r.status == "ok"
        assert r.inner_git == "absent"
        assert not any(
            p.startswith("working/.workflow-controller-inner-git")
            for p in _tree_paths(eng, r.git_ref)
        )

    def test_git_is_file_marks_absent(self, tmp_path: Path):
        # linked worktree / submodule 的 .git 是 gitdir 指针文件 → 不捕获
        eng = self._engine(tmp_path)
        (eng.project_dir / ".git").write_text("gitdir: /somewhere/else\n")
        r = self._snapshot(eng)
        assert r.status == "ok"
        assert r.inner_git == "absent"

    def test_over_limit_marks_skipped_and_clears_stale(self, tmp_path: Path):
        eng = self._engine(tmp_path, snapshot_git_state_max_mb=1)
        _init_repo(eng.project_dir)
        r1 = self._snapshot(eng, "cp_001")
        assert r1.status == "ok"
        assert r1.inner_git == "captured"
        # 撑爆 1MB 上限 → 下一检查点 skipped_size，且旧的保留目录必须从树里消失
        (eng.project_dir / ".git" / "objects" / "big.blob").write_bytes(b"x" * (2 * 1024 * 1024))
        r2 = self._snapshot(eng, "cp_002")
        assert r2.status == "ok"
        assert r2.inner_git == "skipped_size"
        paths = _tree_paths(eng, r2.git_ref)
        assert not any(p.startswith("working/.workflow-controller-inner-git") for p in paths)

    def test_restore_recovers_git_state_at_snapshot_time(self, tmp_path: Path):
        import subprocess as sp

        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        # T1：打快照（.git 停在 c1）
        r = self._snapshot(eng)
        assert r.status == "ok"
        t1_head = _rev_parse(eng.project_dir)
        # T1 之后 origin 继续提交（未来历史）
        (eng.project_dir / "spec.md").write_text("v2\n")
        sp.run(["git", "add", "-A"], cwd=str(eng.project_dir), capture_output=True, env=_git_env())
        sp.run(
            ["git", "commit", "-m", "future"],
            cwd=str(eng.project_dir),
            capture_output=True,
            env=_git_env(),
        )
        # restore 回 T1：HEAD == T1，log 不含未来 commit
        dest = tmp_path / "fork"
        res = eng.restore(r.git_ref, dest)
        assert res.ok and res.inner_git_captured
        assert (dest / ".git").is_dir()
        assert not (dest / ".workflow-controller-inner-git").exists()
        assert _rev_parse(dest) == t1_head
        log = sp.run(
            ["git", "log", "--oneline"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        assert "future" not in log.stdout
        assert "c1" in log.stdout

    def test_restore_carries_index_state(self, tmp_path: Path):
        # 暂存态（git add 未 commit）随 .git 一起恢复 → git status 与检查点时刻一致
        import subprocess as sp

        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        (eng.project_dir / "staged.txt").write_text("staged\n")
        sp.run(
            ["git", "add", "staged.txt"],
            cwd=str(eng.project_dir),
            capture_output=True,
            env=_git_env(),
        )
        origin_status = sp.run(
            ["git", "status", "--porcelain"],
            cwd=str(eng.project_dir),
            capture_output=True,
            text=True,
            env=_git_env(),
        ).stdout
        r = self._snapshot(eng)
        assert r.status == "ok"
        dest = tmp_path / "fork"
        res = eng.restore(r.git_ref, dest)
        assert res.ok and res.inner_git_captured
        fork_status = sp.run(
            ["git", "status", "--porcelain"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            env=_git_env(),
        ).stdout
        assert fork_status == origin_status
        assert "A  staged.txt" in fork_status

    def test_restore_old_style_checkpoint_degrades(self, tmp_path: Path):
        # 旧式检查点（snapshot_git_state=false，树里无保留目录）→ restore 正常、无 .git
        eng = self._engine(tmp_path, snapshot_git_state=False)
        _init_repo(eng.project_dir)
        r = self._snapshot(eng)
        assert r.status == "ok"
        assert r.inner_git == ""
        assert not any(
            p.startswith("working/.workflow-controller-inner-git")
            for p in _tree_paths(eng, r.git_ref)
        )
        dest = tmp_path / "fork"
        res = eng.restore(r.git_ref, dest)
        assert res.ok and res.inner_git_captured is False
        assert not (dest / ".git").exists()
        assert (dest / "spec.md").read_text() == "v1\n"

    def test_list_files_hides_reserved_dir(self, tmp_path: Path):
        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        r = self._snapshot(eng)
        names = [e["name"] for e in eng.list_files(r.git_ref)]
        assert ".workflow-controller-inner-git" not in names
        assert "spec.md" in names

    def test_working_mirror_preserves_reserved_dir_incrementally(self, tmp_path: Path):
        """工作树镜像趟（--delete）不得删掉 working/ 里上一检查点的内部 git 镜像。"""
        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        r1 = self._snapshot(eng, "cp_001")
        assert r1.status == "ok" and r1.inner_git == "captured"
        assert (eng.working_dir / ".workflow-controller-inner-git" / "HEAD").exists()
        # 工作树有变更 → 第二次快照走完整 _sync_mirror（--delete）后镜像仍须留存
        (eng.project_dir / "spec.md").write_text("v2\n")
        r2 = self._snapshot(eng, "cp_002")
        assert r2.status == "ok" and r2.inner_git == "captured"
        assert (eng.working_dir / ".workflow-controller-inner-git" / "HEAD").exists()
        assert any(
            p.startswith("working/.workflow-controller-inner-git")
            for p in _tree_paths(eng, r2.git_ref)
        )
        # 删除项目文件后镜像目录依旧留存（delete 只作用于工作树范围）
        (eng.project_dir / "spec.md").unlink()
        r3 = self._snapshot(eng, "cp_003")
        assert r3.status == "ok"
        assert (eng.working_dir / ".workflow-controller-inner-git" / "HEAD").exists()

    @pytest.mark.skipif(not _has_rsync(), reason="rsync not installed")
    def test_inner_git_mirror_catches_same_mtime_ref_change(self, tmp_path: Path):
        """ref 恒 41 字节、固定名，「同 mtime、同长、不同内容」须靠 checksum 趟抓。"""
        import os as _os
        import subprocess as sp

        from workflow_controller.plugins.agent_traceback.snapshot_engine import (
            INNER_GIT_RESERVED_PATH,
            mirror_inner_git,
        )

        project = tmp_path / "project"
        working = tmp_path / "working"
        project.mkdir()
        working.mkdir()
        _init_repo(project)
        head1 = _rev_parse(project)
        assert mirror_inner_git(project, working) == "captured"
        ref = project / ".git" / "refs" / "heads" / "master"
        if not ref.exists():  # init 默认分支可能是 main
            ref = project / ".git" / "refs" / "heads" / "main"
        assert ref.exists()
        _os.utime(ref, ns=(1_000_000_000_000_000_000, 0))
        _os.utime(
            working / INNER_GIT_RESERVED_PATH / "refs" / "heads" / ref.name,
            ns=(1_000_000_000_000_000_000, 0),
        )
        # origin 打第二个 commit，并把 ref mtime 钉回同一值（模拟同秒重写）
        (project / "spec.md").write_text("v2\n")
        sp.run(["git", "add", "-A"], cwd=str(project), capture_output=True, env=_git_env())
        sp.run(
            ["git", "commit", "-m", "c2"],
            cwd=str(project),
            capture_output=True,
            env=_git_env(),
        )
        head2 = _rev_parse(project)
        assert head2 != head1
        _os.utime(ref, ns=(1_000_000_000_000_000_000, 0))
        assert mirror_inner_git(project, working) == "captured"
        dest_ref = working / INNER_GIT_RESERVED_PATH / "refs" / "heads" / ref.name
        assert dest_ref.read_text().strip() == head2, "mirror missed the ref rewrite"

    def test_inner_git_mirror_strips_remotes(self, tmp_path: Path):
        """remote URL（可能内嵌 token）不得持久化进快照仓历史。"""
        import subprocess as sp

        from workflow_controller.plugins.agent_traceback.snapshot_engine import _run_sync

        eng = self._engine(tmp_path)
        _init_repo(eng.project_dir)
        sp.run(
            ["git", "remote", "add", "origin", "https://user:secret@example.com/x.git"],
            cwd=str(eng.project_dir),
            capture_output=True,
            env=_git_env(),
        )
        r = self._snapshot(eng)
        assert r.status == "ok" and r.inner_git == "captured"
        # 落库的 config blob 不含 remote URL / token
        cfg = _run_sync(
            ["git", "show", f"{r.git_ref}:working/.workflow-controller-inner-git/config"],
            cwd=eng.snapshot_dir,
        )
        assert cfg.returncode == 0
        assert "example.com" not in cfg.stdout
        assert "secret" not in cfg.stdout
        # restore 出的 .git 无 remote
        dest = tmp_path / "fork"
        res = eng.restore(r.git_ref, dest)
        assert res.ok and res.inner_git_captured
        remotes = sp.run(
            ["git", "remote"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        assert remotes.returncode == 0
        assert remotes.stdout.split() == []
