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

"""opencode 会话 → claude-jsonl 行流写出（.native 单元的转换轨）。

读入复用 opencode_db 读取轨（RawInteraction 流）；assistant 行带
message.id/model（provider 前缀）/usage、thinking 独立块、tool_result 带
is_error，顶层 duration_ms/stopReason 与 cc-db-turn 元数据信封；
system 行独立类型；无正文行不写；source 字段不写（Insight 侧生产者
水印）。输出确定性——行序=源消息序，不注入导出时间戳。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .opencode_db import (
    _subagent_name_from_title,
    _table_exists,
    _v2_session_row,
    read_session,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .types import RawInteraction, TokenUsage, ToolCallInfo

logger = logging.getLogger(__name__)

# content 由 _extract_content 以该前缀并入 reasoning（见 opencode_db）；
# 此处拆回独立块时按同款构造剥离
_THINKING_WRAP = "<thinking>{}</thinking>"


def write_opencode_session(db_path: Path, session_id: str, out_path: Path) -> bool:
    """opencode.db 会话转换为 claude-jsonl；会话不在库内返回 False。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return False
    try:
        interactions = read_session(conn, session_id)
    except Exception:
        return False
    finally:
        conn.close()
    if not interactions:
        return False
    out_path.write_text(
        "".join(
            json.dumps(line, ensure_ascii=False) + "\n"
            for line in _interactions_to_lines(interactions)
        ),
        encoding="utf-8",
    )
    return True


def _subagent_rows(db: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """按父会话定位子代理会话（v2 优先，回落 v1），与 read_session 双轨一致。"""
    if _v2_session_row(db, session_id) is not None:
        return db.execute(
            "SELECT id, title, version FROM session_v2 WHERE parent_id = ?",
            (session_id,),
        ).fetchall()
    if not _table_exists(db, "session"):
        return []
    return db.execute(
        "SELECT id, title, version FROM session WHERE parent_id = ?",
        (session_id,),
    ).fetchall()


def _write_subagent_unit(conn: sqlite3.Connection, sub: sqlite3.Row, session_dir: Path) -> bool:
    """写单个子代理的 .jsonl + .meta.json；读失败或空会话返回 False。"""
    try:
        interactions = read_session(conn, sub["id"])
    except Exception as e:
        logger.warning(
            "subagent export: read session %s failed: %s",
            sub["id"],
            e,
        )
        return False
    if not interactions:
        return False
    sub_name = _subagent_name_from_title(sub["title"]) or sub["title"] or sub["id"]
    sub_dir = session_dir / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / f"{sub['id']}.jsonl").write_text(
        "".join(
            json.dumps(line, ensure_ascii=False) + "\n"
            for line in _interactions_to_lines(interactions)
        ),
        encoding="utf-8",
    )
    sub_meta = {
        "toolUseId": None,
        "name": sub_name,
        "agentType": None,
        "x_context_bay": {
            "schema": "cc-subagent-meta",
            "version": 1,
            "data": {
                "toolUseId": None,
                "name": sub_name,
                "agentType": None,
                "subagentSessionId": sub["id"],
            },
        },
    }
    (sub_dir / f"{sub['id']}.meta.json").write_text(
        json.dumps(sub_meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def write_opencode_subagents(db_path: Path, session_id: str, session_dir: Path) -> int:
    """把 session 的子代理会话写到 session_dir/subagents/ 下，返回写入的子代理数。

    子代理按 opencode.db ``parent_id = session_id`` 定位（v2 库走
    session_v2，v1 库走 session，与 read_session 同轨）；每个子代理写一份
    与主会话同形状的 .jsonl + cc-subagent-meta 的 .meta.json。任一步失败
    不抛——子代理是附属产物，主会话单元不受影响；但会记日志，避免 schema
    未命中时静默产出 0 个子代理。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.warning("subagent export: cannot open %s: %s", db_path, e)
        return 0
    written = 0
    try:
        subs = _subagent_rows(conn, session_id)
        if not subs:
            return 0
        for sub in subs:
            if _write_subagent_unit(conn, sub, session_dir):
                written += 1
    except Exception as e:
        logger.warning("subagent export failed for session %s: %s", session_id, e)
        return written
    finally:
        conn.close()
    return written


def _interactions_to_lines(interactions: list[RawInteraction]) -> list[dict]:
    """RawInteraction 流 → claude-jsonl 行流（主会话与子代理共用）。"""
    lines: list[dict] = []
    for interaction in interactions:
        if interaction.role == "assistant":
            lines.append(_assistant_line(interaction))
            tool_results = _tool_result_line(interaction)
            if tool_results is not None:
                lines.append(tool_results)
        elif interaction.role in ("user", "system") and interaction.content:
            lines.append(
                {
                    "type": interaction.role,
                    "message": {"role": interaction.role, "content": interaction.content},
                    "timestamp": interaction.timestamp,
                }
            )
    return lines


def _text_only(interaction: RawInteraction) -> str | None:
    """content 剥离 <thinking> 段后的纯文本（无 reasoning 或非拼接态原样返回）。"""
    content = interaction.content
    if not content or not interaction.reasoning:
        return content
    prefix = _THINKING_WRAP.format(interaction.reasoning)
    if content.startswith(prefix):
        # 段间分隔符兼容 \n 与 \x14（_extract_content 的 chr(10+10) 实为 DC4）
        return content[slice(len(prefix), None)].lstrip("\n\x14") or None
    return content


def _assistant_line(interaction: RawInteraction) -> dict:
    """assistant 行：thinking/text/tool_use 块合成 content 列表。"""
    blocks: list[dict] = []
    if interaction.reasoning:
        blocks.append({"type": "thinking", "thinking": interaction.reasoning})
    text = _text_only(interaction)
    if text:
        blocks.append({"type": "text", "text": text})
    for call in interaction.tool_calls or []:
        try:
            args = json.loads(call.args_json) if call.args_json else {}
        except ValueError:
            args = {"raw": call.args_json}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.tool_call_id,
                "name": call.tool_name,
                "input": args,
            }
        )
    message: dict = {
        "id": interaction.id,
        "role": "assistant",
        "content": blocks if blocks else None,
        "model": interaction.model,
        "usage": _usage(interaction.usage),
    }
    line: dict = {
        "type": "assistant",
        "message": message,
        "timestamp": interaction.timestamp,
    }
    if interaction.latency is not None:
        line["duration_ms"] = int(interaction.latency)
    if interaction.finish_reason is not None:
        line["stopReason"] = interaction.finish_reason
    line["x_context_bay"] = _turn_envelope(interaction)
    return line


def _usage(usage: TokenUsage | None) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input,
        "output_tokens": usage.output,
        "cache_read_input_tokens": usage.cache_read or None,
        "cache_creation_input_tokens": usage.cache_write or None,
    }


def _turn_envelope(interaction: RawInteraction) -> dict:
    """assistant 行的 cc-db-turn 信封（Insight 侧 turn 级元数据）。"""
    data: dict = {}
    if interaction.latency is not None:
        data["latencyMs"] = int(interaction.latency)
    if interaction.finish_reason is not None:
        data["stopReason"] = interaction.finish_reason
    if interaction.usage and interaction.usage.reasoning:
        data["reasoningTokens"] = interaction.usage.reasoning
    if interaction.model_id:
        data["modelId"] = interaction.model_id
    if interaction.provider_id:
        data["providerId"] = interaction.provider_id
    calls = [_tool_call_meta(c) for c in interaction.tool_calls or []]
    if calls:
        data["toolCalls"] = calls
    data["turnKind"] = "normal"
    return {"schema": "cc-db-turn", "version": 1, "data": data}


def _tool_call_meta(call: ToolCallInfo) -> dict:
    meta: dict = {"toolUseId": call.tool_call_id, "state": call.state}
    if call.started_ms is not None:
        meta["startedAt"] = datetime.fromtimestamp(
            call.started_ms / 1000,
            tz=UTC,
        ).isoformat()
    return meta


def _tool_result_line(interaction: RawInteraction) -> dict | None:
    """tool 结果行：携带 tool_use 的 assistant 行紧随的 user 行。"""
    results = [c for c in interaction.tool_calls or [] if c.result_json is not None]
    if not results:
        return None
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": c.tool_call_id,
                    "content": c.result_json,
                    "is_error": c.state not in ("completed", "ok"),
                }
                for c in results
            ],
        },
        "timestamp": interaction.timestamp,
    }
