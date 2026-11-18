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

"""sync_dual_repo.py 黑盒测试：临时旧仓与目标仓演练覆盖检查、同步、提交与阻断路径。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_dual_repo.py"
if not SCRIPT.is_file():
    # 同步工具住旧仓，不随内核包同步；其余仓跳过本组黑盒测试
    pytest.skip("sync_dual_repo.py 不在本仓", allow_module_level=True)
REPO_ROOT = SCRIPT.parents[1]
GIT_EXECUTABLE = shutil.which("git")
assert GIT_EXECUTABLE is not None, "git 必须可用于构造临时仓库"

MAPPING_TEXT = """
dir  src/app/core             axcor packages/kernel/src/app/core
file src/app/__main__.py      axcor packages/kernel/src/app/__main__.py
dir  src/app/plugins/demo     axcor packages/plugins/demo/src/app/plugins/demo
dir  src/app/web              opsbot src/app/web  exclude=/__init__.py
file tests/__init__.py        axcor tests/__init__.py
dir  tests/integration        axcor packages/kernel/tests/integration
dir  tests/core/integration   axcor packages/kernel/tests/integration
dir  tests/core/unit          axcor packages/kernel/tests/unit
dir  tests/web                opsbot tests/web
exclude-file .gitignore
exclude-file src/app/__init__.py
exclude-file src/app/plugins/__init__.py
exclude-file pyproject.toml
exclude-file README.md
exclude-dir docs
dependency-file pyproject.toml
"""

OLD_FILES = {
    ".gitignore": "__pycache__/\n",
    "pyproject.toml": "deps",
    "README.md": "readme",
    "docs/notes.md": "notes",
    "src/app/__init__.py": "ns",
    "src/app/__main__.py": "main",
    "src/app/core/a.py": "a",
    "src/app/plugins/__init__.py": "ns",
    "src/app/plugins/demo/a.py": "demo",
    "src/app/web/app.py": "web",
    "src/app/web/__init__.py": "ns",
    "tests/__init__.py": "tests",
    "tests/integration/t1.py": "t1",
    "tests/core/integration/t2.py": "t2",
    "tests/core/unit/t3.py": "t3",
    "tests/web/tw.py": "tw",
}


def git(repo: Path, *args: str) -> str:
    """在指定仓执行 git 命令并返回 stdout。"""
    return subprocess.run(
        [GIT_EXECUTABLE, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def write_files(repo: Path, files: dict[str, str]) -> None:
    """按相对路径批量写文件。"""
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def make_repo(path: Path) -> Path:
    """初始化带测试身份的空 git 仓。"""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "tester")
    git(path, "config", "user.email", "tester@example.com")
    return path


def commit(repo: Path, message: str) -> str:
    """暂存全部改动并提交，返回新提交 SHA。"""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def build_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """构造旧仓 + axcor/opsbot 空仓 + 映射表的完整同步夹具。"""
    old = make_repo(tmp_path / "old")
    write_files(old, OLD_FILES)
    (old / "src/app/core/__pycache__").mkdir()
    (old / "src/app/core/__pycache__/a.pyc").write_text("junk", encoding="utf-8")
    commit(old, "init")
    axcor = make_repo(tmp_path / "axcor")
    opsbot = make_repo(tmp_path / "opsbot")
    mapping = tmp_path / "mapping.txt"
    mapping.write_text(MAPPING_TEXT.lstrip(), encoding="utf-8")
    return old, axcor, opsbot, mapping


def run_sync(
    old: Path, mapping: Path, *extra: str, axcor: Path | None = None, opsbot: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """以旧仓为工作目录执行同步脚本。"""
    command = [sys.executable, str(SCRIPT), "--mapping", str(mapping)]
    if axcor is not None:
        command += ["--axcor", str(axcor)]
    if opsbot is not None:
        command += ["--opsbot", str(opsbot)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [*command, *extra], cwd=old, capture_output=True, text=True, encoding="utf-8", env=env
    )


def test_real_mapping_covers_repo() -> None:
    """真实映射表必须覆盖真实仓的全部跟踪文件。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "映射覆盖检查通过" in result.stdout


def test_unmapped_tracked_file_blocks(tmp_path: Path) -> None:
    old, axcor, _, mapping = build_fixture(tmp_path)
    write_files(old, {"surprise.txt": "?"})
    commit(old, "add surprise")
    result = run_sync(old, mapping, axcor=axcor)
    assert result.returncode == 1
    assert "surprise.txt" in result.stderr
    assert "未映射且未显式排除" in result.stderr


def test_stale_mapping_source_rejected(tmp_path: Path) -> None:
    old, _, _, mapping = build_fixture(tmp_path)
    mapping.write_text(mapping.read_text(encoding="utf-8") + "dir  no/such/dir  axcor x\n")
    result = run_sync(old, mapping)
    assert result.returncode == 2
    assert "no/such/dir" in result.stderr


def test_sync_lands_content_and_records_source(tmp_path: Path) -> None:
    old, axcor, opsbot, mapping = build_fixture(tmp_path)
    result = run_sync(old, mapping, axcor=axcor, opsbot=opsbot)
    assert result.returncode == 0, result.stderr
    source_sha = git(old, "rev-parse", "HEAD").strip()

    assert (axcor / "packages/kernel/src/app/core/a.py").is_file()
    assert (axcor / "packages/kernel/src/app/__main__.py").is_file()
    assert (axcor / "packages/plugins/demo/src/app/plugins/demo/a.py").is_file()
    assert (axcor / "tests/__init__.py").is_file()
    assert (axcor / "packages/kernel/tests/integration/t1.py").is_file()
    assert (axcor / "packages/kernel/tests/integration/t2.py").is_file()
    assert (axcor / "packages/kernel/tests/unit/t3.py").is_file()
    assert not (axcor / "packages/kernel/src/app/core/__pycache__").exists()
    assert (opsbot / "src/app/web/app.py").is_file()
    assert not (opsbot / "src/app/web/__init__.py").exists()
    assert not (opsbot / "README.md").exists()

    for target in (axcor, opsbot):
        message = git(target, "log", "-1", "--format=%B")
        assert f"Source-SHA: {source_sha}" in message

    again = run_sync(old, mapping, axcor=axcor, opsbot=opsbot)
    assert again.returncode == 0, again.stderr
    assert "内容无变化" in again.stdout
    assert git(axcor, "rev-list", "--count", "HEAD").strip() == "1"


def test_deletion_propagates(tmp_path: Path) -> None:
    old, axcor, opsbot, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor, opsbot=opsbot).returncode == 0
    (old / "src/app/core/a.py").unlink()
    (old / "src/app/__main__.py").unlink()
    commit(old, "remove files")
    result = run_sync(old, mapping, axcor=axcor, opsbot=opsbot)
    assert result.returncode == 0, result.stderr
    assert not (axcor / "packages/kernel/src/app/core/a.py").exists()
    assert not (axcor / "packages/kernel/src/app/__main__.py").exists()
    assert "目标文件源已消失" in result.stdout


def test_whole_dir_source_deletion_commits(tmp_path: Path) -> None:
    """整目录映射源消失时，目标删除必须进入同步提交，不得残留为下次同步的阻断。"""
    old, axcor, _, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    git(old, "rm", "-q", "-r", "src/app/plugins/demo")
    commit(old, "remove demo plugin dir")
    result = run_sync(old, mapping, axcor=axcor)
    assert result.returncode == 0, result.stderr
    assert not (axcor / "packages/plugins/demo/src/app/plugins/demo").exists()
    assert git(axcor, "status", "--porcelain").strip() == ""
    changed = git(axcor, "show", "--name-only", "--format=", "HEAD").split()
    assert any(path.startswith("packages/plugins/demo/") for path in changed)
    again = run_sync(old, mapping, axcor=axcor)
    assert again.returncode == 0, again.stderr


def test_merge_group_unions_and_prunes(tmp_path: Path) -> None:
    old, axcor, _, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    merged = axcor / "packages/kernel/tests/integration"
    stray = merged / "stray.py"
    stray.write_text("stray", encoding="utf-8")
    git(axcor, "add", "-A")
    git(axcor, "commit", "-q", "-m", "stray")
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    assert not stray.exists()
    assert (merged / "t1.py").is_file() and (merged / "t2.py").is_file()


def test_merge_name_collision_rejected(tmp_path: Path) -> None:
    old, _, _, mapping = build_fixture(tmp_path)
    write_files(old, {"tests/integration/__init__.py": "one"})
    write_files(old, {"tests/core/integration/__init__.py": "two"})
    commit(old, "collide")
    result = run_sync(old, mapping)
    assert result.returncode == 2
    assert "同名文件内容不一致" in result.stderr


def test_dependency_change_halts_until_allowed(tmp_path: Path) -> None:
    old, axcor, opsbot, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor, opsbot=opsbot).returncode == 0
    write_files(old, {"pyproject.toml": "deps v2"})
    commit(old, "bump deps")
    halted = run_sync(old, mapping, axcor=axcor, opsbot=opsbot)
    assert halted.returncode == 1
    assert "pyproject.toml" in halted.stderr
    assert "--allow-dependency-change" in halted.stderr
    allowed = run_sync(old, mapping, "--allow-dependency-change", axcor=axcor, opsbot=opsbot)
    assert allowed.returncode == 0, allowed.stderr


def test_empty_dependency_list_lets_source_changes_through(tmp_path: Path) -> None:
    """映射表未登记依赖监控时，源码改动不得被误判为依赖变化而停。"""
    old, axcor, _, mapping = build_fixture(tmp_path)
    mapping.write_text(
        "\n".join(
            line
            for line in mapping.read_text(encoding="utf-8").splitlines()
            if not line.startswith("dependency-file")
        )
        + "\n"
    )
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    write_files(old, {"src/app/core/a.py": "a v2"})
    commit(old, "update core")
    result = run_sync(old, mapping, axcor=axcor)
    assert result.returncode == 0, result.stderr
    assert (axcor / "packages/kernel/src/app/core/a.py").read_text(encoding="utf-8") == "a v2"


def test_unrelated_staged_changes_stay_out_of_sync_commit(tmp_path: Path) -> None:
    old, axcor, _, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    manual = axcor / "manual"
    manual.mkdir()
    (manual / "note.txt").write_text("wip", encoding="utf-8")
    git(axcor, "add", "manual")

    write_files(old, {"src/app/core/a.py": "a v2"})
    commit(old, "update core")
    result = run_sync(old, mapping, axcor=axcor)
    assert result.returncode == 0, result.stderr
    changed = git(axcor, "show", "--name-only", "--format=", "HEAD").split()
    assert "packages/kernel/src/app/core/a.py" in changed
    assert not any(path.startswith("manual/") for path in changed)
    assert git(axcor, "diff", "--cached", "--name-only").strip() == "manual/note.txt"

    again = run_sync(old, mapping, axcor=axcor)
    assert again.returncode == 0, again.stderr
    assert "内容无变化" in again.stdout


def test_dry_run_leaves_targets_untouched(tmp_path: Path) -> None:
    old, axcor, opsbot, mapping = build_fixture(tmp_path)
    result = run_sync(old, mapping, "--dry-run", axcor=axcor, opsbot=opsbot)
    assert result.returncode == 0, result.stderr
    assert not (axcor / "packages").exists()
    assert not (opsbot / "src").exists()


def test_dirty_source_repo_blocked(tmp_path: Path) -> None:
    old, axcor, _, mapping = build_fixture(tmp_path)
    assert run_sync(old, mapping, axcor=axcor).returncode == 0
    write_files(old, {"src/app/core/a.py": "dirty"})
    result = run_sync(old, mapping, axcor=axcor)
    assert result.returncode == 1
    assert "未提交改动" in result.stderr


def test_source_rev_option_removed(tmp_path: Path) -> None:
    """内容始终取自旧仓 HEAD 工作树，不允许指定与内容不同源的来源修订。"""
    old, _, _, mapping = build_fixture(tmp_path)
    result = run_sync(old, mapping, "--source-rev", "HEAD~1")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert "--source-rev" in result.stderr
