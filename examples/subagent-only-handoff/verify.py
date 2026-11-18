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

"""Check the first child-only handoff, optionally through redispatch and completion."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

import handoff_demo_lib as demo_lib

logger = logging.getLogger(__name__)


def _check_handoff_scope(manifest: dict, root: Path, main_threshold: float) -> None:
    """校验仅子代理整组交接：无主代理交接产物，条目为 reader/auditor/reporter 三名。"""
    if manifest["main_handoff"]:
        raise demo_lib.HandoffVerifyError(f"主代理也越线了：{manifest['main_handoff']}")
    if list(root.glob("*-handoff-*-doc")):
        raise demo_lib.HandoffVerifyError(
            f"发现主代理单独交接文档，检查本项目主代理阈值是否为 {main_threshold:g}%"
        )
    entries = manifest["entries"]
    expected = {"reader", "auditor", "reporter"}
    if len(entries) != 3 or {entry["agent_type"] for entry in entries} != expected:
        raise demo_lib.HandoffVerifyError(
            f"预期 reader、auditor、reporter 三名子代理，实际：{entries}"
        )
    if (
        any(not Path(entry["handoff"]).is_file() for entry in entries)
        or not Path(manifest["done"]).is_file()
    ):
        raise demo_lib.HandoffVerifyError("三份子代理交接文档或完成信号尚未写入")


def _collect_handoff_events(root: Path, manifest: dict, manifest_path: Path) -> tuple[list, list]:
    """原主 session 的子代理启动事件与上下文事件（截止首次交接时刻）。"""
    first_handoff_at = demo_lib.manifest_mtime_iso(manifest_path)
    with demo_lib.connect_events_db(root) as conn:
        starts = demo_lib.fetch_subagent_starts(conn, manifest["parent_session_id"])
        contexts = demo_lib.fetch_context_events(
            conn, manifest["parent_session_id"], first_handoff_at
        )
    return starts, contexts


def _check_context_limits(
    contexts: list, manifest: dict, child_threshold: float, main_threshold: float
) -> None:
    """校验主代理未越线、仅 reader 越线。"""
    entries = manifest["entries"]
    old_ids = {entry["old_agent_id"] for entry in entries}
    main_pcts = [
        json.loads(data).get("percentage", 0) for kind, data in contexts if kind == "main_context"
    ]
    if not main_pcts or max(main_pcts) >= main_threshold:
        raise demo_lib.HandoffVerifyError(f"主代理未保持低于 {main_threshold:g}%：{main_pcts}")
    reader_id = next(entry["old_agent_id"] for entry in entries if entry["agent_type"] == "reader")
    reader_pcts = [
        json.loads(data).get("percentage", 0)
        for kind, data in contexts
        if kind == "subagent_context" and json.loads(data).get("agent_id") == reader_id
    ]
    if not reader_pcts or max(reader_pcts) <= child_threshold:
        raise demo_lib.HandoffVerifyError(f"reader 未超过 {child_threshold:g}%：{reader_pcts}")
    other_ids = old_ids - {reader_id}
    other_pcts = [
        json.loads(data).get("percentage", 0)
        for kind, data in contexts
        if kind == "subagent_context" and json.loads(data).get("agent_id") in other_ids
    ]
    if other_pcts and max(other_pcts) > child_threshold:
        raise demo_lib.HandoffVerifyError(f"auditor 或 reporter 也越线了：{other_pcts}")
    logger.info(f"PASS 首次交接：主代理未越线；原 session {manifest['parent_session_id']}")
    logger.info(f"PASS reader 超过 {child_threshold:g}%，主代理保持低于 {main_threshold:g}%")
    logger.info("PASS 三名子代理的交接文档和完成信号已写入")


def _check_redispatch(starts: list, manifest: dict) -> None:
    """校验原主 session 已重新派发全部三名子代理（新代理不在旧 id 集合中）。"""
    old_ids = {entry["old_agent_id"] for entry in manifest["entries"]}
    new_types = {
        json.loads(data).get("agent_type")
        for (data,) in starts
        if json.loads(data).get("agent_id") not in old_ids
    }
    if not {"reader", "auditor", "reporter"}.issubset(new_types):
        raise demo_lib.HandoffVerifyError(f"原主 session 尚未重新派发全部三个类型：{new_types}")
    logger.info("PASS 原主 session 已重新派发三名子代理")


def _check_final_outputs(root: Path) -> None:
    """校验续跑产物：三份文件齐全且 result.md 列齐十个异常标记。"""
    if any(not (root / name).is_file() for name in ("result.md", "audit.md", "report.md")):
        raise demo_lib.HandoffVerifyError("续跑产物 result.md、audit.md 或 report.md 尚未生成")
    report = (root / "result.md").read_text(encoding="utf-8")
    if any(f"ALERT-{index:02d}" not in report for index in range(1, 11)):
        raise demo_lib.HandoffVerifyError("result.md 没有列齐十个异常标记")
    logger.info("PASS result.md 包含十个异常标记，audit.md 与 report.md 均已生成")


def verify(root: Path, level: str = "handoff") -> None:
    limits = demo_lib.load_workflow_config(root)
    child_threshold = float(limits["subagent_context_threshold_pct"])
    main_threshold = float(limits["context_threshold_pct"])
    manifest, manifest_path = demo_lib.load_earliest_manifest(
        root, "未找到整组交接清单；子代理尚未越线或交接尚未完成"
    )
    _check_handoff_scope(manifest, root, main_threshold)
    starts, contexts = _collect_handoff_events(root, manifest, manifest_path)
    _check_context_limits(contexts, manifest, child_threshold, main_threshold)
    if level == "handoff":
        return

    _check_redispatch(starts, manifest)
    if level == "redispatched":
        return

    _check_final_outputs(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    level = parser.add_mutually_exclusive_group()
    level.add_argument(
        "--redispatched", action="store_true", help="同时检查原主会话重新派发三名子代理"
    )
    level.add_argument("--full", action="store_true", help="同时检查重新派发和最终产物")
    args = parser.parse_args()
    try:
        verify(
            Path(__file__).resolve().parent,
            "full" if args.full else "redispatched" if args.redispatched else "handoff",
        )
    except demo_lib.HandoffVerifyError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    demo_lib.configure_stdout_logging(logger)
    raise SystemExit(main())
