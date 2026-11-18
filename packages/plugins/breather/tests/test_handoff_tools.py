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

"""handoff_tools.py 的确定性工具单测。

重点：``extract_worker_narrative`` 从 Workflow Controller 事件库（``.workflow-controller/events.db``）
重建 worker 的推理过程——三 runtime（claude_sdk / claude_cli / opencode）都把
worker 的 ``ASSISTANT_MESSAGE`` / ``LOG_EVAL`` 事件写进同一张 events 表，
所以读事件库是通用数据源，替代已废弃的 ``iter_*_worker.stdout.log``
（无任何 runtime 写它，migbot 残留）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from workflow_controller.plugins.breather.scripts.handoff_tools import (
    _narrative_from_events,
    extract_worker_narrative,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── _narrative_from_events：纯函数，输入原始行 ────────────────────────


class TestNarrativeFromEvents:
    @staticmethod
    def test_extracts_assistant_text_blocks_in_order() -> None:
        rows = [
            ("assistant_message", {"content": [{"type": "text", "text": "先读 plan"}]}),
            ("assistant_message", {"content": [{"type": "text", "text": "再写算子"}]}),
        ]
        narrative = _narrative_from_events(rows)
        assert "先读 plan" in narrative
        assert "再写算子" in narrative
        # 顺序保留：先读在前
        assert narrative.index("先读 plan") < narrative.index("再写算子")

    @staticmethod
    def test_skips_non_text_blocks_and_tool_events() -> None:
        rows = [
            (
                "assistant_message",
                {
                    "content": [
                        {"type": "thinking", "thinking": "内部推理不该进 narrative"},
                        {"type": "text", "text": "可见结论"},
                    ]
                },
            ),
            ("tool_use", {"name": "Read", "input": {"file_path": "x.py"}}),
            ("tool_result", {"content": "raw file dump"}),
        ]
        narrative = _narrative_from_events(rows)
        assert "可见结论" in narrative
        assert "内部推理不该进 narrative" not in narrative
        assert "raw file dump" not in narrative

    @staticmethod
    def test_includes_reviewer_summary_with_iteration_tag() -> None:
        rows = [
            ("log_eval", {"summary": "产物缺失，需补 main.py", "iteration": 3}),
        ]
        narrative = _narrative_from_events(rows)
        assert "[Reviewer/iter 3]" in narrative
        assert "产物缺失，需补 main.py" in narrative

    @staticmethod
    def test_empty_rows_returns_empty_string() -> None:
        assert _narrative_from_events([]) == ""

    @staticmethod
    def test_skips_log_eval_with_blank_summary() -> None:
        rows = [
            ("log_eval", {"summary": "", "iteration": 1}),
            ("log_eval", {"summary": "   ", "iteration": 2}),
        ]
        assert _narrative_from_events(rows) == ""


# ── extract_worker_narrative：经真实事件库（temp sqlite）─────────────


def _build_events_db(project_dir: Path, rows: list[tuple[str, str, int, dict[str, Any]]]) -> Path:
    """在 {project_dir}/.workflow-controller/events.db 建表并插入事件行。

    rows: (session_id, event_type, iteration, data) —— id/timestamp 自动生成。
    """
    db_path = project_dir / ".workflow-controller" / "events.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, iteration INTEGER NOT NULL, "
            "timestamp TEXT NOT NULL, event_type TEXT NOT NULL, data TEXT NOT NULL, "
            "created_at TEXT DEFAULT (datetime('now')))"
        )
        for i, (session_id, event_type, iteration, data) in enumerate(rows):
            conn.execute(
                "INSERT INTO events (id, session_id, iteration, timestamp, event_type, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"evt-{i}",
                    session_id,
                    iteration,
                    f"2026-06-29T00:00:{i:02d}",
                    event_type,
                    json.dumps(data, ensure_ascii=False),
                ),
            )
    return db_path


class TestExtractWorkerNarrativeFromDb:
    @staticmethod
    def test_reconstructs_narrative_from_event_store(tmp_path: Path) -> None:
        sid = "worker-abc"
        _build_events_db(
            tmp_path,
            [
                (
                    sid,
                    "assistant_message",
                    1,
                    {"content": [{"type": "text", "text": "决定拆成两步"}]},
                ),
                (sid, "tool_use", 1, {"name": "Write", "input": {}}),
                (
                    sid,
                    "assistant_message",
                    2,
                    {"content": [{"type": "text", "text": "第二步遇到 lint 报错"}]},
                ),
                (sid, "log_eval", 2, {"summary": "lint 未过，继续修", "iteration": 2}),
            ],
        )

        narrative = extract_worker_narrative(str(tmp_path), sid)

        assert "决定拆成两步" in narrative
        assert "第二步遇到 lint 报错" in narrative
        assert "[Reviewer/iter 2]" in narrative
        assert "lint 未过，继续修" in narrative
        # 工具原始数据不进 narrative
        assert "Write" not in narrative

    @staticmethod
    def test_isolates_by_session_id(tmp_path: Path) -> None:
        _build_events_db(
            tmp_path,
            [
                (
                    "other-session",
                    "assistant_message",
                    1,
                    {"content": [{"type": "text", "text": "别人的对话"}]},
                ),
            ],
        )
        narrative = extract_worker_narrative(str(tmp_path), "worker-abc")
        assert narrative.startswith("[空]")

    @staticmethod
    def test_missing_db_returns_empty_marker(tmp_path: Path) -> None:
        # tmp_path 存在但没有 .workflow-controller/events.db
        narrative = extract_worker_narrative(str(tmp_path), "worker-abc")
        assert narrative.startswith("[空]")

    @staticmethod
    def test_missing_args_returns_marker(tmp_path: Path) -> None:
        assert extract_worker_narrative("", "sid").startswith("[空]")
        assert extract_worker_narrative(str(tmp_path), "").startswith("[空]")

    @staticmethod
    def test_rows_but_no_text_returns_marker(tmp_path: Path) -> None:
        sid = "worker-abc"
        _build_events_db(
            tmp_path,
            [
                (sid, "tool_use", 1, {"name": "Read", "input": {}}),
                (sid, "tool_result", 1, {"content": "dump"}),
            ],
        )
        narrative = extract_worker_narrative(str(tmp_path), sid)
        assert narrative.startswith("[空]")


@pytest.mark.asyncio
async def test_router_writes_daemon_db_and_handoff_reads_it(tmp_path: Path) -> None:
    """单库直写：worker 事件落守护进程库（不再按 cwd 分库），handoff_tools
    从该库读到 narrative。
    """
    from workflow_controller.session.events import Event, EventType
    from workflow_controller.store.event_router import EventStoreRouter

    boot = tmp_path / "boot-proj"
    router = EventStoreRouter(default_project_dir=boot)
    sid = "worker-sess"
    await router.store(
        Event.create(
            session_id=sid,
            iteration=0,
            event_type=EventType.ASSISTANT_MESSAGE,
            data={"content": [{"type": "text", "text": "我在库里"}]},
        ),
    )

    narrative = extract_worker_narrative(str(boot), sid)
    assert "我在库里" in narrative

    # worker cwd 下没有库：单库化后事件不落 worker 工程，读库目录必须由
    # prompt 注入守护进程侧（见 HandoffGenerator.build_handoff_agent_prompt）
    work = tmp_path / "work-proj"
    work.mkdir()
    assert extract_worker_narrative(str(work), sid).startswith("[空]")
