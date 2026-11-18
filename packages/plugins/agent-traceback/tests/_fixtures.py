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

"""agent_traceback 测试共享夹具 —— 行字典驱动的函数式构造器。

原先各 ``test_*.py`` 各自内联的重复构造逻辑（cannbot 5 表 SQLite db、
source opencode.db schema 模板、可读写的假 opencode.db、TimelineEntry、
part/message 行）在此统一收尾：调用方给行字典（缺列由默认值补齐），
本模块负责落库 / 组装，保证各测试产出的 db 与 JSONL 逐字节等价。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, NamedTuple

from workflow_controller.plugins.agent_traceback.session_reconstructor.db_reader import (
    TimelineEntry,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── cannbot 5 表（Session/Turn/ToolCall/Execution/InteractionBridge） ──

_SESSION_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "taskId TEXT",
    "label TEXT",
    "query TEXT",
    "framework TEXT",
    "model TEXT",
    "directory TEXT",
    "sourcePath TEXT",
    "rootExecutionId TEXT",
    "createdAt TEXT",
)
_TURN_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "sessionId TEXT",
    "turnIndex INTEGER",
    "role TEXT",
    "content TEXT",
    "contentJson TEXT",
    "contentSummary TEXT",
    "agentName TEXT",
    "subagentName TEXT",
    "subagentSessionId TEXT",
    "parentExecutionId TEXT",
    "finishReason TEXT",
    "isSubagent BOOLEAN",
    "createdAt TEXT",
    "completedAt TEXT",
    "model TEXT",
)
_TOOL_CALL_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "turnId TEXT",
    "toolCallId TEXT",
    "toolName TEXT",
    "argsJson TEXT",
    "resultJson TEXT",
    "state TEXT",
    "errorType TEXT",
    "errorMessage TEXT",
    "startedAt TEXT",
    "completedAt TEXT",
    "durationMs INTEGER",
    "dispatchBridgeId TEXT",
    "isSkillRelated BOOLEAN",
    "createdAt TEXT",
)
_EXECUTION_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "sessionId TEXT",
    "agentName TEXT",
    "agentSessionId TEXT",
    "isSubagent BOOLEAN",
    "subagentType TEXT",
    "subagentName TEXT",
    "parentExecutionId TEXT",
    "rootExecutionId TEXT",
    "depth INTEGER",
)
_BRIDGE_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "sessionId TEXT",
    "dispatchExecutionId TEXT",
    "dispatchTurnId TEXT",
    "dispatchToolCallId TEXT",
    "dispatchContent TEXT",
    "dispatchTimestamp TEXT",
    "responseExecutionId TEXT",
    "responseTurnId TEXT",
    "responseContent TEXT",
    "responseTimestamp TEXT",
    "subagentSessionId TEXT",
    "subagentType TEXT",
    "subagentName TEXT",
    "subagentTokens INTEGER DEFAULT 0",
    "status TEXT",
)
# 供 INSERT 用的纯列名序列（与上面建表列一一对齐）
_SESSION_COLUMNS = tuple(c.split()[0] for c in _SESSION_TABLE_COLUMNS)
_TURN_COLUMNS = tuple(c.split()[0] for c in _TURN_TABLE_COLUMNS)
_TOOL_CALL_COLUMNS = tuple(c.split()[0] for c in _TOOL_CALL_TABLE_COLUMNS)
_EXECUTION_COLUMNS = tuple(c.split()[0] for c in _EXECUTION_TABLE_COLUMNS)
_BRIDGE_COLUMNS = tuple(c.split()[0] for c in _BRIDGE_TABLE_COLUMNS)


def _create_table_sql(table: str, columns: tuple[str, ...]) -> str:
    """由 "name TYPE" 列描述元组生成建表 SQL。"""
    return f"CREATE TABLE {table} ({', '.join(columns)})"


def _insert_sql(table: str, names: tuple[str, ...]) -> str:
    placeholders = ", ".join("?" for _ in names)
    return f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})"


def _turn_values(row: dict, session_default: str) -> tuple:
    merged = {
        "sessionId": session_default,
        "turnIndex": 0,
        "role": "user",
        "content": "",
        "contentJson": "",
        "contentSummary": "",
        "agentName": "build",
        **row,
    }
    merged["turnIndex"] = int(merged["turnIndex"])
    merged["isSubagent"] = 1 if merged.get("isSubagent") else 0
    return tuple(merged.get(col) for col in _TURN_COLUMNS)


def _tool_call_values(row: dict) -> tuple:
    merged = {
        "argsJson": "",
        "resultJson": "",
        "state": "completed",
        "durationMs": 0,
        **row,
    }
    merged["durationMs"] = int(merged["durationMs"])
    merged["isSkillRelated"] = 1 if merged.get("isSkillRelated") else 0
    return tuple(merged.get(col) for col in _TOOL_CALL_COLUMNS)


def _execution_values(row: dict, session_default: str) -> tuple:
    merged = {"sessionId": session_default, "agentName": "build", "depth": 0, **row}
    merged["isSubagent"] = 1 if merged.get("isSubagent") else 0
    merged["depth"] = int(merged["depth"])
    return tuple(merged.get(col) for col in _EXECUTION_COLUMNS)


def _bridge_values(row: dict, session_default: str) -> tuple:
    merged = {
        "id": "b1",
        "sessionId": session_default,
        "subagentTokens": 0,
        "status": "completed",
        **row,
    }
    merged["subagentTokens"] = int(merged["subagentTokens"])
    return tuple(merged.get(col) for col in _BRIDGE_COLUMNS)


def cannbot_turn_row(
    tid: str,
    *,
    idx: int = 0,
    role: str | None = "user",
    content: str = "",
    sub_sid: str | None = None,
    finish: str | None = None,
    at: str | None = None,
) -> dict:
    """Turn 行字典：主/子 agent 对话回合（缺列由 ``build_cannbot_db`` 补默认值）。"""
    row: dict[str, Any] = {
        "id": tid,
        "turnIndex": idx,
        "role": role,
        "content": content,
        "createdAt": at,
    }
    if sub_sid is not None:
        row["isSubagent"] = True
        row["subagentSessionId"] = sub_sid
    if finish is not None:
        row["finishReason"] = finish
    return row


def cannbot_task_call_row(
    row_id: str,
    turn_id: str,
    call_id: str,
    sub_sid: str,
    sub_type: str = "developer",
    *,
    description: str = "do X",
    prompt: str = "be X",
    result: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """主 agent 的 task 派发 ToolCall 行字典（argsJson 内嵌子会话 id）。"""
    return {
        "id": row_id,
        "turnId": turn_id,
        "toolCallId": call_id,
        "toolName": "task",
        "argsJson": json.dumps(
            {
                "description": description,
                "prompt": prompt,
                "subagent_type": sub_type,
                "subagent_session_id": sub_sid,
            }
        ),
        "resultJson": result if result is not None else f"task_id: {sub_sid}",
        "startedAt": start,
        "completedAt": end,
    }


def cannbot_tool_call_row(
    row_id: str,
    turn_id: str,
    call_id: str,
    tool_name: str,
    args: dict | str = "",
    *,
    result: str = "",
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """任意工具的 ToolCall 行字典（args 允许 dict——自动 dumps——或现成 JSON 串）。"""
    return {
        "id": row_id,
        "turnId": turn_id,
        "toolCallId": call_id,
        "toolName": tool_name,
        "argsJson": args if isinstance(args, str) else json.dumps(args),
        "resultJson": result,
        "startedAt": start,
        "completedAt": end,
    }


def cannbot_bridge_row(
    dispatch_call_id: str,
    sub_sid: str,
    sub_type: str,
    *,
    row_id: str = "b1",
    sub_name: str | None = None,
    tokens: int = 0,
    status: str = "completed",
    at_dispatch: str | None = None,
    at_response: str | None = None,
    dispatch_turn: str | None = None,
) -> dict:
    """InteractionBridge 行字典（派发 ↔ 子会话的桥接记录）。"""
    return {
        "id": row_id,
        "dispatchToolCallId": dispatch_call_id,
        "dispatchTurnId": dispatch_turn,
        "subagentSessionId": sub_sid,
        "subagentType": sub_type,
        "subagentName": sub_name,
        "subagentTokens": tokens,
        "status": status,
        "dispatchTimestamp": at_dispatch,
        "responseTimestamp": at_response,
    }


def cannbot_execution_row(
    exec_id: str,
    agent_session_id: str,
    *,
    agent_name: str = "build",
    sub: bool = False,
    sub_type: str | None = None,
    sub_name: str | None = None,
    parent: str | None = None,
    root: str = "exec_root",
    depth: int | None = None,
) -> dict:
    """Execution 行字典（agent 执行层级树）。"""
    return {
        "id": exec_id,
        "agentName": agent_name,
        "agentSessionId": agent_session_id,
        "isSubagent": sub,
        "subagentType": sub_type,
        "subagentName": sub_name,
        "parentExecutionId": parent,
        "rootExecutionId": root,
        "depth": (1 if sub else 0) if depth is None else depth,
    }


def build_cannbot_db(
    tmp_path: Path,
    *,
    session_row: dict | None = None,
    turns: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    executions: list[dict] | None = None,
    bridges: list[dict] | None = None,
    session_defaults: dict[str, Any] | None = None,
    default_session_id: str = "cmr_test_session_id",
    db_name: str = "cannbot.db",
) -> Path:
    """建一个 cannbot-style SQLite db（5 表），缺列按默认值落库。

    ``session_defaults`` 给 Session 行的逐列默认值（列序敏感），
    ``session_row`` 覆盖其中若干列；Turn/Execution/Bridge 行缺
    ``sessionId`` 时回退 ``default_session_id``。
    """
    db = tmp_path / db_name
    con = sqlite3.connect(db)
    con.execute(_create_table_sql("Session", _SESSION_TABLE_COLUMNS))
    defaults = session_defaults or {}
    session_values = tuple(
        (session_row or {}).get(col, defaults.get(col)) for col in _SESSION_COLUMNS
    )
    con.execute(_insert_sql("Session", _SESSION_COLUMNS), session_values)
    con.execute(_create_table_sql("Turn", _TURN_TABLE_COLUMNS))
    for t in turns or []:
        con.execute(_insert_sql("Turn", _TURN_COLUMNS), _turn_values(t, default_session_id))
    con.execute(_create_table_sql("ToolCall", _TOOL_CALL_TABLE_COLUMNS))
    for tc in tool_calls or []:
        con.execute(_insert_sql("ToolCall", _TOOL_CALL_COLUMNS), _tool_call_values(tc))
    con.execute(_create_table_sql("Execution", _EXECUTION_TABLE_COLUMNS))
    for e in executions or []:
        con.execute(
            _insert_sql("Execution", _EXECUTION_COLUMNS),
            _execution_values(e, default_session_id),
        )
    con.execute(_create_table_sql("InteractionBridge", _BRIDGE_TABLE_COLUMNS))
    for b in bridges or []:
        con.execute(
            _insert_sql("InteractionBridge", _BRIDGE_COLUMNS),
            _bridge_values(b, default_session_id),
        )
    con.commit()
    con.close()
    return db


# ── TimelineEntry（db_reader 产出的时间线条目形态） ──


def task_dispatch_timeline_entry(
    ts: int,
    sub_sid: str = "ses_sub_001",
    call_id: str = "call_t1",
    *,
    with_task_result: bool = False,
) -> TimelineEntry:
    """主 agent 的 task 派发条目（checkpoint_deriver / file_state_rebuilder 共用形态）。"""
    result = f"task_id: {sub_sid}"
    if with_task_result:
        result += "\n<task_result>ok</task_result>"
    return TimelineEntry(
        ts=ts,
        role="main",
        kind="tool_call",
        turn=None,
        tool_call={
            "id": "tc_t",
            "turnId": "t_t",
            "toolCallId": call_id,
            "toolName": "task",
            "argsJson": json.dumps(
                {
                    "description": "do X",
                    "prompt": "be X",
                    "subagent_type": "developer",
                    "subagent_session_id": sub_sid,
                }
            ),
            "resultJson": result,
            "state": "completed",
            "startedAt": "2026-07-16T12:00:03.000Z",
            "completedAt": "2026-07-16T12:00:04.000Z",
        },
        subagent_session_id=sub_sid,
        parent_task_call_id=None,
    )


def _subagent_tool_entry(
    ts: int,
    tool_call: dict,
    sub_sid: str,
    parent_call: str,
) -> TimelineEntry:
    return TimelineEntry(
        ts=ts,
        role="subagent",
        kind="tool_call",
        turn=None,
        tool_call=tool_call,
        subagent_session_id=sub_sid,
        parent_task_call_id=parent_call,
    )


def write_timeline_entry(
    ts: int, file_path: str, content: str, call_id: str = "call_w"
) -> TimelineEntry:
    """子 agent 的 write 工具条目（file_state_rebuilder 重放输入）。"""
    return _subagent_tool_entry(
        ts,
        {
            "id": "tc_w",
            "turnId": "t_w",
            "toolCallId": call_id,
            "toolName": "write",
            "argsJson": json.dumps({"filePath": file_path, "content": content}),
            "resultJson": "",
            "state": "completed",
            "startedAt": "2026-07-16T12:00:00.000Z",
            "completedAt": "2026-07-16T12:00:00.100Z",
        },
        "ses_sub_001",
        "call_main_task_1",
    )


def edit_timeline_entry(
    ts: int, file_path: str, old: str, new: str, call_id: str = "call_e"
) -> TimelineEntry:
    """子 agent 的 edit 工具条目（oldString 精确替换）。"""
    return _subagent_tool_entry(
        ts,
        {
            "id": "tc_e",
            "turnId": "t_e",
            "toolCallId": call_id,
            "toolName": "edit",
            "argsJson": json.dumps({"filePath": file_path, "oldString": old, "newString": new}),
            "resultJson": "",
            "state": "completed",
            "startedAt": "2026-07-16T12:00:01.000Z",
            "completedAt": "2026-07-16T12:00:01.100Z",
        },
        "ses_sub_001",
        "call_main_task_1",
    )


def bash_timeline_entry(
    ts: int, command: str, call_id: str = "call_b", result: str = ""
) -> TimelineEntry:
    """子 agent 的 bash 工具条目（命令重放 / 分类输入）。"""
    return _subagent_tool_entry(
        ts,
        {
            "id": "tc_b",
            "turnId": "t_b",
            "toolCallId": call_id,
            "toolName": "bash",
            "argsJson": json.dumps({"command": command, "description": ""}),
            "resultJson": result,
            "state": "completed",
            "startedAt": "2026-07-16T12:00:02.000Z",
            "completedAt": "2026-07-16T12:00:02.100Z",
        },
        "ses_sub_001",
        "call_main_task_1",
    )


def multi_edit_timeline_entry(
    ts: int,
    file_path: str,
    edits: list[dict],
    call_id: str = "call_me",
) -> TimelineEntry:
    """子 agent 的 multi_edit 工具条目（同文件多组替换按序应用）。"""
    return _subagent_tool_entry(
        ts,
        {
            "id": "tc_me",
            "turnId": "t_me",
            "toolCallId": call_id,
            "toolName": "multi_edit",
            "argsJson": json.dumps({"file_path": file_path, "edits": edits}),
            "resultJson": "",
            "state": "completed",
            "startedAt": "2026-07-16T12:00:01.500Z",
            "completedAt": "2026-07-16T12:00:01.600Z",
        },
        "ses_sub_001",
        "call_main_task_1",
    )


def dialect_tool_timeline_entry(ts: int, tool_name: str, args: dict, call_id: str) -> TimelineEntry:
    """任意工具名/参数键的子 agent 条目（cpx 方言参数键测试用）。"""
    return _subagent_tool_entry(
        ts,
        {
            "id": f"tc_{call_id}",
            "turnId": f"t_{call_id}",
            "toolCallId": call_id,
            "toolName": tool_name,
            "argsJson": json.dumps(args),
            "resultJson": "",
            "state": "completed",
            "startedAt": "2026-07-16T12:00:02.000Z",
            "completedAt": "2026-07-16T12:00:02.100Z",
        },
        "ses_sub_001",
        "call_main_task_1",
    )


def turn_timeline_entry(
    ts: int,
    turn_id: str,
    *,
    idx: int = 0,
    role: str = "user",
    content: str = "",
    finish: str | None = None,
    sub_sid: str | None = None,
    at: str | None = None,
    parent_call: str | None = None,
) -> TimelineEntry:
    """对话回合条目：turn 字典按给定字段组装（缺省字段不写入字典）。"""
    turn: dict[str, Any] = {
        "id": turn_id,
        "turnIndex": idx,
        "role": role,
        "content": content,
    }
    if finish is not None:
        turn["finishReason"] = finish
    turn["isSubagent"] = sub_sid is not None
    if sub_sid is not None:
        turn["subagentSessionId"] = sub_sid
    if at is not None:
        turn["createdAt"] = at
    return TimelineEntry(
        ts=ts,
        role="subagent" if sub_sid is not None else "main",
        kind="turn",
        turn=turn,
        tool_call=None,
        subagent_session_id=sub_sid or "",
        parent_task_call_id=parent_call,
    )


def tool_timeline_entry(
    ts: int,
    row_id: str,
    turn_id: str,
    call_id: str,
    tool_name: str,
    args: dict | str,
    *,
    result: str = "",
    start: str | None = None,
    end: str | None = None,
    entry_role: str = "main",
    entry_sub_sid: str = "",
    parent_call: str | None = None,
) -> TimelineEntry:
    """工具调用条目：tool_call 字典 + 条目归属（role/sub_sid/parent_call）。"""
    return TimelineEntry(
        ts=ts,
        role=entry_role,
        kind="tool_call",
        turn=None,
        tool_call={
            "id": row_id,
            "turnId": turn_id,
            "toolCallId": call_id,
            "toolName": tool_name,
            "argsJson": args if isinstance(args, str) else json.dumps(args),
            "resultJson": result,
            "state": "completed",
            "startedAt": start,
            "completedAt": end,
        },
        subagent_session_id=entry_sub_sid,
        parent_task_call_id=parent_call,
    )


# ── opencode.db（part/message 行 + 假库 / source schema 模板库） ──


class OpencodePartRow(NamedTuple):
    """part 表插入行（列序同建表语句）。"""

    id: str
    message_id: str
    session_id: str
    time_created: int
    time_updated: int
    data: str


def opencode_text_part_row(pid: str, sid: str, tc: int, text: str) -> OpencodePartRow:
    """文本 part 行（message_id 始终为 m_<pid>）。"""
    return OpencodePartRow(pid, f"m_{pid}", sid, tc, tc, json.dumps({"type": "text", "text": text}))


def opencode_task_part_row(
    pid: str, sid: str, tc: int, sub_session_id: str, output_text: str = ""
) -> OpencodePartRow:
    """task 工具 part 行（l2_reader 形态：agentType 输入 + 可定制输出）。"""
    return OpencodePartRow(
        pid,
        f"m_{pid}",
        sid,
        tc,
        tc,
        json.dumps(
            {
                "type": "tool",
                "tool": "task",
                "callID": f"call_{pid}",
                "state": {
                    "status": "completed",
                    "input": {"agentType": "developer"},
                    "output": (
                        f"<task_result session_id='{sub_session_id}'>{output_text}</task_result>"
                    ),
                },
            }
        ),
    )


def opencode_done_task_part_row(pid: str, sid: str, tc: int, sub_id: str) -> OpencodePartRow:
    """task 工具 part 行（traceback_runner 形态：c_<pid> 调用 id + done 输出）。"""
    part = {
        "type": "tool",
        "tool": "task",
        "callID": f"c_{pid}",
        "state": {
            "status": "completed",
            "input": {},
            "output": f"<task_result session_id='{sub_id}'>done</task_result>",
        },
    }
    return OpencodePartRow(pid, f"m_{pid}", sid, tc, tc, json.dumps(part))


def opencode_user_message_row(msg_id: str, sid: str, tc: int) -> tuple:
    """user 角色 message 行（opencode message.data 形态）。"""
    return (msg_id, sid, tc, tc, json.dumps({"role": "user", "time": {"created": tc}}))


def opencode_assistant_message_row(
    msg_id: str, sid: str, tc: int, parent_id: str | None = None
) -> tuple:
    """assistant 角色message 行（可选 parentID 链）。"""
    data: dict[str, object] = {"role": "assistant", "time": {"created": tc}}
    if parent_id:
        data["parentID"] = parent_id
    return (msg_id, sid, tc, tc, json.dumps(data))


_OPENCODE_SESSION_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "project_id TEXT",
    "parent_id TEXT",
    "slug TEXT",
    "directory TEXT",
    "title TEXT",
    "version TEXT",
    "share_url TEXT",
    "summary_additions INTEGER",
    "summary_deletions INTEGER",
    "summary_files INTEGER",
    "summary_diffs TEXT",
    "revert INTEGER",
    "permission TEXT",
    "time_created INTEGER",
    "time_updated INTEGER",
    "time_compacting INTEGER",
    "time_archived INTEGER",
    "workspace_id TEXT",
    "path TEXT",
    "agent TEXT",
    "model TEXT",
    "cost REAL",
    "tokens_input INTEGER",
    "tokens_output INTEGER",
    "tokens_reasoning INTEGER",
    "tokens_cache_read INTEGER",
    "tokens_cache_write INTEGER",
    "metadata TEXT",
)
_TRACEBACK_SESSION_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "parent_id TEXT",
    "title TEXT",
    "directory TEXT",
    "time_created INTEGER",
    "time_updated INTEGER",
    "agent TEXT",
    "model TEXT",
    "version TEXT",
)
_PART_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "message_id TEXT",
    "session_id TEXT",
    "time_created INTEGER",
    "time_updated INTEGER",
    "data TEXT",
)
_MESSAGE_TABLE_COLUMNS = (
    "id TEXT PRIMARY KEY",
    "session_id TEXT",
    "time_created INTEGER",
    "time_updated INTEGER",
    "data TEXT",
)
# 供 INSERT 用的纯列名序列
_PART_COLUMNS = tuple(c.split()[0] for c in _PART_TABLE_COLUMNS)
_MESSAGE_COLUMNS = tuple(c.split()[0] for c in _MESSAGE_TABLE_COLUMNS)


def build_opencode_db(
    xdg_root: Path,
    *,
    sessions: list[dict],
    parts: list[OpencodePartRow | tuple],
    messages: list[tuple] | None = None,
    traceback_session_schema: bool = False,
) -> Path:
    """造一个可读写的假 opencode.db（session/part/message 三表）。

    ``sessions`` 为列值字典（INSERT 列随字典键走）；``parts`` /
    ``messages`` 为列序对齐建表语句的元组。``traceback_session_schema``
    为 True 时 session 表用 traceback 重放子集列（与实时快照库对齐）。
    """
    dbdir = xdg_root / "opencode"
    dbdir.mkdir(parents=True, exist_ok=True)
    db = dbdir / "opencode.db"
    session_columns = (
        _TRACEBACK_SESSION_TABLE_COLUMNS
        if traceback_session_schema
        else _OPENCODE_SESSION_TABLE_COLUMNS
    )
    con = sqlite3.connect(db)
    con.execute(_create_table_sql("session", session_columns))
    for s in sessions:
        cols = ", ".join(s.keys())
        ph = ", ".join("?" for _ in s)
        con.execute(f"INSERT INTO session ({cols}) VALUES ({ph})", list(s.values()))
    con.execute(_create_table_sql("part", _PART_TABLE_COLUMNS))
    for p in parts:
        con.execute(_insert_sql("part", _PART_COLUMNS), p)
    con.execute(_create_table_sql("message", _MESSAGE_TABLE_COLUMNS))
    for m in messages or []:
        con.execute(_insert_sql("message", _MESSAGE_COLUMNS), m)
    con.commit()
    con.close()
    return db


_OPENCODE_STUB_TABLES = (
    "todo",
    "event",
    "event_sequence",
    "session_input",
    "session_message",
    "session_share",
    "session_context_epoch",
)


def build_source_opencode_db(
    tmp_path: Path,
    *,
    migrations_pk: bool = True,
    migrations_hash: str = "fakehash",
    full_project_table: bool = False,
) -> Path:
    """建一个最小 source opencode.db（schema 模板，供 fork db schema-only 初始化）。

    ``__drizzle_migrations`` 让 opencode 不重跑 CREATE TABLE；project 表
    按 ``full_project_table`` 取完整列或最小列（session.project_id 外键目标）。
    """
    src = tmp_path / "source_opencode" / "opencode.db"
    src.parent.mkdir(parents=True)
    con = sqlite3.connect(src)
    migrations_id = "id TEXT PRIMARY KEY" if migrations_pk else "id TEXT"
    con.execute(
        f"CREATE TABLE __drizzle_migrations ({migrations_id}, hash TEXT, created_at INTEGER)"
    )
    con.execute("INSERT INTO __drizzle_migrations VALUES ('0001', ?, 0)", (migrations_hash,))
    con.execute(_create_table_sql("session", _OPENCODE_SESSION_TABLE_COLUMNS))
    con.execute(_create_table_sql("message", _MESSAGE_TABLE_COLUMNS))
    con.execute(_create_table_sql("part", _PART_TABLE_COLUMNS))
    for table in _OPENCODE_STUB_TABLES:
        con.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    if full_project_table:
        con.execute(
            "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT, vcs TEXT, "
            "icon_url TEXT, icon_color TEXT, time_created INTEGER, time_updated INTEGER, "
            "time_initialized INTEGER, sandboxes TEXT, commands TEXT, icon_url_override TEXT)"
        )
    else:
        con.execute("CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    return src


def can_open_readonly(path: Path) -> bool:
    """探测一个 db 文件是否可被只读打开（防 macOS TCC/隔离属性致 skip 不触发）。

    ``is_file()`` 返回 True 但 ``sqlite3.connect`` 仍可能抛 ``OperationalError``
    （macOS Downloads 受 TCC 保护 / ``com.apple.quarantine`` 标记时）。此函数
    显式试连一次，捕获则返回 False 让上层 ``pytest.skip``。
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        con.close()
        return True
    except (sqlite3.Error, OSError):
        return False
