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

"""Verify the first main-and-child handoff in the real Claude CLI example."""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from handoff_demo_lib import (
    HandoffVerifyError,
    configure_stdout_logging,
    connect_events_db,
    fetch_context_events,
    fetch_subagent_starts,
    load_earliest_manifest,
    load_workflow_config,
    manifest_mtime_iso,
    successor_session_id,
)

logger = logging.getLogger(__name__)


def _check_handoff_documents(manifest: dict) -> None:
    """校验首次整组交接清单：三名子代理条目、产物文件与主摘要引用齐全。"""
    entries = manifest["entries"]
    expected = {"researcher", "checker", "writer"}
    if len(entries) != 3 or {entry["agent_type"] for entry in entries} != expected:
        raise HandoffVerifyError("首次交接没有包含全部三名在途子代理")
    required = [
        manifest["main_handoff"],
        manifest["main_source"],
        manifest["main_events"],
        manifest["done"],
    ]
    required.extend(entry["handoff"] for entry in entries)
    if not all(path and Path(path).is_file() for path in required):
        raise HandoffVerifyError("主代理摘要、三份子代理摘要、源快照或完成信号尚未齐全")
    main_doc = Path(manifest["main_handoff"]).read_text(encoding="utf-8")
    if any(
        entry["handoff"] not in main_doc or entry["dispatch_prompt"] not in main_doc
        for entry in entries
    ):
        raise HandoffVerifyError("主代理摘要缺少子代理交接路径或重新派发指令")


def _check_thresholds(config: dict, contexts: list, entries: list) -> None:
    """校验主代理与 checker 均超过阈值（首次双越线）。"""
    main_pcts = [
        json.loads(data)["percentage"] for kind, data in contexts if kind == "main_context"
    ]
    checker_id = next(
        entry["old_agent_id"] for entry in entries if entry["agent_type"] == "checker"
    )
    checker_pcts = [
        json.loads(data)["percentage"]
        for kind, data in contexts
        if kind == "subagent_context" and json.loads(data).get("agent_id") == checker_id
    ]
    if not main_pcts or max(main_pcts) <= config["context_threshold_pct"]:
        raise HandoffVerifyError(f"主代理未超过 {config['context_threshold_pct']}%：{main_pcts}")
    if not checker_pcts or max(checker_pcts) <= config["subagent_context_threshold_pct"]:
        raise HandoffVerifyError(
            f"checker 未超过 {config['subagent_context_threshold_pct']}%：{checker_pcts}"
        )
    logger.info(f"PASS 首次双越线：主代理 {max(main_pcts):g}%，checker {max(checker_pcts):g}%")


def verify(root: Path, redispatched: bool = False) -> None:
    config = load_workflow_config(root)
    manifest, manifest_path = load_earliest_manifest(root, "尚未找到首次整组交接清单")
    _check_handoff_documents(manifest)

    parent = manifest["parent_session_id"]
    cutoff = manifest_mtime_iso(manifest_path)
    with connect_events_db(root) as conn:
        contexts = fetch_context_events(conn, parent, cutoff)
        _check_thresholds(config, contexts, manifest["entries"])
        logger.info("PASS 主代理摘要、三份子代理摘要及完成信号齐全")
        if not redispatched:
            return
        handoff = successor_session_id(conn, parent)
        successor = successor_session_id(conn, handoff) if handoff else None
        if not successor or successor == parent:
            raise HandoffVerifyError("尚未找到新主会话")
        starts = fetch_subagent_starts(conn, successor)
    types = {json.loads(data).get("agent_type") for (data,) in starts}
    if not {"researcher", "checker", "writer"}.issubset(types):
        raise HandoffVerifyError(f"新主会话尚未重新派发全部三名子代理：{types}")
    logger.info(f"PASS 新主会话 {successor} 已重新派发三名子代理")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redispatched", action="store_true")
    args = parser.parse_args()
    try:
        verify(Path(__file__).resolve().parent, args.redispatched)
    except HandoffVerifyError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    configure_stdout_logging(logger)
    raise SystemExit(main())
