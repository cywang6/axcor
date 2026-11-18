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

"""read_subagent_messages / read_subagent_parts / list_subagent_session_ids
— best-effort subagent message-stream lookups in opencode's session db.

Hermetic: each test builds a throwaway sqlite db under XDG_DATA_HOME so nothing
touches the host's real ~/.local/share/opencode/opencode.db.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from workflow_controller.driver.opencode_session import (
    list_descendant_sessions,
    list_subagent_session_ids,
    read_subagent_messages,
    read_subagent_parts,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fill_part_table(con: sqlite3.Connection, rows: list[tuple]) -> None:
    """建最小 part 表（仅读者触及的列）并按 (id, message_id, session_id, ts, data) 插入。"""
    con.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
        "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    con.executemany(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        rows,
    )


def _make_part_db(tmp_path: Path, parts: list[tuple[str, int, str]]) -> Path:
    """Build a throwaway opencode.db with a ``part`` table.

    Each entry: (session_id, time_created, data_json_string). The part ``id``
    is auto-generated as ``p0, p1, …``.
    """
    dbdir = tmp_path / "opencode"
    dbdir.mkdir()
    db = dbdir / "opencode.db"
    con = sqlite3.connect(db)
    _fill_part_table(
        con, [(f"p{i}", f"m{i}", sid, ts, data) for i, (sid, ts, data) in enumerate(parts)]
    )
    con.commit()
    con.close()
    return db


def _make_part_db_with_ids(tmp_path: Path, parts: list[tuple[str, str, int, str]]) -> Path:
    """Like _make_part_db but each entry specifies an explicit part id.

    Each entry: (part_id, session_id, time_created, data_json_string).
    """
    dbdir = tmp_path / "opencode"
    dbdir.mkdir()
    db = dbdir / "opencode.db"
    con = sqlite3.connect(db)
    _fill_part_table(
        con, [(pid, f"m{i}", sid, ts, data) for i, (pid, sid, ts, data) in enumerate(parts)]
    )
    con.commit()
    con.close()
    return db


def _make_session_db(
    tmp_path: Path,
    sessions: list[tuple[str, str | None]],
    parts: list[tuple[str, str, int, str]] | None = None,
) -> Path:
    """Build a throwaway opencode.db with ``session`` (id, parent_id) and
    optional ``part`` rows.
    """
    dbdir = tmp_path / "opencode"
    dbdir.mkdir(parents=True, exist_ok=True)
    db = dbdir / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT)")
    con.executemany(
        "INSERT INTO session (id, parent_id) VALUES (?, ?)",
        [(sid, pid) for sid, pid in sessions],
    )
    if parts:
        _fill_part_table(
            con, [(pid, f"m{i}", sid, ts, data) for i, (pid, sid, ts, data) in enumerate(parts)]
        )
    con.commit()
    con.close()
    return db


def test_reads_text_parts(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "text", "text": "Hello world"})),
        ("ses_a", 2, json.dumps({"type": "text", "text": "Second message"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 2
    assert msgs[0] == {"type": "text", "text": "Hello world"}
    assert msgs[1] == {"type": "text", "text": "Second message"}


def test_reads_reasoning_as_thinking(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "reasoning", "text": "Let me think..."})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 1
    assert msgs[0] == {"type": "thinking", "text": "Let me think..."}


def test_reads_tool_as_tool_use_plus_tool_result(tmp_path: Path):
    parts = [
        (
            "ses_a",
            1,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call-123",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls -la"},
                        "output": "total 0\ndrwxr-xr-x 2 root root 40 Jan 1 00:00 .",
                    },
                }
            ),
        ),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 2
    assert msgs[0]["type"] == "tool_use"
    assert msgs[0]["name"] == "bash"
    assert msgs[0]["id"] == "call-123"
    assert msgs[0]["input"] == {"command": "ls -la"}
    assert msgs[1]["type"] == "tool_result"
    assert msgs[1]["tool_use_id"] == "call-123"
    assert "total 0" in msgs[1]["content"]
    assert msgs[1]["is_error"] is False


def test_tool_error_state(tmp_path: Path):
    parts = [
        (
            "ses_a",
            1,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "edit",
                    "callID": "call-err",
                    "state": {"status": "error", "input": {}, "output": "File not found"},
                }
            ),
        ),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert msgs[1]["is_error"] is True


def test_mixed_part_types_ordered_by_time(tmp_path: Path):
    parts = [
        ("ses_a", 3, json.dumps({"type": "text", "text": "Result"})),
        ("ses_a", 1, json.dumps({"type": "reasoning", "text": "Thinking first"})),
        ("ses_a", 2, json.dumps({"type": "step-start"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 3
    assert msgs[0]["type"] == "thinking"
    assert msgs[0]["text"] == "Thinking first"
    assert msgs[1]["type"] == "step_start"
    assert msgs[2]["type"] == "text"
    assert msgs[2]["text"] == "Result"


def test_empty_text_skipped(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "text", "text": "   "})),
        ("ses_a", 2, json.dumps({"type": "text", "text": "Real"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Real"


def test_unknown_part_type_skipped(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "patch", "hash": "abc", "files": ["x.py"]})),
        ("ses_a", 2, json.dumps({"type": "text", "text": "Visible"})),
        ("ses_a", 3, json.dumps({"type": "compaction", "tokens": 5000})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 1
    assert msgs[0]["type"] == "text"


def test_truncates_to_tail_max_parts(tmp_path: Path):
    parts = [("ses_a", i, json.dumps({"type": "text", "text": f"msg-{i}"})) for i in range(1, 10)]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)}, max_parts=3)
    assert len(msgs) == 3
    assert msgs[0]["text"] == "msg-7"
    assert msgs[2]["text"] == "msg-9"


def test_scoped_to_sub_session_id(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "text", "text": "from-a"})),
        ("ses_b", 1, json.dumps({"type": "text", "text": "from-b"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 1
    assert msgs[0]["text"] == "from-a"


def test_empty_id_returns_empty():
    assert read_subagent_messages("") == []


def test_missing_db_returns_empty(tmp_path: Path):
    assert read_subagent_messages("ses_x", {"XDG_DATA_HOME": str(tmp_path)}) == []


def test_missing_part_table_returns_empty(tmp_path: Path):
    """Schema drift: db exists but no ``part`` table → graceful empty list."""
    dbdir = tmp_path / "opencode"
    dbdir.mkdir()
    con = sqlite3.connect(dbdir / "opencode.db")
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    assert read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)}) == []


def test_malformed_json_skipped(tmp_path: Path):
    parts = [
        ("ses_a", 1, "not-json-at-all"),
        ("ses_a", 2, json.dumps({"type": "text", "text": "OK"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 1
    assert msgs[0]["text"] == "OK"


def test_step_start_preserved(tmp_path: Path):
    parts = [
        ("ses_a", 1, json.dumps({"type": "step-start"})),
        ("ses_a", 2, json.dumps({"type": "text", "text": "after step"})),
    ]
    _make_part_db(tmp_path, parts)
    msgs = read_subagent_messages("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
    assert len(msgs) == 2
    assert msgs[0]["type"] == "step_start"
    assert msgs[1]["type"] == "text"


# ── read_subagent_parts: part-id-tagged for incremental diffing ──────────


class TestReadSubagentParts:
    """read_subagent_parts returns (part_id, [messages]) so the poller can
    diff by part id and emit only new messages between polls.
    """

    @staticmethod
    def test_text_part_carries_id(tmp_path: Path):
        parts = [("prt_1", "ses_a", 1, json.dumps({"type": "text", "text": "hi"}))]
        _make_part_db_with_ids(tmp_path, parts)
        out = read_subagent_parts("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
        assert len(out) == 1
        assert out[0][0] == "prt_1"
        assert out[0][1] == [{"type": "text", "text": "hi"}]

    @staticmethod
    def test_tool_part_yields_two_msgs_one_id(tmp_path: Path):
        """A 'tool' part expands to tool_use + tool_result sharing one part_id."""
        parts = [
            (
                "prt_tool",
                "ses_a",
                1,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call-1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls"},
                            "output": "a\nb",
                        },
                    }
                ),
            )
        ]
        _make_part_db_with_ids(tmp_path, parts)
        out = read_subagent_parts("ses_a", {"XDG_DATA_HOME": str(tmp_path)})
        assert len(out) == 1
        pid, msgs = out[0]
        assert pid == "prt_tool"
        assert len(msgs) == 2
        assert msgs[0]["type"] == "tool_use"
        assert msgs[1]["type"] == "tool_result"

    @staticmethod
    def test_messages_flatten_matches_read_subagent_messages(tmp_path: Path):
        """read_subagent_messages is just the flattened form of _parts."""
        parts = [
            ("p1", "ses_a", 1, json.dumps({"type": "reasoning", "text": "think"})),
            ("p2", "ses_a", 2, json.dumps({"type": "text", "text": "say"})),
            ("p3", "ses_a", 3, json.dumps({"type": "step-start"})),
        ]
        _make_part_db_with_ids(tmp_path, parts)
        env = {"XDG_DATA_HOME": str(tmp_path)}
        flat = read_subagent_messages("ses_a", env)
        from_parts: list = []
        for _pid, msgs in read_subagent_parts("ses_a", env):
            from_parts.extend(msgs)
        assert flat == from_parts

    @staticmethod
    def test_empty_id_returns_empty():
        assert read_subagent_parts("") == []

    @staticmethod
    def test_missing_db_returns_empty(tmp_path: Path):
        assert read_subagent_parts("ses_x", {"XDG_DATA_HOME": str(tmp_path)}) == []

    @staticmethod
    def test_truncates_to_tail(tmp_path: Path):
        parts = [
            (f"p{i}", "ses_a", i, json.dumps({"type": "text", "text": f"m{i}"}))
            for i in range(1, 6)
        ]
        _make_part_db_with_ids(tmp_path, parts)
        out = read_subagent_parts("ses_a", {"XDG_DATA_HOME": str(tmp_path)}, max_parts=2)
        assert len(out) == 2
        assert out[0][0] == "p4"
        assert out[1][0] == "p5"


# ── list_subagent_session_ids: discovery for the streaming poller ───────


class TestListSubagentSessionIds:
    """list_subagent_session_ids discovers sub-sessions for the poller,
    excluding the main session and (optionally) a baseline of stale ids.
    """

    @staticmethod
    def test_lists_distinct_sessions(tmp_path: Path):
        parts = [
            ("p1", "ses_main", 1, json.dumps({"type": "text", "text": "main"})),
            ("p2", "ses_sub_a", 2, json.dumps({"type": "text", "text": "a"})),
            ("p3", "ses_sub_b", 3, json.dumps({"type": "text", "text": "b"})),
        ]
        _make_part_db_with_ids(tmp_path, parts)
        ids = list_subagent_session_ids({"XDG_DATA_HOME": str(tmp_path)})
        assert set(ids) == {"ses_main", "ses_sub_a", "ses_sub_b"}

    @staticmethod
    def test_exclude_filters_out_main(tmp_path: Path):
        parts = [
            ("p1", "ses_main", 1, json.dumps({"type": "text", "text": "m"})),
            ("p2", "ses_sub", 2, json.dumps({"type": "text", "text": "s"})),
        ]
        _make_part_db_with_ids(tmp_path, parts)
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            exclude={"ses_main"},
        )
        assert ids == ["ses_sub"]

    @staticmethod
    def test_ordered_by_earliest_time(tmp_path: Path):
        parts = [
            ("p1", "ses_b", 5, json.dumps({"type": "text", "text": "b"})),
            ("p2", "ses_a", 1, json.dumps({"type": "text", "text": "a"})),
            ("p3", "ses_b", 6, json.dumps({"type": "text", "text": "b2"})),
        ]
        _make_part_db_with_ids(tmp_path, parts)
        ids = list_subagent_session_ids({"XDG_DATA_HOME": str(tmp_path)})
        # ses_a has the earliest part (time 1) → comes first
        assert ids == ["ses_a", "ses_b"]

    @staticmethod
    def test_missing_db_returns_empty(tmp_path: Path):
        assert list_subagent_session_ids({"XDG_DATA_HOME": str(tmp_path)}) == []

    @staticmethod
    def test_missing_table_returns_empty(tmp_path: Path):
        dbdir = tmp_path / "opencode"
        dbdir.mkdir()
        con = sqlite3.connect(dbdir / "opencode.db")
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        con.commit()
        con.close()
        assert list_subagent_session_ids({"XDG_DATA_HOME": str(tmp_path)}) == []

    # ── parent_id filtering (scopes poller to current worker only) ──

    @staticmethod
    def test_parent_id_filters_to_own_subsessions(tmp_path: Path):
        """Only sub-sessions whose session.parent_id matches are returned."""
        sessions = [
            ("ses_main_a", None),
            ("ses_main_b", None),
            ("ses_sub_of_a", "ses_main_a"),
            ("ses_sub_of_b", "ses_main_b"),
            ("ses_tui", None),
        ]
        parts = [
            ("p1", "ses_main_a", 1, json.dumps({"type": "text", "text": "a"})),
            ("p2", "ses_main_b", 2, json.dumps({"type": "text", "text": "b"})),
            ("p3", "ses_sub_of_a", 3, json.dumps({"type": "text", "text": "sa"})),
            ("p4", "ses_sub_of_b", 4, json.dumps({"type": "text", "text": "sb"})),
            ("p5", "ses_tui", 5, json.dumps({"type": "text", "text": "tui"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            exclude={"ses_main_a"},
            parent_id="ses_main_a",
        )
        assert ids == ["ses_sub_of_a"]

    @staticmethod
    def test_parent_id_excludes_other_workers_children(tmp_path: Path):
        """Only the current worker's children are returned."""
        sessions = [
            ("ses_main_a", None),
            ("ses_main_b", None),
            ("ses_sub_of_b", "ses_main_b"),
        ]
        parts = [
            ("p1", "ses_main_a", 1, json.dumps({"type": "text", "text": "a"})),
            ("p2", "ses_sub_of_b", 2, json.dumps({"type": "text", "text": "sb"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            parent_id="ses_main_a",
        )
        assert ids == []

    @staticmethod
    def test_parent_id_no_parts_returns_empty(tmp_path: Path):
        """Sub-session with no parts not returned (poller reads from part table)."""
        _make_session_db(tmp_path, [("ses_main", None), ("ses_sub", "ses_main")])
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            parent_id="ses_main",
        )
        assert ids == []

    @staticmethod
    def test_parent_id_missing_session_table_returns_empty(tmp_path: Path):
        """When session table absent, JOIN fails gracefully → empty list."""
        parts = [("p1", "ses_main", 1, json.dumps({"type": "text", "text": "x"}))]
        _make_part_db_with_ids(tmp_path, parts)
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            parent_id="ses_main",
        )
        assert ids == []

    @staticmethod
    def test_parent_id_empty_falls_back_to_part_scan(tmp_path: Path):
        """parent_id 留空时回落到 part 扫描（兼容旧行为）。"""
        parts = [
            ("p1", "ses_main", 1, json.dumps({"type": "text", "text": "m"})),
            ("p2", "ses_sub", 2, json.dumps({"type": "text", "text": "s"})),
        ]
        _make_part_db_with_ids(tmp_path, parts)
        ids = list_subagent_session_ids(
            {"XDG_DATA_HOME": str(tmp_path)},
            exclude={"ses_main"},
            parent_id="",
        )
        assert ids == ["ses_sub"]


# ── list_descendant_sessions: recursive discovery (孙 agent) ────────────


class TestListDescendantSessions:
    """list_descendant_sessions 递归发现 root 的全部后代（子 + 孙 + …），
    携带各自 parent_id 供拓扑层级化。
    """

    @staticmethod
    def test_recursive_children_and_grandchildren(tmp_path: Path):
        """孙 agent（parent 指向子会话）也被发现，且各带自己的 parent。"""
        sessions = [
            ("ses_main", None),
            ("ses_child", "ses_main"),
            ("ses_grand", "ses_child"),  # 孙：parent 不是 main
            ("ses_great", "ses_grand"),  # 曾孙（三层）
        ]
        parts = [
            ("p1", "ses_child", 1, json.dumps({"type": "text", "text": "c"})),
            ("p2", "ses_grand", 2, json.dumps({"type": "text", "text": "g"})),
            ("p3", "ses_great", 3, json.dumps({"type": "text", "text": "gg"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        rows = list_descendant_sessions(
            "ses_main",
            {"XDG_DATA_HOME": str(tmp_path)},
        )
        assert rows == [
            ("ses_child", "ses_main"),
            ("ses_grand", "ses_child"),
            ("ses_great", "ses_grand"),
        ]

    @staticmethod
    def test_scopes_to_own_worker_subtree(tmp_path: Path):
        """其他 opencode 进程的子会话（别的 main 的后代）不进结果。"""
        sessions = [
            ("ses_main_a", None),
            ("ses_main_b", None),
            ("ses_child_a", "ses_main_a"),
            ("ses_grand_a", "ses_child_a"),
            ("ses_child_b", "ses_main_b"),
            ("ses_tui", None),
        ]
        parts = [
            ("p1", "ses_child_a", 1, json.dumps({"type": "text", "text": "ca"})),
            ("p2", "ses_grand_a", 2, json.dumps({"type": "text", "text": "ga"})),
            ("p3", "ses_child_b", 3, json.dumps({"type": "text", "text": "cb"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        rows = list_descendant_sessions(
            "ses_main_a",
            {"XDG_DATA_HOME": str(tmp_path)},
        )
        assert rows == [("ses_child_a", "ses_main_a"), ("ses_grand_a", "ses_child_a")]

    @staticmethod
    def test_ordered_by_earliest_part_time(tmp_path: Path):
        """按 part 表最早时间排序（与直接子代发现一致）。"""
        sessions = [("ses_main", None), ("ses_a", "ses_main"), ("ses_b", "ses_main")]
        parts = [
            ("p1", "ses_b", 5, json.dumps({"type": "text", "text": "b"})),
            ("p2", "ses_a", 1, json.dumps({"type": "text", "text": "a"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        rows = list_descendant_sessions(
            "ses_main",
            {"XDG_DATA_HOME": str(tmp_path)},
        )
        assert rows == [("ses_a", "ses_main"), ("ses_b", "ses_main")]

    @staticmethod
    def test_no_part_rows_excluded(tmp_path: Path):
        """part 表无行的会话不返回（poller 从 part 表读流）。"""
        _make_session_db(
            tmp_path,
            [("ses_main", None), ("ses_sub", "ses_main")],
        )
        assert (
            list_descendant_sessions(
                "ses_main",
                {"XDG_DATA_HOME": str(tmp_path)},
            )
            == []
        )

    @staticmethod
    def test_corrupt_self_loop_row_ignored(tmp_path: Path):
        """脏数据自环行（parent_id = 自身）不会让递归查询挂起或进结果。"""
        sessions = [
            ("ses_main", None),
            ("ses_child", "ses_main"),
            ("ses_selfloop", "ses_selfloop"),  # 脏数据：自环
        ]
        parts = [
            ("p1", "ses_child", 1, json.dumps({"type": "text", "text": "c"})),
            ("p2", "ses_selfloop", 2, json.dumps({"type": "text", "text": "x"})),
        ]
        _make_session_db(tmp_path, sessions, parts)
        rows = list_descendant_sessions(
            "ses_main",
            {"XDG_DATA_HOME": str(tmp_path)},
        )
        assert rows == [("ses_child", "ses_main")]

    @staticmethod
    def test_missing_db_returns_empty(tmp_path: Path):
        assert (
            list_descendant_sessions(
                "ses_main",
                {"XDG_DATA_HOME": str(tmp_path)},
            )
            == []
        )

    @staticmethod
    def test_missing_session_table_returns_empty(tmp_path: Path):
        """session 表缺失（仅 part 表）时 JOIN 失败优雅返回空。"""
        parts = [("p1", "ses_main", 1, json.dumps({"type": "text", "text": "x"}))]
        _make_part_db_with_ids(tmp_path, parts)
        assert (
            list_descendant_sessions(
                "ses_main",
                {"XDG_DATA_HOME": str(tmp_path)},
            )
            == []
        )

    @staticmethod
    def test_empty_root_returns_empty():
        assert list_descendant_sessions("") == []
