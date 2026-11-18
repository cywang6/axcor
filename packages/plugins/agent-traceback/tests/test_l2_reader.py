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

"""opencode_session Phase 2: 主 agent 对话史读取 + 预制 session（设计 agent-traceback §4）。

Hermetic：每个测试用 tmp_path 造一个 opencode.db（session + part 表，真实 schema
子集），通过 XDG_DATA_HOME 指向，不碰真实 opencode.db。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from workflow_controller.plugins.agent_traceback.l2_reader import (
    find_exclude_message_id,
    find_main_part_id_for_subagent,
    read_main_session_messages,
    read_main_session_parts,
    seed_prebuilt_session,
)

from ._fixtures import OpencodePartRow
from ._fixtures import build_opencode_db as _make_opencode_db
from ._fixtures import (
    opencode_assistant_message_row as _asst_msg,
)
from ._fixtures import (
    opencode_task_part_row as _task_part,
)
from ._fixtures import (
    opencode_text_part_row as _text_part,
)
from ._fixtures import (
    opencode_user_message_row as _user_msg,
)

if TYPE_CHECKING:
    from pathlib import Path


def _main_db_env(tmp_path: Path, parts: list, messages: list | None = None) -> dict:
    """播种单主会话（ses_main，时间戳 1/1）的假 opencode.db，返回 XDG env。"""
    _make_opencode_db(
        tmp_path,
        sessions=[{"id": "ses_main", "time_created": 1, "time_updated": 1}],
        parts=parts,
        messages=messages,
    )
    return {"XDG_DATA_HOME": str(tmp_path)}


def _origin_db_env(
    tmp_path: Path,
    parts: list,
    messages: list | None = None,
    *,
    directory: str = "/proj",
    with_agent_model: bool = True,
) -> dict:
    """播种源会话（ses_orig，title=orig，目录可指定）的假 opencode.db，返回 XDG env。"""
    session: dict = {
        "id": "ses_orig",
        "title": "orig",
        "directory": directory,
        "time_created": 100,
        "time_updated": 100,
    }
    if with_agent_model:
        session["agent"] = "worker"
        session["model"] = "m"
    _make_opencode_db(tmp_path, sessions=[session], parts=parts, messages=messages)
    return {"XDG_DATA_HOME": str(tmp_path)}


def _seed_fork(
    tmp_path: Path,
    new_session_id: str,
    title: str,
    *,
    up_to_part_id: str | None = None,
    exclude_part_id: str | None = None,
    exclude_message_id: str | None = None,
    directory: str | None = None,
) -> bool:
    """对源会话 ses_orig 执行 seed_prebuilt_session（env 与 source_env 同源）。"""
    env = {"XDG_DATA_HOME": str(tmp_path)}
    return seed_prebuilt_session(
        source_session_id="ses_orig",
        new_session_id=new_session_id,
        up_to_part_id=up_to_part_id,
        title=title,
        env=env,
        source_env=env,
        exclude_part_id=exclude_part_id,
        exclude_message_id=exclude_message_id,
        directory=directory,
    )


def _seeded_part_rows(tmp_path: Path, session_id: str) -> list:
    """读回 seed 产物库里某会话的 part data 行（按 time_created 升序）。"""
    con = sqlite3.connect(tmp_path / "opencode" / "opencode.db")
    rows = con.execute(
        "SELECT data FROM part WHERE session_id = ? ORDER BY time_created ASC",
        (session_id,),
    ).fetchall()
    con.close()
    return rows


def _read_part_row(
    pid: str, sid: str, tc: int, file_path: str, *, with_metadata: bool
) -> OpencodePartRow:
    """read 工具 part 行（路径重写回归用：input/output/metadata 三处路径）。"""
    state: dict = {
        "status": "completed",
        "input": {"filePath": file_path},
        "output": f"<path>{file_path}</path>",
    }
    if with_metadata:
        state["metadata"] = {"filepath": file_path}
    return OpencodePartRow(
        pid,
        f"m_{pid}",
        sid,
        tc,
        tc,
        json.dumps({"type": "tool", "tool": "read", "callID": "c1", "state": state}),
    )


# ── read_main_session_parts / messages ──────────────────────────────────


class TestReadMainSession:
    @staticmethod
    def test_reads_parts_ordered_and_truncates(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "first"),
            _text_part("p2", "ses_main", 2, "second"),
            _text_part("p3", "ses_main", 3, "third (after checkpoint)"),
        ]
        _make_opencode_db(
            tmp_path,
            sessions=[
                {"id": "ses_main", "title": "main", "time_created": 1, "time_updated": 1},
            ],
            parts=parts,
        )
        env = {"XDG_DATA_HOME": str(tmp_path)}
        # Full stream
        all_parts = read_main_session_parts("ses_main", env)
        assert [p[0] for p in all_parts] == ["p1", "p2", "p3"]
        # Truncated at p2 (checkpoint moment) — p3 excluded
        trunc = read_main_session_parts("ses_main", env, up_to_part_id="p2")
        assert [p[0] for p in trunc] == ["p1", "p2"]

    @staticmethod
    def test_messages_normalize_text_and_tool(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "hello"),
            _task_part("p2", "ses_main", 2, "ses_sub_a", "result-A"),
        ]
        env = _main_db_env(tmp_path, parts)
        msgs = read_main_session_messages("ses_main", env)
        assert msgs[0] == {"type": "text", "text": "hello"}
        assert msgs[1]["type"] == "tool_use" and msgs[1]["name"] == "task"
        assert msgs[2]["type"] == "tool_result"
        assert "ses_sub_a" in msgs[2]["content"]

    @staticmethod
    def test_truncate_excludes_after_checkpoint(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "before"),
            _task_part("p2", "ses_main", 2, "ses_sub_x"),
            _text_part("p3", "ses_main", 3, "after"),
        ]
        env = _main_db_env(tmp_path, parts)
        msgs = read_main_session_messages("ses_main", env, up_to_part_id="p2")
        # p1 text + p2 tool_use + tool_result; p3 excluded
        assert len(msgs) == 3
        assert msgs[-1]["type"] == "tool_result"
        assert not any(m.get("text") == "after" for m in msgs)

    @staticmethod
    def test_exclude_part_id_excludes_task_part_and_after(tmp_path: Path):
        # re-call X (设计 §4.6): exclude X's task part so main agent re-dispatches X
        parts = [
            _text_part("p1", "ses_main", 1, "intro"),
            _task_part("p2", "ses_main", 2, "ses_sub_x", "X-out"),
            _text_part("p3", "ses_main", 3, "after X"),
            _task_part("p4", "ses_main", 4, "ses_sub_y", "Y-out"),
        ]
        env = _main_db_env(tmp_path, parts)
        # Exclude p2 (X's task part): only p1 returned, p2/p3/p4 excluded
        result = read_main_session_parts("ses_main", env, exclude_part_id="p2")
        assert [p[0] for p in result] == ["p1"]
        # messages too
        msgs = read_main_session_messages("ses_main", env, exclude_part_id="p2")
        assert len(msgs) == 1
        assert msgs[0] == {"type": "text", "text": "intro"}

    @staticmethod
    def test_exclude_part_id_first_part_yields_empty(tmp_path: Path):
        # Edge: X's task part is the very first part → no prior context
        parts = [
            _task_part("p1", "ses_main", 1, "ses_sub_x", "X-out"),
            _text_part("p2", "ses_main", 2, "after"),
        ]
        env = _main_db_env(tmp_path, parts)
        result = read_main_session_parts("ses_main", env, exclude_part_id="p1")
        assert result == []

    @staticmethod
    def test_exclude_part_id_none_behaves_like_original(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "a"),
            _task_part("p2", "ses_main", 2, "ses_sub_x"),
            _text_part("p3", "ses_main", 3, "b"),
        ]
        env = _main_db_env(tmp_path, parts)
        full = read_main_session_parts("ses_main", env)
        assert [p[0] for p in full] == ["p1", "p2", "p3"]

    @staticmethod
    def test_missing_returns_empty(tmp_path: Path):
        _make_opencode_db(tmp_path, sessions=[], parts=[])
        env = {"XDG_DATA_HOME": str(tmp_path)}
        assert read_main_session_parts("ses_zzz", env) == []
        assert read_main_session_messages("ses_zzz", env) == []

    @staticmethod
    def test_empty_id_returns_empty():
        assert read_main_session_parts("") == []
        assert read_main_session_messages("") == []

    @staticmethod
    def test_exclude_message_id_drops_whole_message_and_after(tmp_path: Path):
        # 透明重放·message 级截断（设计 §2.6）：opencode 一条 assistant 消息可含多 part
        # （reasoning + text + task），共享同一 message_id。exclude_message_id 命中即 break
        # —— 丢弃该 message 的全部 part（含「我将调用 X」前置 text/reasoning）及其之后，
        # 消除「孤儿文本」。
        #
        # 布局：p1(m_a) | p2+p3+p4 同属 m_b（reasoning + text + task）| p5(m_c)
        # exclude m_b → 只留 p1（p2/p3/p4/p5 全丢）。
        parts = [
            _text_part("p1", "ses_main", 1, "before"),  # m_p1
            # p2/p3/p4 share a custom message_id "m_b" (reasoning + text + task)
            (
                "p2",
                "m_b",
                "ses_main",
                2,
                2,
                json.dumps({"type": "reasoning", "text": "I'll call X"}),
            ),
            ("p3", "m_b", "ses_main", 3, 3, json.dumps({"type": "text", "text": "dispatching X"})),
            _task_part("p4", "ses_main", 4, "ses_sub_x"),  # m_b (task part)
            _text_part("p5", "ses_main", 5, "after"),  # m_p5
        ]
        env = _main_db_env(tmp_path, parts)
        # exclude_message_id="m_b" → first part of m_b (p2) triggers break → only p1 kept
        result = read_main_session_parts("ses_main", env, exclude_message_id="m_b")
        assert [p[0] for p in result] == ["p1"]
        # The reasoning/text/task parts of m_b are all dropped (no orphan text)
        msgs = read_main_session_messages("ses_main", env, exclude_message_id="m_b")
        assert len(msgs) == 1
        assert msgs[0] == {"type": "text", "text": "before"}

    @staticmethod
    def test_exclude_message_id_precedence_over_exclude_part_id(tmp_path: Path):
        # When both exclude_message_id and exclude_part_id given, message_id wins
        # (break happens at the first part of the excluded message, which is at or
        # before the exclude_part_id position).
        parts = [
            _text_part("p1", "ses_main", 1, "a"),  # m_p1
            ("p2", "m_b", "ses_main", 2, 2, json.dumps({"type": "text", "text": "pre"})),
            _task_part("p3", "ses_main", 3, "ses_sub_x"),  # m_b
            _text_part("p4", "ses_main", 4, "post"),  # m_p4
        ]
        env = _main_db_env(tmp_path, parts)
        # 仅按 p3 截断：保留 p1、p2（p3 及其后丢弃）
        r_part = read_main_session_parts("ses_main", env, exclude_part_id="p3")
        assert [p[0] for p in r_part] == ["p1", "p2"]
        # message 与 part 两个截断键同时给：message 优先，只留 p1
        r_msg = read_main_session_parts(
            "ses_main",
            env,
            exclude_message_id="m_b",
            exclude_part_id="p3",
        )
        assert [p[0] for p in r_msg] == ["p1"]

    @staticmethod
    def test_exclude_message_id_first_message_yields_empty(tmp_path: Path):
        # Edge: excluded message is the very first → empty stream
        parts = [
            ("p1", "m_a", "ses_main", 1, 1, json.dumps({"type": "text", "text": "first"})),
            ("p2", "m_a", "ses_main", 2, 2, json.dumps({"type": "text", "text": "second"})),
            _text_part("p3", "ses_main", 3, "after"),
        ]
        env = _main_db_env(tmp_path, parts)
        assert read_main_session_parts("ses_main", env, exclude_message_id="m_a") == []


# ── find_exclude_message_id ────────────────────────────────────────────


class TestFindExcludeMessageId:
    @staticmethod
    def test_finds_message_id_for_part(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "intro"),  # m_p1
            _task_part("p2", "ses_main", 2, "ses_sub_x"),  # m_p2
        ]
        env = _main_db_env(tmp_path, parts)
        assert find_exclude_message_id("ses_main", "p2", env) == "m_p2"

    @staticmethod
    def test_returns_none_for_missing_part(tmp_path: Path):
        _make_opencode_db(
            tmp_path,
            sessions=[
                {"id": "ses_main", "time_created": 1, "time_updated": 1},
            ],
            parts=[_text_part("p1", "ses_main", 1, "x")],
        )
        env = {"XDG_DATA_HOME": str(tmp_path)}
        assert find_exclude_message_id("ses_main", "p_zzz", env) is None

    @staticmethod
    def test_returns_none_for_empty_args():
        assert find_exclude_message_id("", "p1") is None
        assert find_exclude_message_id("ses", "") is None

    @staticmethod
    def test_returns_none_when_part_has_no_message_id(tmp_path: Path):
        # Old db schema: part.message_id NULL → can't do message-level truncation
        parts = [("p1", None, "ses_main", 1, 1, json.dumps({"type": "text", "text": "x"}))]
        env = _main_db_env(tmp_path, parts)
        assert find_exclude_message_id("ses_main", "p1", env) is None


# ── find_main_part_id_for_subagent ──────────────────────────────────────


class TestFindMainPartId:
    @staticmethod
    def test_finds_task_part_for_subagent(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_main", 1, "intro"),
            _task_part("p2", "ses_main", 2, "ses_sub_x", "X-out"),
            _task_part("p3", "ses_main", 3, "ses_sub_y", "Y-out"),
        ]
        env = _main_db_env(tmp_path, parts)
        assert find_main_part_id_for_subagent("ses_main", "ses_sub_x", env) == "p2"
        assert find_main_part_id_for_subagent("ses_main", "ses_sub_y", env) == "p3"

    @staticmethod
    def test_not_found_returns_none(tmp_path: Path):
        parts = [_task_part("p1", "ses_main", 1, "ses_sub_x")]
        env = _main_db_env(tmp_path, parts)
        assert find_main_part_id_for_subagent("ses_main", "ses_sub_zzz", env) is None

    @staticmethod
    def test_empty_args_return_none():
        assert find_main_part_id_for_subagent("", "x") is None
        assert find_main_part_id_for_subagent("s", "") is None


# ── seed_prebuilt_session ─────────────────────────────────────────────


class TestSeedPrebuiltSession:
    @staticmethod
    def test_clones_session_and_truncated_parts(tmp_path: Path):
        parts = [
            _text_part("p1", "ses_orig", 1, "before"),
            _task_part("p2", "ses_orig", 2, "ses_sub_x"),
            _text_part("p3", "ses_orig", 3, "after checkpoint"),
        ]
        messages = [
            _user_msg("m_p1", "ses_orig", 1),
            _asst_msg("m_p2", "ses_orig", 2, parent_id="m_p1"),
            _user_msg("m_p3", "ses_orig", 3),
        ]
        _origin_db_env(tmp_path, parts, messages)
        ok = _seed_fork(
            tmp_path, "ses_tb_new", "[tb] test", up_to_part_id="p2", directory="/fork/cwd"
        )
        assert ok is True
        # Verify the new session + parts exist in the db
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT title, parent_id, directory, path FROM session WHERE id = 'ses_tb_new'"
        ).fetchone()
        assert row is not None
        assert row[0] == "[tb] test"
        assert row[1] is None  # parent_id NULL: replay is a top-level session, not a subagent
        assert row[2] == "/fork/cwd"  # directory overridden to fork cwd
        assert row[3] == ""  # path = '' (matches opencode real session format)
        new_parts = con.execute(
            "SELECT id, session_id, data FROM part WHERE session_id = 'ses_tb_new' "
            "ORDER BY time_created ASC",
        ).fetchall()
        assert len(new_parts) == 2  # only p1 + p2 (truncated, excludes p3)
        # New part ids (not colliding with source)
        assert all(p[0].startswith("prt_tb_") for p in new_parts)
        # Data preserved (p2 is the task part)
        last_data = json.loads(new_parts[-1][2])
        assert last_data["tool"] == "task"
        # message rows for kept parts are seeded (m_p1, m_p2); m_p3 excluded
        new_msgs = con.execute(
            "SELECT id, session_id, data FROM message WHERE session_id = 'ses_tb_new' "
            "ORDER BY time_created ASC",
        ).fetchall()
        con.close()
        assert len(new_msgs) == 2
        seeded_ids = {r[0] for r in new_msgs}
        # message ids remapped to "msg" prefix (opencode MessageV2 schema requires it)
        assert seeded_ids == {"msg_p1", "msg_p2"}
        # Roles preserved
        roles = {r[0]: json.loads(r[2])["role"] for r in new_msgs}
        assert roles["msg_p1"] == "user"
        assert roles["msg_p2"] == "assistant"
        # Original session still intact (not clobbered)
        con = sqlite3.connect(db)
        orig = con.execute(
            "SELECT title FROM session WHERE id = 'ses_orig'",
        ).fetchone()
        con.close()
        assert orig[0] == "orig"

    @staticmethod
    def test_returns_false_for_missing_source_session(tmp_path: Path):
        _make_opencode_db(tmp_path, sessions=[], parts=[])
        env = {"XDG_DATA_HOME": str(tmp_path)}
        ok = seed_prebuilt_session(
            source_session_id="ses_zzz",
            new_session_id="ses_tb_new",
            up_to_part_id=None,
            title="x",
            env=env,
            source_env=env,
        )
        assert ok is False

    @staticmethod
    def test_returns_false_for_missing_db(tmp_path: Path):
        ok = _seed_fork(tmp_path, "ses_tb_new", "x")
        assert ok is False

    @staticmethod
    def test_idempotent_on_same_new_id(tmp_path: Path):
        parts = [_text_part("p1", "ses_orig", 1, "x")]
        _make_opencode_db(
            tmp_path,
            sessions=[
                {"id": "ses_orig", "title": "o", "time_created": 1, "time_updated": 1},
            ],
            parts=parts,
        )
        assert _seed_fork(tmp_path, "ses_tb", "t1", up_to_part_id="p1") is True
        # Re-seed with same new id → INSERT OR REPLACE, no error
        assert _seed_fork(tmp_path, "ses_tb", "t2", up_to_part_id="p1") is True

    @staticmethod
    def test_exclude_part_id_seeds_without_task_part(tmp_path: Path):
        # re-call X (设计 §4.6): seed excludes X's task part → main agent re-dispatches X
        parts = [
            _text_part("p1", "ses_orig", 1, "intro"),
            _task_part("p2", "ses_orig", 2, "ses_sub_x", "X-out"),
            _text_part("p3", "ses_orig", 3, "after X"),
        ]
        _origin_db_env(
            tmp_path,
            parts,
            [
                _user_msg("m_p1", "ses_orig", 1),
                _asst_msg("m_p2", "ses_orig", 2, parent_id="m_p1"),
                _user_msg("m_p3", "ses_orig", 3),
            ],
        )
        # exclude X's task part（re-call X，设计 §4.6）
        ok = _seed_fork(
            tmp_path, "ses_tb_rec", "[tb-rec]", up_to_part_id="p2", exclude_part_id="p2"
        )
        assert ok is True
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        new_parts = con.execute(
            "SELECT data FROM part WHERE session_id = 'ses_tb_rec' ORDER BY time_created ASC",
        ).fetchall()
        # Only p1 (intro text) — X's task part (p2) and p3 excluded
        assert len(new_parts) == 1
        data = json.loads(new_parts[0][0])
        assert data["type"] == "text"
        assert data["text"] == "intro"
        # Only m_p1 message seeded (m_p2/m_p3 excluded along with their parts)
        new_msgs = con.execute(
            "SELECT id FROM message WHERE session_id = 'ses_tb_rec'",
        ).fetchall()
        con.close()
        assert len(new_msgs) == 1
        # message id remapped to "msg" prefix
        assert new_msgs[0][0] == "msg_p1"

    @staticmethod
    def test_exclude_part_id_first_part_seeds_empty_session(tmp_path: Path):
        # Edge: X's task part is the first part → seed an empty session (zero parts)
        parts = [
            _task_part("p1", "ses_orig", 1, "ses_sub_x", "X-out"),
            _text_part("p2", "ses_orig", 2, "after"),
        ]
        _origin_db_env(tmp_path, parts)
        ok = _seed_fork(
            tmp_path, "ses_tb_empty", "[tb-empty]", up_to_part_id="p1", exclude_part_id="p1"
        )
        assert ok is True
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        # Session row exists, zero parts
        sess = con.execute(
            "SELECT title FROM session WHERE id = 'ses_tb_empty'",
        ).fetchone()
        assert sess is not None
        assert sess[0] == "[tb-empty]"
        n_parts = con.execute(
            "SELECT COUNT(*) FROM part WHERE session_id = 'ses_tb_empty'",
        ).fetchone()[0]
        con.close()
        assert n_parts == 0

    @staticmethod
    def test_rewrites_origin_cwd_paths_to_fork_cwd(tmp_path: Path):
        # Regression: seeded part/message data contains absolute paths to the
        # ORIGIN cwd (tool filePath, cwd text, metadata.filepath). If not rewritten
        # to the fork cwd, the replay main agent uses the old absolute paths →
        # subagents write files to the origin dir, not fork cwd → snapshot engine
        # mirrors fork cwd and misses the new files → checkpoints lack subagent
        # products (e.g. tester's checkpoint missing impl.py).
        #
        # 透明重放·结构化路径重写（设计 §3.6 方案 2）：
        # - 路径字段（filePath/path/filepath/directory/cwd）→ resolve + prefix-match 替换；
        # - 内容字段（output/command/content/text）→ path-prefix-aware 替换（消除 /foo/bar-baz
        #   误伤）；text 也重写：worker 用 L2 text 构造 subagent prompt，若 text 仍提
        #   origin_cwd（如「当前工作目录：<origin_cwd>」）→ subagent prompt 带 origin_cwd
        #   → subagent 访问 origin 文件 → opencode workspace 边界阻写 → 悬死。
        origin_cwd = "/proj/origin"
        fork_cwd = "/proj/origin/.workflow-controller/traceback/abc123"
        # A tool part (read) whose input.filePath is an absolute path under origin
        read_part = _read_part_row(
            "p1", "ses_orig", 1, f"{origin_cwd}/design_api.md", with_metadata=True
        )
        # A text part that mentions the origin cwd in prose — prose IS rewritten
        # (path-prefix-aware: origin_cwd at end-of-string is a path reference; the
        # worker uses L2 text to construct subagent prompts, so origin_cwd must be
        # rewritten to fork_cwd to avoid path leak → subagent accessing origin files).
        text_part = _text_part("p2", "ses_orig", 2, f"cwd is {origin_cwd}")
        _origin_db_env(
            tmp_path,
            [read_part, text_part],
            [
                _user_msg("m_p1", "ses_orig", 1),
                _asst_msg("m_p2", "ses_orig", 2, parent_id="m_p1"),
            ],
            directory=origin_cwd,
        )
        ok = _seed_fork(tmp_path, "ses_tb_rw", "[tb-rw]", directory=fork_cwd)
        assert ok is True
        new_parts = _seeded_part_rows(tmp_path, "ses_tb_rw")
        assert len(new_parts) == 2
        # read part: all origin_cwd path fields rewritten → fork_cwd
        read_data = json.loads(new_parts[0][0])
        assert read_data["state"]["input"]["filePath"] == f"{fork_cwd}/design_api.md"
        assert f"{fork_cwd}/design_api.md" in read_data["state"]["output"]
        assert read_data["state"]["metadata"]["filepath"] == f"{fork_cwd}/design_api.md"
        # No stale path to origin_cwd that ISN'T part of fork_cwd: verify every
        # occurrence of origin_cwd in the raw is followed by /.workflow-controller/traceback
        # (i.e. it's been rewritten into fork_cwd, not left as a bare origin path).
        import re

        stale = re.findall(
            re.escape(origin_cwd) + r"(?!/\.workflow-controller/traceback/abc123)",
            new_parts[0][0],
        )
        assert stale == [], f"un-rewritten origin_cwd reference remains: {stale}"
        # text part: prose cwd mention IS rewritten (path-prefix-aware) — the worker
        # uses L2 text to construct subagent prompts, so origin_cwd must be rewritten
        # to fork_cwd to avoid the subagent accessing origin files (path leak → hang).
        text_data = json.loads(new_parts[1][0])
        assert text_data["text"] == f"cwd is {fork_cwd}", (
            f"text prose origin_cwd not rewritten: {text_data['text']!r}"
        )

    @staticmethod
    def test_no_rewrite_when_directory_omitted(tmp_path: Path):
        # When directory=None (no fork cwd override), part data is preserved as-is
        # (no path rewriting). Backward compatibility for callers that don't fork.
        origin_cwd = "/proj/origin"
        read_part = (
            "p1",
            "m_p1",
            "ses_orig",
            1,
            1,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {"input": {"filePath": f"{origin_cwd}/file.md"}},
                }
            ),
        )
        _origin_db_env(
            tmp_path,
            [read_part],
            [_user_msg("m_p1", "ses_orig", 1)],
            directory=origin_cwd,
            with_agent_model=False,
        )
        ok = _seed_fork(tmp_path, "ses_tb_norw", "[tb]")  # no fork cwd → no rewrite
        assert ok is True
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        raw = con.execute(
            "SELECT data FROM part WHERE session_id = 'ses_tb_norw'",
        ).fetchone()
        con.close()
        assert raw is not None
        # origin_cwd preserved (not rewritten)
        assert origin_cwd in raw[0]

    @staticmethod
    def test_no_substring_misfire_on_path_sibling(tmp_path: Path):
        # Regression for str.replace misfire: origin_cwd "/proj/origin" would naively
        # rewrite "/proj/origin-baz" → "<fork_cwd>-baz" (a nonexistent path). Structured
        # walk (resolve + is_relative_to) must NOT touch sibling paths sharing a prefix.
        origin_cwd = "/proj/origin"
        fork_cwd = "/tmp/fork"
        # A read part whose filePath is a sibling path (NOT under origin_cwd)
        read_part = _read_part_row(
            "p1", "ses_orig", 1, f"{origin_cwd}-baz/design_api.md", with_metadata=False
        )
        _origin_db_env(
            tmp_path,
            [read_part],
            [_user_msg("m_p1", "ses_orig", 1)],
            directory=origin_cwd,
            with_agent_model=False,
        )
        ok = _seed_fork(tmp_path, "ses_tb_nosub", "[tb-nosub]", directory=fork_cwd)
        assert ok is True
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        raw = con.execute(
            "SELECT data FROM part WHERE session_id = 'ses_tb_nosub'",
        ).fetchone()
        con.close()
        assert raw is not None
        data = json.loads(raw[0])
        # Sibling path NOT rewritten (resolve+is_relative_to correctly excludes it)
        assert data["state"]["input"]["filePath"] == f"{origin_cwd}-baz/design_api.md"
        # output content field: also NOT rewritten (path-prefix-aware lookahead
        # requires origin_cwd followed by / — here it's followed by "-baz")
        assert data["state"]["output"] == f"<path>{origin_cwd}-baz/design_api.md</path>"

    @staticmethod
    def test_non_origin_absolute_path_not_rewritten(tmp_path: Path):
        # Design §3.6 方案 2 + §6 验证清单 8: non-origin_cwd absolute paths are
        # explicitly NOT rewritten (combined with fs isolation, the worker can't
        # reach them even if it reads the stale path from L2).
        origin_cwd = "/proj/origin"
        fork_cwd = "/tmp/fork"
        outside = "/abs/outside/origin_cwd/foo.txt"  # shares no prefix with origin_cwd
        read_part = _read_part_row("p1", "ses_orig", 1, outside, with_metadata=False)
        _origin_db_env(
            tmp_path,
            [read_part],
            [_user_msg("m_p1", "ses_orig", 1)],
            directory=origin_cwd,
            with_agent_model=False,
        )
        ok = _seed_fork(tmp_path, "ses_tb_outside", "[tb-outside]", directory=fork_cwd)
        assert ok is True
        db = tmp_path / "opencode" / "opencode.db"
        con = sqlite3.connect(db)
        raw = con.execute(
            "SELECT data FROM part WHERE session_id = 'ses_tb_outside'",
        ).fetchone()
        con.close()
        data = json.loads(raw[0])
        # Non-origin absolute path preserved verbatim
        assert data["state"]["input"]["filePath"] == outside
        assert data["state"]["output"] == f"<path>{outside}</path>"

    @staticmethod
    def test_message_level_truncation_in_seed(tmp_path: Path):
        # 透明重放·message 级截断（设计 §2.6）在 seed 里的端到端验证：exclude_message_id
        # 让 seed 丢弃 X 所在 message 的全部 part 及其后，worker 看不到「我将调用 X」
        # 的前置 text/reasoning（同 message 的孤儿文本）。
        #
        # 布局：p1(m_a) | p2+p3 同属 m_b（reasoning「我将调用 X」+ task part）| p4(m_c)
        # exclude_message_id="m_b" → 只 seed p1。
        parts = [
            _text_part("p1", "ses_orig", 1, "before"),  # m_p1
            (
                "p2",
                "m_b",
                "ses_orig",
                2,
                2,
                json.dumps({"type": "reasoning", "text": "I'll call X"}),
            ),
            _task_part("p3", "ses_orig", 3, "ses_sub_x"),  # m_b (task part)
            _text_part("p4", "ses_orig", 4, "after"),  # m_p4
        ]
        _origin_db_env(
            tmp_path,
            parts,
            [
                _user_msg("m_p1", "ses_orig", 1),
                ("m_b", "ses_orig", 2, 2, json.dumps({"role": "assistant"})),
                _user_msg("m_p4", "ses_orig", 4),
            ],
        )
        ok = _seed_fork(
            tmp_path, "ses_tb_msg", "[tb-msg]", exclude_message_id="m_b", directory="/fork"
        )
        assert ok is True
        new_parts = _seeded_part_rows(tmp_path, "ses_tb_msg")
        # Only p1 (before message m_b) — p2/p3/p4 all dropped (message-level truncation)
        assert len(new_parts) == 1
        data = json.loads(new_parts[0][0])
        assert data["text"] == "before"
        # The reasoning "I'll call X" is NOT seeded (no orphan text)
        assert not any("I'll call X" in p[0] for p in new_parts)


# ── schema-only fork db init ───────────────────────────────────────────


class TestInitForkDbSchemaOnly:
    @staticmethod
    def test_schema_only_copy_skips_session_data(tmp_path: Path):
        # schema-only 初始化：只拷 schema + 全局小表行，session 表建空
        from workflow_controller.plugins.agent_traceback.l2_reader import _init_fork_db_from_source

        # Build a source db with a big 'part' table (lots of rows → big file)
        src_db = tmp_path / "opencode" / "opencode.db"
        src_db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(src_db))
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
        con.execute("CREATE TABLE part (id TEXT, data TEXT)")
        con.execute("CREATE TABLE message (id TEXT, data TEXT)")
        con.execute("CREATE TABLE __drizzle_migrations (id TEXT PRIMARY KEY, hash TEXT)")
        con.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT)")
        con.execute("INSERT INTO __drizzle_migrations VALUES ('0001', 'h1')")
        con.execute("INSERT INTO project VALUES ('p1', '/proj', 'proj')")
        con.execute("INSERT INTO session VALUES ('ses_orig', '/proj')")
        # Insert ~2000 rows with ~2KB data each → ~4MB source db
        big = "x" * 2000
        con.executemany(
            "INSERT INTO part VALUES (?, ?)",
            [(f"p{i}", big) for i in range(2000)],
        )
        con.execute("INSERT INTO message VALUES ('m1', '{}')")
        con.commit()
        con.close()
        src_size = src_db.stat().st_size
        assert src_size > 100_000  # sanity: source is non-trivial

        dest_db = tmp_path / "fork" / "opencode.db"
        dest_db.parent.mkdir(parents=True)
        ok = _init_fork_db_from_source(dest_db, src_db)
        assert ok is True
        dest_size = dest_db.stat().st_size
        # 不拷 session 数据 → dest 远小于 source
        assert dest_size < src_size / 10, (
            f"schema-only init copied too much: src={src_size} dest={dest_size}"
        )
        # session 范围表零行…
        con = sqlite3.connect(str(dest_db))
        assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0
        # …全局表（migrations/project）行已拷
        assert con.execute("SELECT COUNT(*) FROM __drizzle_migrations").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 1
        con.close()

    @staticmethod
    def test_init_failure_cleans_partial_dest(tmp_path: Path):
        # 初始化失败不留半成品（残留文件会毒化下次 need_init 判断）
        from workflow_controller.plugins.agent_traceback.l2_reader import _init_fork_db_from_source

        dest_db = tmp_path / "fork" / "opencode.db"
        dest_db.parent.mkdir(parents=True)
        # source 不存在 → 初始化失败
        ok = _init_fork_db_from_source(dest_db, tmp_path / "nope" / "src.db")
        assert ok is False
        assert not dest_db.exists()


# ── schema self-check warns on unknown tables ─────────────────────────


class TestInitForkDbSchemaSelfCheck:
    @staticmethod
    def test_warns_on_unknown_table(tmp_path: Path, caplog):
        # 源库出现未知表时打 warning（schema 漂移提醒）
        import logging

        from workflow_controller.plugins.agent_traceback.l2_reader import _init_fork_db_from_source

        src_db = tmp_path / "opencode" / "opencode.db"
        src_db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(src_db))
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE part (id TEXT)")
        con.execute("CREATE TABLE message (id TEXT)")
        con.execute("CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY)")
        # An unknown session-scope table (simulates opencode schema drift)
        con.execute("CREATE TABLE session_extra (session_id TEXT, note TEXT)")
        con.execute("INSERT INTO session_extra VALUES ('s1', 'note')")
        con.commit()
        con.close()
        dest_db = tmp_path / "fork" / "opencode.db"
        dest_db.parent.mkdir(parents=True)
        logger = logging.getLogger("workflow_controller.plugins.agent_traceback.l2_reader")
        logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING):
                ok = _init_fork_db_from_source(dest_db, src_db)
            assert ok is True
            # Warning mentions the unknown table
            assert any(
                "session_extra" in rec.message and "unknown" in rec.message
                for rec in caplog.records
            ), f"no warning for unknown table, got: {[r.message for r in caplog.records]}"
        finally:
            logger.removeHandler(caplog.handler)
