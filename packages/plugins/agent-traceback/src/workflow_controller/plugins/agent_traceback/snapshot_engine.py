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

"""Git 文件态快照引擎。

在独立快照仓 ``<project_dir>/.workflow-controller/snapshots/`` 里为每个 subagent stop 时刻打一次
文件态快照：rsync（不可用降级纯 Python tree-sync）镜像 ``project_dir`` → ``working/``，
再 ``git add -A && git commit``（commit message 带结构化 trailer，可 ``git log --grep``
反查 EventStore）。``restore(ref, dest)`` 用 ``git archive`` 抽出某 ref 的文件态。

关键约束：``snapshot`` 是 ``async def``，调用方**必须** ``await``——同步阻塞 worker 让
文件态冻结在 subagent stop 时刻。绝不能 fire-and-forget（``spawn``/``create_task``），
否则 worker 在后台快照期间继续写文件，捕获的就不是检查点时刻的文件态。

单次快照硬超时 30s（``asyncio.wait_for``）：超时记 ``status=timeout`` 跳过该检查点，不
阻塞 workflow。git 不可用 → ``status=unavailable``（仅留事件态，无文件态）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# 镜像排除：默认全收，排除 .workflow-controller（防递归）、项目 .git、构建产物等；用户可在
# workflow-controller.yaml traceback.ignore 覆盖。
DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".workflow-controller",
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "*.pyc",
]

# 单次快照硬超时（秒）。超时跳过该检查点，不阻塞 workflow。
#
# 该值须远大于 git add/commit/rev-parse 本身的耗时（通常 <0.1s），因为
# ``_do_snapshot`` 内的 git 子进程经 ``asyncio.create_subprocess_exec`` 调度，
# 依赖事件循环推进；而 opencode 驱动的后台轮询器（``_poll_subagent_stream``）
# 会对体量可达数 GB 的 opencode.db 做同步 ``list_subagent_session_ids`` 全索引扫描，
# 冷缓存时阻塞事件循环 1-2s。2s 的旧值会在冷缓存/并发 subagent 场景下被这一
# 阻塞吃光，导致 ``asyncio.wait_for`` 超时、检查点以 ``status=timeout`` 落库
# （无 git_ref，不可重放）。30s 既给足余量又不至于在真正卡死时长时间阻塞 workflow。
DEFAULT_SNAPSHOT_TIMEOUT_SECONDS: float = 30.0

# 单 session 检查点上限，超出滚降最早的。
DEFAULT_MAX_CHECKPOINTS: int = 200

# 内部 git 状态捕获总开关默认值（false = 完全维持旧行为，逃生舱）。
DEFAULT_SNAPSHOT_GIT_STATE: bool = True
# 内部 .git 镜像体积上限（MB）。0 = 不限制；超限跳过捕获，重放走降级路径。
DEFAULT_SNAPSHOT_GIT_STATE_MAX_MB: int = 200

# 内部 .git 在快照仓 working/ 下的保留路径名：避开 git 保留名（.git 会被
# git add 静默跳过 / 嵌套 .git 被当 gitlink），restore 后改回 .git。
INNER_GIT_RESERVED_PATH = ".workflow-controller-inner-git"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "workflow-controller-snapshot",
    "GIT_AUTHOR_EMAIL": "workflow-controller-snapshot@local",
    "GIT_COMMITTER_NAME": "workflow-controller-snapshot",
    "GIT_COMMITTER_EMAIL": "workflow-controller-snapshot@local",
}


def _git_env() -> dict[str, str]:
    """Process env + git author identity (so commits are attributable)."""
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return env


@dataclass(frozen=True)
class SnapshotResult:
    """单次快照的结果。``status`` 区分成功/超时/不可用/跳过。

    - ``ok``：快照成功，``git_ref`` 指向新 commit。
    - ``timeout``：超时跳过，``git_ref`` 为 None（该检查点不可重放文件态）。
    - ``unavailable``：git 不可用 / 仓库初始化失败。
    - ``skipped``：``enabled=False`` 或无文件变更且无前序快照。
    - ``error``：其他异常（best-effort 不抛）。

    ``inner_git``：内部 git 捕获结果（``captured`` / ``absent`` /
    ``skipped_size``；空串 = 捕获关闭或快照未成功）。
    """

    status: str
    git_ref: str | None = None
    file_count: int = 0
    byte_size: int = 0
    error: str | None = None
    checkpoint_id: str | None = None
    inner_git: str = ""


@dataclass(frozen=True)
class RestoreResult:
    """``restore`` 结果：``ok`` 是否成功，``inner_git_captured`` 是否恢复出 ``.git``。"""

    ok: bool
    inner_git_captured: bool = False


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run_sync(
    cmd: Sequence[str],
    cwd: Path,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    """同步跑子进程，捕获输出。失败不抛（调用方检查 returncode）。"""
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_git_env(),
    )


async def _run_async(
    cmd: Sequence[str],
    cwd: Path,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """异步跑子进程，返回 (returncode, stdout, stderr)。不抛异常。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *list(cmd),
            cwd=str(cwd),
            env=_git_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "command not found"
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace"),
        stderr_b.decode(errors="replace"),
    )


def _match_double_star(parts: list[str], clean: str) -> bool:
    """``**`` 模式匹配：把 ``**`` 当作「任意层」，逐段比对非 ``**`` 部分。"""
    from fnmatch import fnmatch

    segs = [s for s in clean.split("/") if s and s != "**"]
    if not segs:
        return False
    pi = 0
    for seg in parts:
        if pi < len(segs) and fnmatch(seg, segs[pi]):
            pi += 1
        if pi == len(segs):
            return True
    return False


def _match_ignore(rel_path: str, patterns: Sequence[str]) -> bool:
    """纯 Python glob 判断 rel_path 是否应被忽略。

    rel_path 用 ``/`` 分隔、相对 project_dir、无前导 ``./``。匹配逻辑：
    - 模式以 ``/`` 结尾或无通配 → 匹配路径任一段（目录）。
    - 含 ``**`` → 任意层目录。
    - 含 ``*`` → 单段内通配。
    """
    from fnmatch import fnmatch

    parts = rel_path.replace("\\", "/").split("/")
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        # 去掉尾部 /
        clean = p.rstrip("/")
        # 整路径匹配
        if fnmatch(rel_path, clean):
            return True
        # 任一段匹配（目录名匹配 → 该文件在忽略目录下）
        for seg in parts:
            if fnmatch(seg, clean):
                return True
        # ** 形式：匹配前缀
        if "**" in clean and _match_double_star(parts, clean):
            return True
    return False


def _collect_src_files(src: Path, ignore_patterns: Sequence[str]) -> dict[str, Path]:
    """收集源文件（相对路径 → abs），忽略目录剪枝。"""
    src_files: dict[str, Path] = {}
    for root, dirs, files in os.walk(src):
        # 过滤忽略目录（剪枝）
        rel_root = os.path.relpath(root, src).replace("\\", "/")
        kept_dirs = []
        for d in dirs:
            if not _match_ignore(
                f"{rel_root}/{d}".lstrip("./"),
                ignore_patterns,
            ):
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), src).replace("\\", "/")
            if _match_ignore(rel, ignore_patterns):
                continue
            src_files[rel] = Path(root) / f
    return src_files


def _unlink_stale_dest_file(
    root: str,
    fname: str,
    rel: str,
    src_files: dict[str, Path],
    ignore_patterns: Sequence[str],
) -> None:
    """删除 dest 中不在 src 的单个文件；匹配 ignore 的跳过（与 rsync 的
    ``--delete`` + ``--exclude`` 组合语义一致：排除项不受删除影响）。"""
    if rel in src_files or _match_ignore(rel, ignore_patterns):
        return
    with contextlib.suppress(OSError):
        (Path(root) / fname).unlink()


def _delete_stale_dest_files(
    dest: Path, src_files: dict[str, Path], ignore_patterns: Sequence[str]
) -> None:
    """删除 dest 中不在 src 的文件。匹配 ignore 的跳过（与 rsync 语义一致：
    --exclude 项不受 --delete 影响）。"""
    for root, _dirs, files in os.walk(dest):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), dest).replace("\\", "/")
            _unlink_stale_dest_file(root, f, rel, src_files, ignore_patterns)


def _mirror_copy_needed(sp: Path, dp: Path) -> bool:
    """目标缺失或 size/mtime_ns 不一致时需复制。

    纳秒 mtime + size 判定：避免「同一秒内、同尺寸、不同内容」被漏掉
    （subagent 改一个配置数字常见此情形）。stat 失败按需复制处理。
    """
    if not dp.exists():
        return True
    try:
        ss = sp.stat()
        ds = dp.stat()
    except OSError:
        return True
    return not (ss.st_size == ds.st_size and ss.st_mtime_ns == ds.st_mtime_ns)


def _copy_changed_files(src_files: dict[str, Path], dest: Path) -> tuple[int, int]:
    """复制新/改文件（按 size+mtime_ns 判定），返回 (file_count, byte_size)。"""
    file_count = 0
    byte_size = 0
    for rel, sp in src_files.items():
        dp = dest / rel
        try:
            if not dp.parent.exists():
                dp.parent.mkdir(parents=True, exist_ok=True)
            if _mirror_copy_needed(sp, dp):
                shutil.copy2(sp, dp)
            st = dp.stat()
            file_count += 1
            byte_size += st.st_size
        except OSError:
            logger.debug("snapshot mirror: skip %s", rel, exc_info=True)
    return file_count, byte_size


def _prune_empty_dirs(dest: Path) -> None:
    """清理空目录（best-effort）。"""
    for root, dirs, _files in list(os.walk(dest, topdown=False)):
        for d in dirs:
            with contextlib.suppress(OSError):
                (Path(root) / d).rmdir()


def _python_sync_mirror(
    src: Path,
    dest: Path,
    ignore_patterns: Sequence[str],
) -> tuple[int, int]:
    """纯 Python 增量镜像：复制新/改文件（按 size+mtime），删除源已删文件。

    rsync 不可用时的降级路径。返回 (file_count, byte_size)。best-effort。
    """
    src = src.resolve()
    dest = dest.resolve()
    src_files = _collect_src_files(src, ignore_patterns)
    _delete_stale_dest_files(dest, src_files, ignore_patterns)
    file_count, byte_size = _copy_changed_files(src_files, dest)
    _prune_empty_dirs(dest)
    return file_count, byte_size


def _rsync_mirror(
    src: Path,
    dest: Path,
    ignore_patterns: Sequence[str],
) -> tuple[int, int] | None:
    """用 rsync 镜像。不可用返回 None（调用方降级到 Python）。

    用 ``--checksum``（而非默认的 size+mtime）判定是否需传输——避免「同一秒内、
    同尺寸、不同内容」的改动被漏掉（subagent 改一个配置数字常见此情形）。
    """
    rsync = shutil.which("rsync")
    if rsync is None:
        return None
    # 构造 --exclude
    excludes = []
    for pat in ignore_patterns:
        p = pat.strip().rstrip("/")
        if p:
            excludes.extend(["--exclude", p])
    # src 末尾加 / → 复制内容到 dest（不嵌一层）
    src_arg = f"{src}/"
    cmd = [rsync, "-a", "--delete", "--checksum", *excludes, src_arg, str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    # 统计
    file_count = 0
    byte_size = 0
    for root, _dirs, files in os.walk(dest):
        for f in files:
            fp = Path(root) / f
            try:
                st = fp.stat()
                file_count += 1
                byte_size += st.st_size
            except OSError:
                pass
    return file_count, byte_size


def _dir_size_bytes(path: Path, limit: int = 0) -> int:
    """目录体积（字节），best-effort（walk 失败按 0 计）。

    ``limit`` > 0 时累计超限即提前返回——调用方只需判定是否超限，避免在
    本就要跳过的大仓（objects/pack 躺着数 GB packfile）上每次扫完全量 walk。
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
        if limit > 0 and total > limit:
            break
    return total


def _rsync_inner_git(src: Path, dest: Path, rsync: str) -> bool:
    """两趟 rsync 镜像内部 .git；返回 quick-check 趟是否成功。"""
    try:
        # checksum 趟失败不致命（quick-check 趟仍能收全量，只失去同秒漏传防护）
        r = subprocess.run(
            [
                rsync,
                "-a",
                "--checksum",
                "--delete",
                "--exclude",
                "objects",
                f"{src}/",
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            logger.debug(
                "mirror_inner_git: checksum pass rc=%d err=%s",
                r.returncode,
                r.stderr.strip()[:200],
            )
        r = subprocess.run(
            [rsync, "-a", "--delete", f"{src}/", str(dest)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def mirror_inner_git(project_dir: Path, working_dir: Path, max_mb: int = 200) -> str:
    """镜像 ``project_dir/.git`` → ``working_dir/.workflow-controller-inner-git``。

    返回 ``captured`` / ``absent``（无 .git 或 .git 非目录）/ ``skipped_size``。
    两趟 rsync：checksum 趟（排除 objects/）抓固定名小文件的「同秒同长不同内容」
    改动（ref 恒 41 字节，quick-check 会漏）；quick-check 趟收 objects/ 增量
    （内容寻址，quick-check 足够，checksum 只会白读 GB 级 packfile），并兜
    checksum 趟失败。rsync 不可用降级纯 Python（size+mtime_ns 判定，**不保证**
    捕获同秒同长的 ref 重写，需保真的场景应安装 rsync；镜像残缺——dest 缺
    HEAD——视为 ``absent`` 走降级）。同步后剥除镜像的 remote
    （URL 可能内嵌 token，不得持久化进快照仓历史）。超限 / absent 时删除
    保留目录（旧态属更早时刻，留着会伪装成本检查点状态）。
    """
    src = Path(project_dir) / ".git"
    dest = Path(working_dir) / INNER_GIT_RESERVED_PATH
    if not src.is_dir():
        # .git 为文件（linked worktree/submodule 的 gitdir 指针）→ 恢复后必坏，不捕获
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return "absent"
    size_limit = max_mb * 1024 * 1024
    if max_mb > 0 and _dir_size_bytes(src, limit=size_limit) > size_limit:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return "skipped_size"
    captured = False
    rsync = shutil.which("rsync")
    if rsync is not None:
        captured = _rsync_inner_git(src, dest, rsync)
    if not captured:
        # rsync 不可用 / 失败 → 纯 Python 降级（size+mtime_ns 快速判定 + delete 语义）。
        # 该路径不保证捕获同秒同长的 ref 重写（见 docstring），需保真场景应装 rsync。
        _python_sync_mirror(src, dest, [])
        if not (dest / "HEAD").is_file():
            # 源中途消失 / 镜像半残（纯 Python 路径 best-effort 不抛）：不留残骸、
            # 不对其跑 remote 剥除，标 absent 让 restore 端走降级分支
            shutil.rmtree(dest, ignore_errors=True)
            return "absent"
    remove_all_remotes(dest)
    return "captured"


def remove_all_remotes(git_dir: Path) -> None:
    """剥除指定 .git 的全部 remote（防 fetch 连外部 / 内嵌 token 的 URL 持久化）。

    公开函数：``traceback_runner`` 跨模块复用。best-effort。
    """
    git = shutil.which("git")
    if git is None:
        return
    try:
        r = subprocess.run(
            [git, "--git-dir", str(git_dir), "remote"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return
        for name in r.stdout.split():
            subprocess.run(
                [git, "--git-dir", str(git_dir), "remote", "rm", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (subprocess.TimeoutExpired, OSError):
        logger.debug("remove_all_remotes failed", exc_info=True)


def build_snapshot_commit_message(
    session_id: str,
    checkpoint_id: str,
    agent_id: str,
    agent_type: str,
    iteration: int,
    trigger: str,
    event_rowid: int | None,
    parent_checkpoint_id: str | None,
    main_part_id: str | None,
) -> str:
    """快照 commit 的结构化 trailer，可 git log --grep 反查（engine 与重建轨共用）。"""
    return (
        "workflow-controller snapshot\n"
        "\n"
        f"session: {session_id}\n"
        f"checkpoint: {checkpoint_id}\n"
        f"iteration: {iteration}\n"
        f"agent_id: {agent_id or 'main'}\n"
        f"agent_type: {agent_type or ''}\n"
        f"trigger: {trigger}\n"
        f"parent_checkpoint: {parent_checkpoint_id or ''}\n"
        f"event_rowid: {event_rowid if event_rowid is not None else ''}\n"
        f"main_part_id: {main_part_id or ''}\n"
    )


def _extract_working_tree(snapshot_dir: Path, git_ref: str, dest_dir: Path) -> bool:
    """``git archive <ref> working | tar -x`` 把文件态解到 dest_dir（剥 working/ 前缀）。"""
    git = shutil.which("git")
    tar_executable = shutil.which("tar")
    if git is None or tar_executable is None:
        logger.warning("snapshot restore failed: git or tar not available")
        return False
    archive_cmd = [git, "archive", "--format=tar", git_ref, "working"]
    try:
        archive = subprocess.Popen(
            archive_cmd,
            cwd=str(snapshot_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
        )
        # tar 从 stdin 解包，--strip-components=1 去掉 working/ 前缀
        tar = subprocess.Popen(
            [tar_executable, "-x", "--strip-components=1", "-C", str(dest_dir)],
            stdin=archive.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        archive.stdout.close()  # type: ignore[union-attr]
        _a_out, a_err = archive.communicate()
        _t_out, t_err = tar.communicate()
        if archive.returncode != 0 or tar.returncode != 0:
            logger.warning(
                "snapshot restore: archive=%s tar=%s err=%s/%s",
                archive.returncode,
                tar.returncode,
                a_err.decode(errors="replace")[:200],
                t_err.decode(errors="replace")[:200],
            )
            return False
        return True
    except OSError as exc:
        logger.warning("snapshot restore failed: %s", exc)
        return False


def _restore_inner_git(dest_dir: Path) -> bool:
    """把解包出的内部 git 保留目录改回 ``.git`` 并补齐 git init 骨架目录。"""
    dest_git = dest_dir / ".git"
    if dest_git.exists():
        if dest_git.is_dir():
            shutil.rmtree(dest_git, ignore_errors=True)
        else:
            dest_git.unlink(missing_ok=True)
    reserved_dir = dest_dir / INNER_GIT_RESERVED_PATH
    if not reserved_dir.is_dir():
        return False
    # rmtree 静默失败（挂载点 / 只读 / OS 锁）→ dest_git 残留时 rename
    # 会失败。内部 git 是可选增强：降级 inner_git_captured=False，
    # 不让整次文件态 restore 失败（本体已完整解出）。
    try:
        reserved_dir.rename(dest_git)
    except OSError:
        logger.warning(
            "snapshot restore: inner-git rename failed — degrade",
            exc_info=True,
        )
        shutil.rmtree(reserved_dir, ignore_errors=True)
        return False
    # 补 git init 骨架目录：unborn/空仓的 objects、refs 是空目录，
    # git 不跟踪空目录（archive 抽不出），缺了 git 不认作有效仓
    for sub in (
        "objects/info",
        "objects/pack",
        "refs/heads",
        "refs/tags",
    ):
        (dest_git / sub).mkdir(parents=True, exist_ok=True)
    return True


class SnapshotEngine:
    """文件态快照引擎：在独立 git 仓里为每个 subagent stop 打快照。

    线程安全：内部 ``asyncio.Lock`` 串行化快照（git 不并发安全）。调用方仍须 ``await``
    ``snapshot()`` 以阻塞 worker（保证文件态冻结在 stop 时刻）。
    """

    def __init__(
        self,
        project_dir: Path,
        snapshot_dir: Path | None = None,
        ignore_patterns: Sequence[str] | None = None,
        snapshot_timeout_seconds: float = DEFAULT_SNAPSHOT_TIMEOUT_SECONDS,
        enabled: bool = True,
        max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS,
        snapshot_git_state: bool = DEFAULT_SNAPSHOT_GIT_STATE,
        snapshot_git_state_max_mb: int = DEFAULT_SNAPSHOT_GIT_STATE_MAX_MB,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.snapshot_dir = (
            Path(snapshot_dir).expanduser().resolve()
            if snapshot_dir is not None
            else (self.project_dir / ".workflow-controller" / "snapshots").resolve()
        )
        self.working_dir = self.snapshot_dir / "working"
        self.ignore_patterns = list(
            ignore_patterns if ignore_patterns is not None else DEFAULT_IGNORE_PATTERNS
        )
        # 保留路径无条件排除：工作树镜像趟的 --delete 不得删掉上一检查点的内部 git 镜像
        if INNER_GIT_RESERVED_PATH not in self.ignore_patterns:
            self.ignore_patterns.append(INNER_GIT_RESERVED_PATH)
        self.snapshot_timeout = snapshot_timeout_seconds
        self.enabled = enabled
        self.max_checkpoints = max_checkpoints
        self.snapshot_git_state = snapshot_git_state
        self.snapshot_git_state_max_mb = snapshot_git_state_max_mb
        self._lock = asyncio.Lock()
        self._initialized = False

    # ── 仓库初始化 ────────────────────────────────────────────────────

    # ── 快照 ──────────────────────────────────────────────────────────

    async def snapshot(
        self,
        *,
        session_id: str,
        checkpoint_id: str,
        agent_id: str,
        agent_type: str,
        iteration: int,
        trigger: str = "subagent_stop",
        event_rowid: int | None = None,
        parent_checkpoint_id: str | None = None,
        main_part_id: str | None = None,
    ) -> SnapshotResult:
        """打一次文件态快照。**必须 await**（同步阻塞 worker 保证文件态一致）。"""
        if not self.enabled:
            return SnapshotResult(status="skipped", checkpoint_id=checkpoint_id)
        if not self._ensure_repo():
            return SnapshotResult(
                status="unavailable",
                checkpoint_id=checkpoint_id,
                error="git not available",
            )
        payload = {
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "iteration": iteration,
            "trigger": trigger,
            "event_rowid": event_rowid,
            "parent_checkpoint_id": parent_checkpoint_id,
            "main_part_id": main_part_id,
        }
        return await self._snapshot_locked(checkpoint_id, payload)

    def restore(self, git_ref: str, dest_dir: Path) -> RestoreResult:
        """抽出某 ref 的文件态到 dest_dir。

        用 ``git archive <ref> working | tar -x -C dest_dir``，隔离不污染快照仓工作树。
        dest_dir 会先清空再解包。若该检查点带内部 git 保留目录，解包后把它改回
        ``dest_dir/.git`` 并置 ``inner_git_captured=True``。

        ``inner_git_captured`` 仅表示树里含保留目录（``git ls-tree`` 探测）；恢复出的
        ``.git`` 是否最终被采用，还取决于重放端 ``_init_git_sandbox`` 的 ``rev-parse``
        sanity（半残镜像会在此降级复制 origin 当刻 ``.git``）。

        空检查点（``working/`` 无文件）的 commit 不含 ``working`` tree 条目，
        ``git archive`` 会失败——此情况视为「成功恢复空文件态」。
        """
        if not self._ensure_repo():
            return RestoreResult(ok=False)
        dest_dir = Path(dest_dir).expanduser().resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 空检查点：tree 不含 working 条目 → 视为空文件态恢复成功
        ls = _run_sync(
            ["git", "ls-tree", git_ref, "--", "working"],
            cwd=self.snapshot_dir,
            timeout=5.0,
        )
        if ls.returncode != 0 or not ls.stdout.strip():
            return RestoreResult(ok=True)
        # 探测该检查点是否捕获了内部 git（无条目 = 旧检查点 / skipped / absent）
        reserved = f"working/{INNER_GIT_RESERVED_PATH}"
        probe = _run_sync(
            ["git", "ls-tree", git_ref, "--", reserved],
            cwd=self.snapshot_dir,
            timeout=5.0,
        )
        inner_git_captured = bool(probe.returncode == 0 and probe.stdout.strip())
        # git archive 输出 tar 流，从 working/ 剥前缀解到 dest_dir
        try:
            if not _extract_working_tree(self.snapshot_dir, git_ref, dest_dir):
                return RestoreResult(ok=False)
            if inner_git_captured:
                # 保留目录改回 .git（rm 防残留；失败降级 False，见 _restore_inner_git）
                inner_git_captured = _restore_inner_git(dest_dir)
            return RestoreResult(ok=True, inner_git_captured=inner_git_captured)
        except OSError as exc:
            logger.warning("snapshot restore failed: %s", exc)
            return RestoreResult(ok=False)

    def list_files(self, git_ref: str, subpath: str = "") -> list[dict[str, Any]]:
        """列出某检查点 git_ref 下 ``working/<subpath>`` 的直接子项。

        供前端在重放前预览检查点时刻的文件态（决定是否 / 从哪重放）。
        返回 ``[{"name", "path", "is_dir"}]``（目录在前）。
        """
        if not self._ensure_repo() or not git_ref:
            return []
        sub = subpath.strip("/").lstrip("./")
        # git ls-tree <ref>:working/<sub> 列该 tree 的直接子项（名字相对该 tree）
        tree_ish = f"{git_ref}:working" if not sub else f"{git_ref}:working/{sub}"
        r = _run_sync(
            ["git", "ls-tree", tree_ish],
            cwd=self.snapshot_dir,
            timeout=5.0,
        )
        if r.returncode != 0:
            logger.warning("snapshot list_files: %s", r.stderr.strip()[:200])
            return []
        entries: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            if "\t" not in line:
                continue
            meta, name = line.split("\t", 1)
            meta_parts = meta.split()
            if len(meta_parts) < 2:
                continue
            obj_type = meta_parts[1]
            if not name:
                continue
            # 预览层隐藏内部 git 保留目录（存储 / 恢复不受影响）
            if not sub and name == INNER_GIT_RESERVED_PATH:
                continue
            entries.append(
                {
                    "name": name,
                    "path": f"{sub}/{name}".strip("/") if sub else name,
                    "is_dir": obj_type == "tree",
                }
            )
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return entries

    def read_file(self, git_ref: str, path: str) -> dict[str, Any] | None:
        """读某检查点 git_ref 下 ``working/<path>`` 的文件内容。

        返回 ``{"name", "path", "size", "content", "truncated"}``；文件过大
        （>512KB）或为空路径返回 ``None``。供前端预览检查点文件内容。
        """
        if not self._ensure_repo() or not git_ref:
            return None
        rel = path.strip("/").lstrip("./")
        if not rel:
            return None
        obj = f"working/{rel}"
        # 先查大小，过大则不读全量
        sr = _run_sync(
            ["git", "cat-file", "-s", f"{git_ref}:{obj}"],
            cwd=self.snapshot_dir,
            timeout=5.0,
        )
        size = 0
        if sr.returncode == 0:
            try:
                size = int(sr.stdout.strip())
            except ValueError:
                size = 0
        if size > 512 * 1024:
            return {
                "name": rel.rsplit("/", 1)[-1],
                "path": rel,
                "size": size,
                "content": "(file too large to preview)",
                "truncated": True,
            }
        r = _run_sync(
            ["git", "show", f"{git_ref}:{obj}"],
            cwd=self.snapshot_dir,
            timeout=5.0,
        )
        if r.returncode != 0:
            return None
        return {
            "name": rel.rsplit("/", 1)[-1],
            "path": rel,
            "size": size,
            "content": r.stdout,
            "truncated": False,
        }

    # ── 恢复 ──────────────────────────────────────────────────────────

    def purge_all(self) -> None:
        """删除整个快照仓目录（项目卸载/手动清理时用）。"""
        try:
            if self.snapshot_dir.exists():
                shutil.rmtree(self.snapshot_dir)
            self._initialized = False
        except OSError:
            logger.exception("snapshot: purge_all failed")

    # ── 检查点文件态预览（不 restore，直接 git ls-tree / git show） ─────

    def _ensure_repo(self) -> bool:
        """幂等初始化快照仓。git 不可用返回 False。"""
        if self._initialized:
            return True
        if not _git_available():
            logger.warning("snapshot: git not available — file-state snapshots disabled")
            return False
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            self.working_dir.mkdir(parents=True, exist_ok=True)
            git_dir = self.snapshot_dir / ".git"
            if not git_dir.exists():
                r = _run_sync(["git", "init"], cwd=self.snapshot_dir)
                if r.returncode != 0:
                    logger.warning("snapshot: git init failed: %s", r.stderr.strip())
                    return False
            # Author identity comes from _GIT_ENV (passed to every git call), so
            # no `git config user.*` needed here.
            self._initialized = True
            return True
        except Exception:
            logger.exception("snapshot: ensure_repo failed")
            return False

    async def _snapshot_locked(
        self,
        checkpoint_id: str,
        payload: dict[str, Any],
    ) -> SnapshotResult:
        """持锁执行 _do_snapshot；超时 / 异常统一转成对应状态的 SnapshotResult。"""
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._do_snapshot(**payload),
                    timeout=self.snapshot_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "snapshot: timeout (>%.1fs) for checkpoint %s — skipped",
                    self.snapshot_timeout,
                    checkpoint_id,
                )
                return SnapshotResult(
                    status="timeout",
                    checkpoint_id=checkpoint_id,
                    error="snapshot exceeded hard timeout",
                )
            except Exception as exc:  # best-effort 不抛
                logger.exception("snapshot: error for %s", checkpoint_id)
                return SnapshotResult(
                    status="error",
                    checkpoint_id=checkpoint_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def _mirror_state(self) -> tuple[int, int, str]:
        """镜像 project_dir → working/ + 内部 .git，返回 (file_count, byte_size, inner_git)。"""
        # 1. 镜像 project_dir → working/（返回文件统计）
        file_count, byte_size = await self._sync_mirror()
        # 1b. 镜像内部 .git → working/.workflow-controller-inner-git/（独立趟，不受 ignore 约束）
        if not self.snapshot_git_state:
            return file_count, byte_size, ""
        loop = asyncio.get_event_loop()
        inner_git = await loop.run_in_executor(
            None,
            mirror_inner_git,
            self.project_dir,
            self.working_dir,
            self.snapshot_git_state_max_mb,
        )
        return file_count, byte_size, inner_git

    async def _git_add_all(self, checkpoint_id: str) -> SnapshotResult | None:
        """``git add -A --force``；失败时返回构造好的 error 结果。

        快照仓是独立 repo，须收 rsync 已镜像的全部文件，不受项目 .gitignore
        影响（否则产物 / .opencode/agents 等漏进快照，restore 时 fork cwd
        缺文件 / 缺 subagent 定义）。rsync 已按 engine ignore_patterns 过滤。
        """
        rc, _out, err = await _run_async(
            ["git", "add", "-A", "--force"],
            cwd=self.snapshot_dir,
        )
        if rc != 0:
            logger.warning("snapshot: git add failed: %s", err.strip())
            return SnapshotResult(
                status="error",
                checkpoint_id=checkpoint_id,
                error=f"git add: {err.strip()}",
            )
        return None

    async def _git_commit_snapshot(
        self,
        msg: str,
        checkpoint_id: str,
    ) -> tuple[str, SnapshotResult | None]:
        """commit + rev-parse；返回 (git_ref, 失败时构造好的 error 结果)。"""
        # --allow-empty：即使文件未变，每个检查点的 trailer checkpoint_id 不同 →
        # 独立 commit，便于 git log --grep 定位。
        rc, _out, err = await _run_async(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=self.snapshot_dir,
        )
        if rc != 0:
            logger.warning("snapshot: git commit failed: %s", err.strip())
            return "", SnapshotResult(
                status="error",
                checkpoint_id=checkpoint_id,
                error=f"git commit: {err.strip()}",
            )
        # 取 HEAD sha
        rc, out, err = await _run_async(
            ["git", "rev-parse", "HEAD"],
            cwd=self.snapshot_dir,
        )
        if rc != 0 or not out.strip():
            return "", SnapshotResult(
                status="error",
                checkpoint_id=checkpoint_id,
                error=f"rev-parse: {err.strip()}",
            )
        return out.strip(), None

    async def _do_snapshot(
        self,
        *,
        session_id: str,
        checkpoint_id: str,
        agent_id: str,
        agent_type: str,
        iteration: int,
        trigger: str,
        event_rowid: int | None,
        parent_checkpoint_id: str | None,
        main_part_id: str | None,
    ) -> SnapshotResult:
        file_count, byte_size, inner_git = await self._mirror_state()
        add_fail = await self._git_add_all(checkpoint_id)
        if add_fail is not None:
            return add_fail
        # 构造 commit message（结构化 trailer）
        msg = build_snapshot_commit_message(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            agent_id=agent_id,
            agent_type=agent_type,
            iteration=iteration,
            trigger=trigger,
            event_rowid=event_rowid,
            parent_checkpoint_id=parent_checkpoint_id,
            main_part_id=main_part_id,
        )
        git_ref, commit_fail = await self._git_commit_snapshot(msg, checkpoint_id)
        if commit_fail is not None:
            return commit_fail

        logger.info(
            "snapshot: %s ok (ref=%s, files=%d, %d bytes, inner_git=%s)",
            checkpoint_id,
            git_ref[:8],
            file_count,
            byte_size,
            inner_git or "-",
        )
        return SnapshotResult(
            status="ok",
            git_ref=git_ref,
            file_count=file_count,
            byte_size=byte_size,
            checkpoint_id=checkpoint_id,
            inner_git=inner_git,
        )

    # ── 清理 ──────────────────────────────────────────────────────────

    async def _sync_mirror(self) -> tuple[int, int]:
        """镜像 project_dir → working/。先 rsync，不可用降级 Python。"""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            _rsync_mirror,
            self.project_dir,
            self.working_dir,
            self.ignore_patterns,
        )
        if result is not None:
            return result
        return await loop.run_in_executor(
            None,
            _python_sync_mirror,
            self.project_dir,
            self.working_dir,
            self.ignore_patterns,
        )
