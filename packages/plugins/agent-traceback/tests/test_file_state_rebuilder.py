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

"""``file_state_rebuilder.py`` 单元测试 — write/edit/bash 重放 + git 历史 + 快照仓。

Hermetic：每个测试用 tmp_path 造一个最小 timeline，跑
FileStateRebuilder.replay，校验产出的文件态 + 快照仓 commit。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from workflow_controller.plugins.agent_traceback.session_reconstructor.db_reader import (
    TimelineEntry,
)
from workflow_controller.plugins.agent_traceback.session_reconstructor.file_state_rebuilder import (
    FileStateRebuilder,
    _classify_bash_command,
    _is_git_subcmd,
    _parse_git_author_from_result,
)

from ._fixtures import (
    bash_timeline_entry as _make_bash_entry,
)
from ._fixtures import (
    dialect_tool_timeline_entry as _make_raw_tool_entry,
)
from ._fixtures import (
    edit_timeline_entry as _make_edit_entry,
)
from ._fixtures import (
    multi_edit_timeline_entry as _make_multi_edit_entry,
)
from ._fixtures import (
    task_dispatch_timeline_entry,
)
from ._fixtures import (
    write_timeline_entry as _make_write_entry,
)


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not installed")


def _make_task_entry(
    ts: int, sub_sid: str = "ses_sub_001", call_id: str = "call_main_task_1"
) -> TimelineEntry:
    """rebuilder 场景的 task 派发条目（resultJson 带 task_result 尾巴）。"""
    return task_dispatch_timeline_entry(ts, sub_sid, call_id, with_task_result=True)


def _rebuilder_under(tmp_path: Path, **kw) -> tuple[Path, FileStateRebuilder]:
    """重放测试的固定开场：建 target 目录 + 按参数构造 rebuilder。"""
    target = tmp_path / "target"
    target.mkdir()
    return target, FileStateRebuilder(target_cwd=target, **kw)


# ── 命令分类 ────────────────────────────────────────────────────────


class TestClassifyBash:
    # ── mutating git → git（保 GIT_AUTHOR_DATE 注入）──
    @staticmethod
    def test_git_init():
        assert _classify_bash_command("git init") == "git"
        assert _is_git_subcmd("git init")

    @staticmethod
    def test_git_add_commit():
        assert _classify_bash_command("git add -A && git commit -m 'x'") == "git"

    @staticmethod
    def test_git_checkout_branch():
        assert _classify_bash_command("git checkout main") == "git"

    @staticmethod
    def test_cd_and_git():
        # 复合命令：cd && git add → 含 git 子命令 → git
        assert _classify_bash_command("cd /workspace/orig && git add .") == "git"

    # ── 双态 git 子命令（config/branch/reflog）→ git，不进只读白名单 ──
    @staticmethod
    def test_git_config_write_is_git():
        # 写 .git/config → 归 git 重放
        assert _classify_bash_command('git config user.name "Alice"') == "git"
        assert _classify_bash_command("git config user.email a@b.com") == "git"
        assert _classify_bash_command("git config core.bare false") == "git"

    @staticmethod
    def test_git_config_global_is_git():
        assert _classify_bash_command('git config --global user.name "Alice"') == "git"

    @staticmethod
    def test_git_config_read_form_still_git():
        # 读形式也归 git（不区分读写形式）
        assert _classify_bash_command("git config user.name") == "git"
        assert _classify_bash_command("git config --list") == "git"
        assert _classify_bash_command("git config --get user.name") == "git"

    @staticmethod
    def test_git_branch_write_is_git():
        # 写 refs/heads → 归 git 重放
        assert _classify_bash_command("git branch dev") == "git"
        assert _classify_bash_command("git branch -d old") == "git"
        assert _classify_bash_command("git branch -m old new") == "git"

    @staticmethod
    def test_git_branch_read_form_still_git():
        # 读形式也归 git
        assert _classify_bash_command("git branch") == "git"
        assert _classify_bash_command("git branch --list") == "git"

    @staticmethod
    def test_git_reflog_write_is_git():
        # 写 logs/refs → 归 git 重放
        assert _classify_bash_command("git reflog expire --all") == "git"
        assert _classify_bash_command("git reflog delete HEAD@{0}") == "git"

    @staticmethod
    def test_git_reflog_read_form_still_git():
        assert _classify_bash_command("git reflog show HEAD") == "git"
        assert _classify_bash_command("git reflog") == "git"

    # ── 只读 git 子命令 → read_only_probe（跳过）──
    @staticmethod
    def test_git_readonly_subcmds_skipped():
        for cmd in (
            "git status",
            "git diff",
            "git log",
            "git show HEAD",
            "git rev-parse HEAD",
            "git ls-files",
            "git ls-remote",
            "git describe",
            "git shortlog",
            "git blame file.txt",
            "git cat-file -p HEAD",
            "git name-rev HEAD",
            "git for-each-ref",
        ):
            assert _classify_bash_command(cmd) == "read_only_probe", cmd

    @staticmethod
    def test_git_readonly_mixed_with_mutating_is_git():
        # 含 mutating git 子命令 → git
        assert _classify_bash_command("git status && git add .") == "git"

    # ── 网络类 git（clone/push/pull/fetch）→ mutating ──
    @staticmethod
    def test_git_clone_is_mutating():
        # 网络类 → mutating
        assert _classify_bash_command("git clone https://github.com/x/y") == "mutating"

    @staticmethod
    def test_git_push_is_mutating():
        assert _classify_bash_command("git push origin main") == "mutating"

    @staticmethod
    def test_git_pull_is_mutating():
        assert _classify_bash_command("git pull origin main") == "mutating"

    @staticmethod
    def test_git_fetch_is_mutating():
        assert _classify_bash_command("git fetch origin") == "mutating"

    # ── 只读探查类（非 git）→ read_only_probe（跳过）──
    @staticmethod
    def test_grep_read_only():
        assert _classify_bash_command("grep foo file.txt") == "read_only_probe"

    @staticmethod
    def test_ls_read_only():
        assert _classify_bash_command("ls -la") == "read_only_probe"

    @staticmethod
    def test_find_read_only():
        assert _classify_bash_command("find . -name '*.py'") == "read_only_probe"

    @staticmethod
    def test_cat_no_redirect_read_only():
        assert _classify_bash_command("cat file.txt") == "read_only_probe"

    @staticmethod
    def test_cat_redirect_is_mutating():
        # cat 有重定向 → mutating
        assert _classify_bash_command("cat in.txt > out.txt") == "mutating"

    @staticmethod
    def test_sed_no_i_read_only():
        assert _classify_bash_command("sed 's/foo/bar/' file.txt") == "read_only_probe"

    @staticmethod
    def test_sed_i_is_mutating():
        assert _classify_bash_command("sed -i 's/foo/bar/' file.txt") == "mutating"

    @staticmethod
    def test_awk_no_redirect_read_only():
        assert _classify_bash_command("awk '{print $1}' file.txt") == "read_only_probe"

    @staticmethod
    def test_awk_redirect_is_mutating():
        assert _classify_bash_command("awk '{print $1}' file.txt > out.txt") == "mutating"

    @staticmethod
    def test_double_bracket_read_only():
        assert _classify_bash_command("[[ -f file.txt ]]") == "read_only_probe"

    @staticmethod
    def test_command_v_read_only():
        assert _classify_bash_command("command -v git") == "read_only_probe"
        assert _classify_bash_command("command -V git") == "read_only_probe"

    @staticmethod
    def test_command_exec_is_mutating():
        # command 后跟其它（非 -v/-V）→ 执行 → mutating
        assert _classify_bash_command("command ls") == "mutating"

    @staticmethod
    def test_command_with_other_flag_is_mutating():
        # command -p / command -<其他字母> 仍属执行，不能误判只读
        assert _classify_bash_command("command -p ./build.sh") == "mutating"
        assert _classify_bash_command("command rm -rf /") == "mutating"

    # ── mutating 非 git → mutating（重放，超时即杀）──
    @staticmethod
    def test_mkdir_is_mutating():
        assert _classify_bash_command("mkdir -p foo/bar") == "mutating"

    @staticmethod
    def test_cp_is_mutating():
        assert _classify_bash_command("cp a.txt b.txt") == "mutating"

    @staticmethod
    def test_rm_is_mutating():
        assert _classify_bash_command("rm -rf build/") == "mutating"

    @staticmethod
    def test_make_is_mutating():
        assert _classify_bash_command("make all") == "mutating"

    @staticmethod
    def test_pytest_is_mutating():
        assert _classify_bash_command("pytest tests/") == "mutating"

    @staticmethod
    def test_build_sh_is_mutating():
        assert _classify_bash_command("./build.sh") == "mutating"
        assert (
            _classify_bash_command("cd operators && ./build.sh euclidean_norm_package")
            == "mutating"
        )

    @staticmethod
    def test_curl_is_mutating():
        assert _classify_bash_command("curl http://example.com") == "mutating"

    @staticmethod
    def test_python3_is_mutating():
        assert _classify_bash_command("python3 script.py") == "mutating"

    # ── 复合命令（&&/||/;/|）：全只读才跳过，任一段 mutating 则重放 ──
    @staticmethod
    def test_compound_readonly_git_then_mutating():
        # 只读 git 头 + mutating 非 git 尾 → 不能整条跳过
        assert _classify_bash_command("git status && make test") == "mutating"
        assert _classify_bash_command("git status && pytest") == "mutating"
        assert _classify_bash_command("git log && rm foo.txt") == "mutating"

    @staticmethod
    def test_compound_readonly_head_then_mutating_tail():
        # 只读首位 + mutating 次段（非 git）→ 重放
        assert _classify_bash_command("ls && rm -rf /") == "mutating"
        assert _classify_bash_command("echo x | tee out.txt") == "mutating"

    @staticmethod
    def test_compound_with_mutating_git_is_git():
        # 含 mutating git 段 → git（保 GIT_AUTHOR_DATE 注入）
        assert _classify_bash_command("git status && git add .") == "git"
        assert _classify_bash_command("git add . && git commit -m x") == "git"
        assert _classify_bash_command("cd /x && git add .") == "git"

    @staticmethod
    def test_compound_all_readonly_skipped():
        # 全只读复合 → 保留跳过优化
        assert _classify_bash_command("git status && git diff") == "read_only_probe"
        assert _classify_bash_command("grep foo f | head") == "read_only_probe"
        assert _classify_bash_command("ls && echo hi") == "read_only_probe"


# ── author 解析 ─────────────────────────────────────────────────────


class TestParseGitAuthor:
    @staticmethod
    def test_normal():
        result = (
            "[main abc1234] msg\n"
            " Author: Alice <alice@example.com>\n"
            " 1 file changed, 1 insertion(+)"
        )
        name, email = _parse_git_author_from_result(result)
        assert name == "Alice"
        assert email == "alice@example.com"

    @staticmethod
    def test_chinese_name():
        result = "[main abc] x\n Author: 张三 <zhang@example.com>"
        name, email = _parse_git_author_from_result(result)
        assert name == "张三"
        assert email == "zhang@example.com"

    @staticmethod
    def test_empty():
        assert _parse_git_author_from_result("") == ("", "")
        assert _parse_git_author_from_result("no author here") == ("", "")


# ── 重放主流程 ──────────────────────────────────────────────────────


class TestReplay:
    @staticmethod
    def test_bash_timeout_kills_pipe_holding_grandchild(tmp_path: Path):
        """后台循环孙进程持管道 + 主 shell 退出成 zombie：超时路径有界返回。

        旧实现超时后按 ``os.getpgid(proc.pid)`` 杀组——主进程已成 zombie，macOS
        上 getpgid 抛 ESRCH 被 suppress 吞掉，killpg 不执行，孙进程存活持有
        stdout 管道，排空 communicate() 无界等待把重放永久吊死。回归断言：有限
        时间内返回 timeout，且不留存活的循环孙进程。
        """
        import threading
        import time

        _target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="", bash_timeout_seconds=1.0)
        # 主 shell 后台派发循环子进程后立即退出；循环持有 stdout 管道
        tc = _make_bash_entry(
            1,
            "for i in $(seq 1 60); do sleep 1; done & exit 0",
        ).tool_call

        result: dict = {}

        def _run() -> None:
            result["status"] = getattr(rebuilder, "_apply_bash")(tc)

        th = threading.Thread(target=_run, daemon=True)
        t0 = time.monotonic()
        th.start()
        th.join(timeout=15)
        assert result.get("status") == "timeout", (
            f"replay should return timeout, got {result.get('status')!r} (hung={th.is_alive()})"
        )
        assert not th.is_alive(), "replay hung on pipe-holding grandchild"
        assert time.monotonic() - t0 < 15

    @staticmethod
    def test_task_boundary_only_for_main_dispatch(tmp_path: Path):
        """检查点只在主 agent 派发边界打：孙 agent 的嵌套派发条目
        （role=subagent 的 task tool_call）跳过——其派发 part 在子会话，
        主 session 无重放锚，检查点不可重放。
        """
        _target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="")
        main_dispatch = _make_task_entry(1, "ses_sub_001", "call_main_1")
        # 孙派发条目：role=subagent，subagent_session_id = 派发者（子会话）
        grandson_tc = _make_task_entry(2, "ses_sub_002", "call_sub_1").tool_call
        grandson_dispatch = TimelineEntry(
            ts=2,
            role="subagent",
            kind="tool_call",
            turn=None,
            tool_call=grandson_tc,
            subagent_session_id="ses_sub_001",
            parent_task_call_id="call_main_1",
        )
        file_states = rebuilder.replay([main_dispatch, grandson_dispatch])

        starts = [fs for fs in file_states if fs.trigger == "subagent_start"]
        assert len(starts) == 1, "孙 agent 派发边界不得产生检查点"
        assert starts[0].subagent_session_id == "ses_sub_001"
        # 终态检查点仍在
        assert sum(1 for fs in file_states if fs.trigger == "session_complete") == 1

    @staticmethod
    def test_write_replays_byte_exact(tmp_path: Path):
        """write 重放逐字节一致（§4.2.1）。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/sub/a.txt", "hello\n世界"),
        ]
        rebuilder.replay(tl)
        out = target / "sub" / "a.txt"
        assert out.is_file()
        assert out.read_bytes() == "hello\n世界".encode()

    @staticmethod
    def test_edit_replays_exact_replace(tmp_path: Path):
        """edit 重放：oldString 精确匹配后替换（§6.2）。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "foo bar baz"),
            _make_edit_entry(2, "/workspace/orig/f.txt", "bar", "BAR"),
        ]
        rebuilder.replay(tl)
        assert (target / "f.txt").read_text() == "foo BAR baz"

    @staticmethod
    def test_edit_oldstring_mismatch_skips(tmp_path: Path):
        """oldString 不匹配时 best-effort 跳过（§6.2 / §7.3）。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "AAA"),
            _make_edit_entry(2, "/workspace/orig/f.txt", "BBB", "CCC"),  # 不匹配
        ]
        rebuilder.replay(tl)
        # 文件未被 edit 改动
        assert (target / "f.txt").read_text() == "AAA"

    @staticmethod
    def test_multi_edit_replays_sequential_edits(tmp_path: Path):
        """multi_edit（MultiEdit 归一化）按序应用同文件多组替换。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "foo bar baz"),
            _make_multi_edit_entry(
                2,
                "/workspace/orig/f.txt",
                [
                    {"old_string": "foo", "new_string": "FOO"},
                    {"old_string": "baz", "new_string": "BAZ"},
                ],
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "f.txt").read_text() == "FOO bar BAZ"

    @staticmethod
    def test_multi_edit_mismatch_edit_skips_rest_continue(tmp_path: Path):
        """multi_edit 单条不匹配 best-effort 跳过，其余条继续应用。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "aaa bbb"),
            _make_multi_edit_entry(
                2,
                "/workspace/orig/f.txt",
                [
                    {"old_string": "XXX", "new_string": "nope"},  # 不匹配
                    {"old_string": "bbb", "new_string": "BBB"},
                ],
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "f.txt").read_text() == "aaa BBB"

    @staticmethod
    def test_multi_edit_default_replaces_first_only(tmp_path: Path):
        """replace_all 缺省（Claude 默认 false）：仅替换首处匹配。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "foo foo foo"),
            _make_multi_edit_entry(
                2,
                "/workspace/orig/f.txt",
                [
                    {"old_string": "foo", "new_string": "FOO"},
                ],
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "f.txt").read_text() == "FOO foo foo"

    @staticmethod
    def test_multi_edit_replace_all_true_replaces_all(tmp_path: Path):
        """单条 edit 带 replace_all=true：替换全部匹配。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_write_entry(1, "/workspace/orig/f.txt", "foo foo foo"),
            _make_multi_edit_entry(
                2,
                "/workspace/orig/f.txt",
                [
                    {"old_string": "foo", "new_string": "FOO", "replace_all": True},
                ],
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "f.txt").read_text() == "FOO FOO FOO"

    @staticmethod
    def test_cpx_snake_case_write_edit_replay(tmp_path: Path):
        """cpx 方言参数键（file_path/old_string/new_string）同样可重放。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            _make_raw_tool_entry(
                1,
                "write",
                {
                    "file_path": "/workspace/orig/s.txt",
                    "content": "one two three",
                },
                "cw",
            ),
            _make_raw_tool_entry(
                2,
                "edit",
                {
                    "file_path": "/workspace/orig/s.txt",
                    "old_string": "two",
                    "new_string": "TWO",
                },
                "ce",
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "s.txt").read_text() == "one TWO three"

    @staticmethod
    def test_path_rewrite_relative_path(tmp_path: Path):
        """相对路径按 target_cwd 解析。"""
        target, rebuilder = _rebuilder_under(tmp_path, origin_cwd="/workspace/orig")
        tl = [
            TimelineEntry(
                ts=1,
                role="subagent",
                kind="tool_call",
                turn=None,
                tool_call={
                    "id": "x",
                    "turnId": "x",
                    "toolCallId": "call_rel",
                    "toolName": "write",
                    "argsJson": json.dumps({"filePath": "rel/a.txt", "content": "rel"}),
                    "resultJson": "",
                    "state": "completed",
                    "startedAt": "2026-07-16T12:00:00.000Z",
                    "completedAt": "2026-07-16T12:00:00.100Z",
                },
                subagent_session_id="ses_sub",
                parent_task_call_id="call_t",
            ),
        ]
        rebuilder.replay(tl)
        assert (target / "rel" / "a.txt").read_text() == "rel"

    @staticmethod
    def test_bash_mkdir_file_op_replays(tmp_path: Path):
        """git_and_file_ops 模式：mkdir 重放（§4.2.2）。"""
        target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", bash_replay_mode="git_and_file_ops"
        )
        tl = [_make_bash_entry(1, "mkdir -p /workspace/orig/foo/bar")]
        rebuilder.replay(tl)
        assert (target / "foo" / "bar").is_dir()

    @staticmethod
    def test_bash_build_skipped_in_git_and_file_ops(tmp_path: Path):
        """build/test 类在 git_and_file_ops 模式跳过（不产生产物）。"""
        target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", bash_replay_mode="git_and_file_ops"
        )
        # make 命令应被跳过；不创建产物文件
        tl = [_make_bash_entry(1, "make all && touch /workspace/orig/build_marker.txt")]
        rebuilder.replay(tl)
        assert not (target / "build_marker.txt").exists()

    @staticmethod
    def test_bash_all_mode_attempts(tmp_path: Path):
        """all 模式：build 类 best-effort 尝试（touch 命令会成功）。"""
        target, rebuilder = _rebuilder_under(
            tmp_path,
            origin_cwd="/workspace/orig",
            bash_replay_mode="all",
            bash_timeout_seconds=10.0,
        )
        # 在 all 模式下，touch 是 build_test_network（不在 file_op 集合），但会尝试执行
        tl = [_make_bash_entry(1, "touch /workspace/orig/marker.txt")]
        rebuilder.replay(tl)
        assert (target / "marker.txt").exists()

    @staticmethod
    def test_git_commands_replay_regardless_of_mode(tmp_path: Path):
        """git 命令始终重放（§4.2.5）——即便 bash_replay_mode=git_and_file_ops。"""
        target, rebuilder = _rebuilder_under(
            tmp_path,
            origin_cwd="/workspace/orig",
            bash_replay_mode="git_and_file_ops",
            git_hash_fidelity=False,
        )  # 简化：不测 hash 保真
        # git init + git add + git commit 应该都能跑
        tl = [
            _make_bash_entry(1, "git init", result="Initialized empty Git repository"),
            _make_write_entry(2, "/workspace/orig/f.txt", "content"),
            _make_bash_entry(3, "git add -A", result=""),
            _make_bash_entry(
                4, 'git commit -m "first"', result="[main abc1234] first\n Author: Alice <a@b.com>"
            ),
        ]
        rebuilder.replay(tl)
        # 验证项目 .git 存在且有 commit
        assert (target / ".git").is_dir()
        r = subprocess_run(["git", "log", "--oneline"], cwd=target)
        assert r.returncode == 0
        assert "first" in r.stdout

    @staticmethod
    def test_snapshot_at_task_boundary(tmp_path: Path):
        """task 边界打快照仓 commit（§4.2.4）——产出 git_ref。"""
        _target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", git_hash_fidelity=False
        )
        tl = [
            _make_write_entry(1, "/workspace/orig/a.txt", "first"),
            _make_task_entry(2, sub_sid="ses_sub_001", call_id="call_t1"),
            _make_write_entry(3, "/workspace/orig/b.txt", "second"),
            _make_task_entry(4, sub_sid="ses_sub_002", call_id="call_t2"),
        ]
        cps = rebuilder.replay(tl)
        # 2 个 task 边界 + 1 个 session_complete = 3 检查点
        assert len(cps) == 3
        # 前两个是 subagent_start，最后是 session_complete
        assert cps[0].trigger == "subagent_start"
        assert cps[0].subagent_session_id == "ses_sub_001"
        assert cps[0].parent_task_call_id == "call_t1"
        assert cps[0].git_ref  # 有 sha
        assert cps[1].subagent_session_id == "ses_sub_002"
        assert cps[2].trigger == "session_complete"
        assert cps[2].subagent_session_id == ""

    @staticmethod
    def test_snapshot_excludes_project_git(tmp_path: Path):
        """快照仓镜像排除项目 .git（§6.3）——与实时 traceback 一致。"""
        target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", git_hash_fidelity=False
        )
        tl = [
            _make_bash_entry(1, "git init"),
            _make_write_entry(2, "/workspace/orig/a.txt", "x"),
            _make_task_entry(3),
        ]
        rebuilder.replay(tl)
        # 项目 .git 存在
        assert (target / ".git").is_dir()
        # 快照仓 working/ 不应含 .git（被 DEFAULT_IGNORE_PATTERNS 排除）
        assert not (rebuilder.working_dir / ".git").exists()
        # 但应含 a.txt
        assert (rebuilder.working_dir / "a.txt").exists()

    @staticmethod
    def test_restore_via_snapshot_engine(tmp_path: Path):
        """快照仓 commit 可被 SnapshotEngine.restore 抽出文件态（设计 §3 / §4.6 复用）。"""
        from workflow_controller.plugins.agent_traceback.snapshot_engine import SnapshotEngine

        target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", git_hash_fidelity=False
        )
        tl = [
            _make_write_entry(1, "/workspace/orig/a.txt", "v1"),
            _make_task_entry(2, sub_sid="ses_sub_001"),
            _make_write_entry(3, "/workspace/orig/a.txt", "v2"),
            _make_task_entry(4, sub_sid="ses_sub_002"),
        ]
        cps = rebuilder.replay(tl)
        # 用 SnapshotEngine.restore 从第一个检查点的 git_ref 抽文件态
        engine = SnapshotEngine(
            project_dir=target,
            snapshot_dir=target / ".workflow-controller" / "snapshots",
        )
        # 第一个检查点时刻 a.txt = "v1"
        dest1 = tmp_path / "fork1"
        dest1.mkdir()
        ok = engine.restore(cps[0].git_ref, dest1).ok
        assert ok
        assert (dest1 / "a.txt").read_text() == "v1"
        # 第二个检查点时刻 a.txt = "v2"
        dest2 = tmp_path / "fork2"
        dest2.mkdir()
        ok = engine.restore(cps[1].git_ref, dest2).ok
        assert ok
        assert (dest2 / "a.txt").read_text() == "v2"

    @staticmethod
    def test_inner_git_captured_at_boundaries_with_advancing_head(tmp_path: Path):
        """重放含 git 命令的 timeline → 边界检查点树含对应时刻的 .workflow-controller-inner-git，
        且 HEAD 随边界推进（每边界定格该边界的历史时刻）。
        """
        from workflow_controller.plugins.agent_traceback.snapshot_engine import (
            INNER_GIT_RESERVED_PATH,
            SnapshotEngine,
            _run_sync,
        )

        target, rebuilder = _rebuilder_under(
            tmp_path, origin_cwd="/workspace/orig", git_hash_fidelity=False
        )
        tl = [
            _make_bash_entry(1, "git init"),
            _make_write_entry(2, "/workspace/orig/a.txt", "v1"),
            _make_bash_entry(3, "git add -A"),
            _make_bash_entry(4, 'git commit -m "c1"'),
            _make_task_entry(5, sub_sid="ses_sub_001"),
            _make_write_entry(6, "/workspace/orig/a.txt", "v2"),
            _make_bash_entry(7, "git add -A"),
            _make_bash_entry(8, 'git commit -m "c2"'),
            _make_task_entry(9, sub_sid="ses_sub_002"),
        ]
        cps = rebuilder.replay(tl)
        # 边界 1/2 都是 subagent_start，均捕获了内部 git
        assert cps[0].inner_git == "captured"
        assert cps[1].inner_git == "captured"

        def _reserved_head(ref: str) -> str:
            r = _run_sync(
                ["git", "show", f"{ref}:working/{INNER_GIT_RESERVED_PATH}/HEAD"],
                cwd=rebuilder.snapshot_dir,
            )
            assert r.returncode == 0, r.stderr
            return r.stdout.strip()

        def _peel(ref: str) -> str:
            # HEAD 是 ref 引用（如 ref: refs/heads/main）→ 解引用到 sha
            head = _reserved_head(ref)
            if head.startswith("ref: "):
                ref_name = head[slice(len("ref: "), None)]
                r = _run_sync(
                    ["git", "show", f"{ref}:working/{INNER_GIT_RESERVED_PATH}/{ref_name}"],
                    cwd=rebuilder.snapshot_dir,
                )
                assert r.returncode == 0, r.stderr
                return r.stdout.strip()
            return head

        head1 = _peel(cps[0].git_ref)
        head2 = _peel(cps[1].git_ref)
        # c2 只发生在边界 1 之后 → 两个边界的 HEAD 不同且随边界推进
        assert head1 != head2
        log1 = subprocess_run(
            ["git", "log", "--format=%s", head1],
            cwd=target,
        ).stdout
        assert "c1" in log1 and "c2" not in log1
        log2 = subprocess_run(
            ["git", "log", "--format=%s", head2],
            cwd=target,
        ).stdout
        assert "c2" in log2

        # restore 边界 1 → fork 拿到边界 1 时刻的 .git（不含 c2）
        engine = SnapshotEngine(
            project_dir=target,
            snapshot_dir=target / ".workflow-controller" / "snapshots",
        )
        dest = tmp_path / "fork1"
        dest.mkdir()
        res = engine.restore(cps[0].git_ref, dest)
        assert res.ok and res.inner_git_captured
        fork_log = subprocess_run(["git", "log", "--format=%s"], cwd=dest).stdout
        assert "c1" in fork_log and "c2" not in fork_log


def subprocess_run(cmd, cwd):
    """小 helper：跑子进程，避免重复 import subprocess。"""
    import subprocess

    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


class TestRewriteCommandPaths:
    @staticmethod
    def test_empty_origin_leaves_command_untouched():
        """origin_cwd 为空时不做前缀替换：空串正则零宽匹配会把 target
        路径插进命令几乎每个非词边界位置（cpx 导出 directory 常为空）。
        """
        rebuilder = FileStateRebuilder(
            target_cwd=Path("/target"),
            origin_cwd="",
        )
        cmd = "cd /home/u/proj && ls src/"
        assert getattr(rebuilder, "_rewrite_command_paths")(cmd) == cmd

    @staticmethod
    def test_origin_prefix_rewrites_to_target():
        rebuilder = FileStateRebuilder(
            target_cwd=Path("/target"),
            origin_cwd="/home/u/proj",
        )
        assert (
            getattr(rebuilder, "_rewrite_command_paths")("cat /home/u/proj/src/a.py")
            == "cat /target/src/a.py"
        )
