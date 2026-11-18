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

"""Deterministic `claude -p` fixture for the group handoff demo."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

NATIVE_ID = "demo-native-main"
SUCCESSOR_ID = "demo-native-successor"
WINDOW = 1_000_000
CHILDREN = ("researcher", "checker", "writer")

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def emit(payload: dict) -> None:
    logger.info("%s", json.dumps(payload, ensure_ascii=False))


def transcript_dir(native_id: str) -> Path:
    project = str(Path.cwd().resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / project / native_id / "subagents"


def write_main(native_id: str, tokens: int) -> None:
    path = transcript_dir(native_id).parent.with_suffix(".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {"input_tokens": tokens},
                        "stop_reason": "tool_use",
                    },
                }
            )
            + "\n"
        )


def write_child(
    agent_id: str, agent_type: str, tokens: int, *, finished: bool, native_id: str = NATIVE_ID
) -> None:
    path = transcript_dir(native_id) / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": f"{agent_type} 处理示例任务",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "attributionAgent": agent_type,
                    "message": {
                        "usage": {"input_tokens": tokens},
                        "stop_reason": "end_turn" if finished else "tool_use",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def record(kind: str, prompt: str, resume_id: str) -> None:
    path = Path(os.environ["WORKFLOW_CONTROLLER_DEMO_STATE"]) / "invocations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"kind": kind, "prompt": prompt, "resume_id": resume_id}, ensure_ascii=False)
            + "\n"
        )


def initial_worker(prompt: str) -> None:
    record("initial_worker", prompt, "")
    emit({"type": "system", "subtype": "init", "session_id": NATIVE_ID})
    write_main(NATIVE_ID, 30_000)
    start_barrier = threading.Barrier(len(CHILDREN))
    ready_barrier = threading.Barrier(len(CHILDREN))

    def child(index: int, agent_type: str) -> None:
        start_barrier.wait()
        write_child(f"old-{agent_type}", agent_type, 2_000 + index * 400, finished=False)
        ready_barrier.wait()
        if index == 1:
            signal = Path(os.environ["WORKFLOW_CONTROLLER_DEMO_STATE"]) / "demo-all-started.signal"
            deadline = time.monotonic() + 10
            while not signal.is_file():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Workflow Controller did not register all three subagents")
                time.sleep(0.02)
            write_child(f"old-{agent_type}", agent_type, 16_000, finished=False)
        time.sleep(60)

    for index, agent_type in enumerate(CHILDREN):
        threading.Thread(target=child, args=(index, agent_type), daemon=True).start()
    emit(
        {
            "type": "assistant",
            "message": {
                "model": "demo",
                "content": [
                    {"type": "text", "text": "三个子代理已并行派发，等待交接。"},
                ],
            },
        }
    )
    time.sleep(60)


def handoff_worker(prompt: str) -> None:
    record("handoff_agent", prompt, "")
    prompt_path = Path(prompt.removeprefix("读取 ").removesuffix(" 并严格执行。"))
    instructions = prompt_path.read_text(encoding="utf-8")
    if "盘点主代理已完成的协调工作" not in instructions:
        raise RuntimeError("交接指令缺少主代理盘点段")
    if "为每名在途子代理分别核对原任务" not in instructions:
        raise RuntimeError("交接指令缺少子代理核对段")
    manifest = json.loads((prompt_path.parent / "manifest.json").read_text(encoding="utf-8"))
    if len(manifest["entries"]) != 3:
        raise RuntimeError(f"manifest 应有 3 个条目：{manifest['entries']!r}")
    if not manifest["main_handoff"]:
        raise RuntimeError("manifest 缺 main_handoff")
    if not Path(manifest["main_source"]).is_file():
        raise RuntimeError(f"main_source 不存在：{manifest['main_source']}")
    if not Path(manifest["main_events"]).is_file():
        raise RuntimeError(f"main_events 不存在：{manifest['main_events']}")
    for entry in manifest["entries"]:
        source = Path(entry["source"])
        if not source.is_file():
            raise RuntimeError(f"条目 source 不存在：{source}")
        transcript = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        if transcript[-1]["message"]["stop_reason"] != "tool_use":
            raise RuntimeError(f"转录末帧 stop_reason 应为 tool_use：{source}")
        Path(entry["handoff"]).write_text(
            f"# {entry['agent_type']}\n原任务：{transcript[0]['message']['content']}。\n"
            "未完成：完成文件产物。\n",
            encoding="utf-8",
        )
    Path(manifest["main_handoff"]).write_text(
        "# Main Agent\n原任务：并行协调三名子代理。\n"
        + "\n".join(
            f"{entry['agent_type']}: {entry['handoff']}\n派发指令：{entry['dispatch_prompt']}"
            for entry in manifest["entries"]
        ),
        encoding="utf-8",
    )
    Path(manifest["done"]).write_text("done", encoding="utf-8")
    emit({"type": "system", "subtype": "init", "session_id": "demo-native-handoff"})
    emit(
        {
            "type": "result",
            "session_id": "demo-native-handoff",
            "usage": {"input_tokens": 80},
            "modelUsage": {"demo": {"contextWindow": WINDOW}},
        }
    )


def _emit_successor_dispatches(manifest: dict) -> None:
    """按清单条目逐个发出 Agent tool_use 事件（重新派发三名子代理）。"""
    for index, entry in enumerate(manifest["entries"]):
        emit(
            {
                "type": "assistant",
                "message": {
                    "model": "demo",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"dispatch-{index}",
                            "name": "Agent",
                            "input": {
                                "subagent_type": entry["agent_type"],
                                "prompt": entry["dispatch_prompt"],
                            },
                        }
                    ],
                },
            }
        )


def _write_successor_outputs(project: Path) -> None:
    """successor 主代理写三份交付文件与完成标记。"""
    write_main(SUCCESSOR_ID, 180)
    for agent_type in CHILDREN:
        write_child(f"new-{agent_type}", agent_type, 140, finished=True, native_id=SUCCESSOR_ID)
    time.sleep(3)
    for agent_type, file_name in zip(
        CHILDREN, ("research.md", "checklist.md", "outline.md"), strict=True
    ):
        (project / file_name).write_text(
            f"# {agent_type}\n已从交接文档继续并完成。\n", encoding="utf-8"
        )
    (project / "demo-complete.json").write_text(
        json.dumps(
            {
                "successor_native_id": SUCCESSOR_ID,
                "new_agents": [f"new-{agent_type}" for agent_type in CHILDREN],
            }
        ),
        encoding="utf-8",
    )


def successor_worker(prompt: str, resume_id: str) -> None:
    record("successor_worker", prompt, resume_id)
    if resume_id:
        raise RuntimeError(f"successor 不应带 resume_id：{resume_id!r}")
    if "dispatch_prompt 原文放进对应 Agent/Task 调用的 prompt 参数" not in prompt:
        raise RuntimeError(f"派发指令不符合约定：{prompt!r}")
    if "先读取 main_handoff" not in prompt:
        raise RuntimeError("派发指令缺少 main_handoff 前置步骤")
    emit({"type": "system", "subtype": "init", "session_id": SUCCESSOR_ID})
    project = Path(os.environ["WORKFLOW_CONTROLLER_DEMO_PROJECT"])
    batch = next((project / ".workflow-controller" / "handoffs").glob("*/*"))
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    _emit_successor_dispatches(manifest)
    _write_successor_outputs(project)
    emit(
        {
            "type": "result",
            "session_id": SUCCESSOR_ID,
            "usage": {"input_tokens": 180},
            "modelUsage": {"demo": {"contextWindow": WINDOW}},
        }
    )


def main() -> None:
    _configure_logging()
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1]
    resume_id = args[args.index("--resume") + 1] if "--resume" in args else ""
    if "--fork-session" in args:
        emit(
            {
                "type": "result",
                "session_id": "demo-native-context-probe",
                "usage": {"input_tokens": 180 if resume_id == SUCCESSOR_ID else 30_000},
                "modelUsage": {"demo": {"contextWindow": WINDOW}},
            }
        )
    elif "handoff-prompt.md" in prompt:
        handoff_worker(prompt)
    elif "子代理整体交接已触发" in prompt:
        successor_worker(prompt, resume_id)
    else:
        initial_worker(prompt)


if __name__ == "__main__":
    main()
