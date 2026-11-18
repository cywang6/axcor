#!/usr/bin/env python3
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

"""handoff_tools.py — handoff agent 专用确定性工具（自包含 CLI）。

这是 breather handoff agent 盘点上一 session 进度时用的"地面真相"工具集。
**本文件是这些纯函数的唯一真相源**：`workflow_controller.plugins.breather.handoff`
从这里 re-export（保持 `from ...handoff import count_checkboxes` 不破）。

设计约束（务必维持）：
- **零 workflow_controller 依赖**——finalize_handoff 会把本文件复制到目标工程的
  `.workflow-controller/handoff_tools.py`，由 handoff agent 用 `python .workflow-controller/handoff_tools.py <cmd> ...`
  调用。那个进程的 cwd 是目标工程目录，PYTHONPATH 上没有 workflow_controller 包，
  所以本文件只能 import 标准库（json / re / pathlib / sys）。
- 每个子命令把结果打到 stdout：结构化结果输出 JSON（ensure_ascii=False），
  narrative 输出原始文本（供 agent 直接阅读）。

CLI 用法：
    python handoff_tools.py count_checkboxes <plan_file>
    python handoff_tools.py list_generated_files <base_dir> <glob_pattern>
    python handoff_tools.py extract_worker_narrative <project_dir> <session_id>
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# 强制 stdout 为 UTF-8，避免 Windows GBK 控制台把中文输出成乱码
with contextlib.suppress(AttributeError, ValueError):
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")


def _emit_stdout(message: str) -> None:
    output_logger = logging.getLogger(f"{__name__}.stdout")
    output_logger.setLevel(logging.INFO)
    output_logger.propagate = False
    output_handler = logging.StreamHandler(sys.stdout)
    output_handler.formatter = logging.Formatter("%(message)s")
    output_logger.handlers[:] = [output_handler]
    output_logger.info("%s", message)
    output_handler.close()


# ── 确定性工具（纯函数，无副作用，无 LLM）─────────────────────────


def count_checkboxes(plan_file: str) -> dict[str, Any]:
    """统计 markdown 文件中的 checkbox 状态。纯代码，不用 LLM。"""
    try:
        text = Path(plan_file).read_text(encoding="utf-8")
    except OSError:
        return {"error": f"无法读取 {plan_file}", "total": 0, "completed": 0, "pending": 0}
    completed = len(re.findall(r"^\s*- \[x\]", text, re.IGNORECASE | re.MULTILINE))
    pending = len(re.findall(r"^\s*- \[ \]", text, re.MULTILINE))
    return {"total": completed + pending, "completed": completed, "pending": pending}


def list_generated_files(base_dir: str, pattern: str) -> list[dict[str, Any]]:
    """列出 base_dir 下匹配 glob 的所有文件。返回 {path, lines, size}。纯代码。"""
    base = Path(base_dir)
    if not base.exists():
        return [{"error": f"目录不存在: {base_dir}"}]
    results = []
    for p in sorted(base.glob(pattern)):
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                lines = text.count("\n") + 1
                size = p.stat().st_size
            except OSError:
                lines, size = 0, 0
            results.append({"path": str(p), "lines": lines, "size": size})
    return results


def extract_worker_narrative(project_dir: str, session_id: str) -> str:
    """从 Workflow Controller 事件库重建刚结束的 worker 的推理过程。

    数据源：``{project_dir}/.workflow-controller/events.db``（sqlite）。三个 runtime
    （claude_sdk / claude_cli / opencode）都把 worker 的 ``ASSISTANT_MESSAGE``
    与 ``LOG_EVAL`` 事件写进同一张 events 表，所以读事件库是通用正解。按插入
    顺序取该 session 的事件，抽出 assistant 的 text block（分析/决策）+ reviewer
    的逐轮 summary，跳过 tool_use / tool_result 等原始数据。返回 ~1-3K tokens。

    历史注记：旧实现读 ``iter_*_worker.stdout.log``（migbot 时代的 CC 流式
    JSONL 日志），但 Workflow Controller 三个 driver 都不写该文件，故永远返回空。读事件库
    把早已存在的数据接上，且本文件零 workflow_controller 依赖（sqlite3 / json 均标准库），
    保持可被复制到目标工程独立运行。
    """
    if not project_dir or not session_id:
        return "[空] 缺少 project_dir 或 session_id。"

    db_path = _find_event_db(project_dir)
    if db_path is None:
        return f"[空] 未找到 session {session_id} 的事件库"

    rows = _read_session_rows(db_path, session_id)
    if not rows:
        return f"[空] session {session_id} 无 assistant/reviewer 事件"

    narrative = _narrative_from_events(rows)
    return narrative or "[空] 该 session 的事件中无 text 内容。"


def _assistant_texts(data: dict[str, Any]) -> list[str]:
    """assistant_message 事件 content 里的非空 text block 文本（保持块序）。"""
    texts: list[str] = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                texts.append(text)
    return texts


def _subagent_stop_line(
    data: dict[str, Any],
    subagent_starts: dict[str, str],
    agent_type: str,
    agent_id: str,
) -> str:
    """subagent_stop 的展示行；对应 start 行存在时拼接在前。"""
    tokens = data.get("total_tokens", 0)
    content = str(data.get("content", "") or "").strip()[:200]
    start_line = subagent_starts.pop(agent_id, "")
    line = f"[Subagent done] {agent_type} ({agent_id}), {tokens} tokens"
    if content:
        line += f" | {content}"
    if start_line:
        line = f"{start_line}\n{line}"
    return line


def _narrative_from_events(
    rows: list[tuple[str, dict[str, Any]]],
) -> str:
    """纯函数：从原始事件行（(event_type, data)，按时间序）抽 narrative。

    保留 ``assistant_message`` 的 text block + ``log_eval`` 的逐轮 summary +
    ``subagent`` 的启动/完成事件；跳过 thinking block 与 tool_use / tool_result
    等原始数据。无 workflow_controller 依赖，便于单测喂构造行 + 供 handoff agent 复用。
    """
    parts: list[str] = []
    subagent_starts: dict[str, str] = {}  # agent_id → start 行
    for event_type, data in rows:
        if event_type == "assistant_message":
            parts.extend(_assistant_texts(data))
        elif event_type == "log_eval":
            summary = str(data.get("summary") or "").strip()
            if summary:
                iteration = data.get("iteration")
                tag = f"[Reviewer/iter {iteration}]" if iteration is not None else "[Reviewer]"
                parts.append(f"{tag} {summary}")
        elif event_type in ("subagent_start", "subagent_stop"):
            agent_type = data.get("agent_type", "") or "unknown"
            agent_id = data.get("agent_id", "")[:12]
            if event_type == "subagent_start":
                subagent_starts[agent_id] = f"[Subagent start] {agent_type} ({agent_id})"
            else:
                parts.append(_subagent_stop_line(data, subagent_starts, agent_type, agent_id))
    return "\n\n---\n\n".join(parts)


def _find_event_db(project_dir: str) -> Path | None:
    """定位 {project_dir}/.workflow-controller/ 下的事件库（优先 events.db，回退任意 *.db）。"""
    base = Path(project_dir) / ".workflow-controller"
    if not base.is_dir():
        return None
    preferred = base / "events.db"
    if preferred.is_file():
        return preferred
    candidates = sorted(base.glob("*.db"))
    return candidates[0] if candidates else None


def _read_session_rows(
    db_path: Path,
    session_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    """读该 session 的事件行（按插入序），返回 (event_type, data_dict) 列表。"""
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        cur = conn.execute(
            "SELECT event_type, data FROM events WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        )
        rows: list[tuple[str, dict[str, Any]]] = []
        for event_type, data_json in cur.fetchall():
            try:
                data = json.loads(data_json) if data_json else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            rows.append((event_type, data))
        return rows


# ── CLI 分发 ─────────────────────────────────────────────────────

# 每个命令的参数个数（用于校验 + 友好报错）。narrative 输出原始文本，
# 其余输出 JSON。
_COMMANDS: dict[str, tuple[Callable[..., Any], int]] = {
    "count_checkboxes": (count_checkboxes, 1),
    "list_generated_files": (list_generated_files, 2),
    "extract_worker_narrative": (extract_worker_narrative, 2),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        _emit_stdout(__doc__)
        return 0 if len(argv) >= 2 else 2

    cmd = argv[1]
    entry = _COMMANDS.get(cmd)
    if entry is None:
        _emit_stdout(
            json.dumps(
                {"error": f"未知命令: {cmd}", "available": list(_COMMANDS)},
                ensure_ascii=False,
            )
        )
        return 2

    func, argc = entry
    call_args = argv[2:]
    if len(call_args) != argc:
        _emit_stdout(
            json.dumps(
                {"error": f"{cmd} 需要 {argc} 个参数，收到 {len(call_args)}", "got": call_args},
                ensure_ascii=False,
            )
        )
        return 2

    result = func(*call_args)
    # narrative 是纯文本，直接打印；结构化结果打 JSON
    if isinstance(result, str):
        _emit_stdout(result)
    else:
        _emit_stdout(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
