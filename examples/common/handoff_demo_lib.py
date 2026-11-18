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

"""subagent 交接演示（subagent-*-handoff）的 reset/verify 脚本共享的工具函数。"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class HandoffVerifyError(RuntimeError):
    """verify 脚本校验失败：消息即用户可见错误文本，由各 verify 的 main 转退出码。"""


def configure_stdout_logging(logger: logging.Logger) -> None:
    """演示脚本统一以纯文本格式把日志打到 stdout。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def remove_handoff_artifacts(root: Path, names: Sequence[str], globs: Sequence[str]) -> None:
    """删除固定名字的文件/目录，以及通配模式命中的交接演示产物。"""
    targets = [root / name for name in names]
    for pattern in globs:
        targets.extend(root.glob(pattern))
    for target in targets:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def load_workflow_config(root: Path) -> dict:
    """读取示例目录下的 workflow-controller.yaml。"""
    return yaml.safe_load((root / "workflow-controller.yaml").read_text(encoding="utf-8"))


def load_earliest_manifest(root: Path, missing_message: str) -> tuple[dict, Path]:
    """找最早写入的整组交接清单；缺失时以 missing_message 抛 HandoffVerifyError。"""
    manifests = list((root / ".workflow-controller" / "handoffs").rglob("manifest.json"))
    if not manifests:
        raise HandoffVerifyError(missing_message)
    manifest_path = min(manifests, key=lambda path: path.stat().st_mtime_ns)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, manifest_path


def manifest_mtime_iso(manifest_path: Path) -> str:
    """交接清单的修改时间（aware-UTC ISO），作为事件查询的截止点。

    SQL 侧按字符串比较，必须与事件库的 aware-UTC 时间戳同格式，
    否则 naive 本地墙钟与 UTC 墙钟错位一个时区差，截止过滤失效。
    """
    return datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=UTC).isoformat()


def connect_events_db(root: Path) -> sqlite3.Connection:
    """以只读方式打开示例目录的事件库。"""
    db = root / ".workflow-controller" / "events.db"
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def fetch_context_events(
    conn: sqlite3.Connection, session_id: str, cutoff: str
) -> list[tuple[str, str]]:
    """取截止时间前某会话的主代理/子代理上下文事件。"""
    return conn.execute(
        "SELECT event_type, data FROM events WHERE session_id = ? "
        "AND event_type IN ('main_context', 'subagent_context') AND timestamp <= ?",
        (session_id, cutoff),
    ).fetchall()


def fetch_subagent_starts(conn: sqlite3.Connection, session_id: str) -> list[tuple[str]]:
    """取某会话的全部 subagent_start 事件数据。"""
    return conn.execute(
        "SELECT data FROM events WHERE session_id = ? AND event_type = 'subagent_start'",
        (session_id,),
    ).fetchall()


def successor_session_id(conn: sqlite3.Connection, session_id: str) -> str | None:
    """沿 handoff_relations 找 session 的后继主会话。"""
    row = conn.execute(
        "SELECT to_session_id FROM handoff_relations WHERE from_session_id = ? ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None
