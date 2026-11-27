# Copyright (c) 2026 Workflow Controller Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
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

"""claude_sdk 真实 runtime 的 run 平面装配与取数（冒烟用例共用）。

经统一启动入口起真实内核：graph 插件按入口点自动发现，成员会话由
claude_sdk 驱动拉起捆绑 CLI。reviewer 关闭，成员只跑一轮且不做 LLM 评估，
避免与冒烟争 hook。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from workflow_controller.api.core_api import CoreAPI, boot
from workflow_controller.config.settings import AppConfig, HookServerConfig, ReviewerConfig
from workflow_controller.core.workspace.repository import WorkspaceRepository
from workflow_controller.session.events import EventType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.core.runs import RunRecord
    from workflow_controller.session.events import Event

# graph 插件登记的驱动格式：内核测试不 import 生产插件，只按格式串启动
GRAPH_FORMAT = "graph-yaml@1"
# 缺省模型名与 reviewer 实模用例一致，环境变量 ANTHROPIC_MODEL 优先
DEFAULT_MODEL = "glm-5.2"
# 单个 run 的等待上界：CLI 启动、多轮工具调用与进程重启的总预算
RUN_TIMEOUT_S = 900.0
# 等成员出生、等指定工具调用的上界：CLI 冷启动加首轮模型响应
MEMBER_TIMEOUT_S = 300.0
# 被弃置实例的任务名前缀：run 任务、宿主受监督任务与会话运行时
_ABANDONED_TASK_PREFIXES = ("run:", "run-", "workflow-controller-session-")


def live_model() -> str:
    """实跑所用模型名。"""
    return os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def write_workflow(path: Path, nodes: list[dict[str, Any]], *, compat: bool = False) -> Path:
    """落冒烟工作流：开启 traceback、单路并行；compat 为真时显式选弱化档。"""
    traceback: dict[str, Any] = {"enabled": True}
    if compat:
        traceback["on_no_dispatch_barrier"] = "compat_late_barrier"
    path.write_text(
        yaml.safe_dump(
            {
                "workflow": "traceback-live",
                "max_parallel": 1,
                "traceback": traceback,
                "nodes": nodes,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _query_db(db_path: Path, sql: str, params: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """只读查询单库，返回字典行。"""
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _flatten_nodes(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """投影里的全部节点行，子图节点以「父:子」为键并入。"""
    driver = state.get("driver") or {}
    nodes = {node["id"]: node for node in driver.get("nodes") or []}
    for subgraph in driver.get("subgraphs") or []:
        parent = subgraph["parent_node"]
        nodes.update({f"{parent}:{node['id']}": node for node in subgraph.get("nodes") or []})
    return nodes


def transcript_rows(path: Path) -> list[dict[str, Any]]:
    """逐行读 CLI 会话转录；文件缺席返回空表。"""
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def result_recorded_at(transcript: Path, tool_use_id: str) -> float | None:
    """父会话转录里指定工具调用结果的落盘时刻，即结果交付给父代理的时刻。"""
    for row in transcript_rows(transcript):
        blocks = (row.get("message") or {}).get("content") or []
        if not isinstance(blocks, list):
            continue
        if any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == tool_use_id
            for block in blocks
        ):
            return datetime.fromisoformat(str(row["timestamp"])).timestamp()
    return None


@dataclass
class LivePlane:
    """一个真实内核实例：存储锚定当前工作目录，run 工作目录独立于数据根。"""

    project_dir: Path
    work_dir: Path
    orchestrator: Orchestrator
    api: CoreAPI
    abandoned: bool = False

    @classmethod
    async def start(cls, project_dir: Path) -> LivePlane:
        """按 headless 形态启动：hook server 只为承载插件装载，端口由系统分配。"""
        config = AppConfig(
            project_dir=str(project_dir),
            runtime="claude_sdk",
            hook_server=HookServerConfig(enabled=True, port=0),
            reviewer=ReviewerConfig(enabled=False),
        )
        orchestrator, api = await boot(
            config,
            project_dir / "workflow-controller-config.yaml",
            settings_dir=project_dir / "settings",
        )
        return cls(project_dir, project_dir / "work", orchestrator, api)

    @property
    def db_path(self) -> Path:
        """守护进程单库：账面、事件与检查点同库。"""
        return self.project_dir / ".workflow-controller" / "events.db"

    async def start_run(self, yaml_path: Path, name: str, *, declare_runtime: bool = True) -> str:
        """启动 graph run：成员只跑一轮、不做 LLM 评估。

        declare_runtime 为真时 run 参数声明 runtime，引擎据此在出生侧禁用
        task 工具；为假时成员沿用全局 runtime，派发只经屏障的能力门。
        """
        params: dict[str, str] = {
            "model": live_model(),
            "evaluator": "always_complete",
            "max_iterations": "1",
        }
        if declare_runtime:
            params["runtime"] = "claude_sdk"
        run = await self.api.run.start(
            name,
            yaml_path,
            fmt=GRAPH_FORMAT,
            params=params,
            labels={"name": name},
            work_dir=str(self.work_dir),
        )
        return run.run_id

    async def wait_run_finished(self, run_id: str, timeout_s: float = RUN_TIMEOUT_S) -> RunRecord:
        """轮询 run 至终态；超时带现场快照判失败。"""
        deadline = time.monotonic() + timeout_s
        while True:
            run = await self.api.run.get_run(run_id)
            if run.finished_at is not None:
                return run
            if time.monotonic() > deadline:
                pytest.fail(f"run 未在 {timeout_s}s 内结束：{await self.describe(run_id)}")
            await asyncio.sleep(0.5)

    async def describe(self, run_id: str) -> str:
        """失败现场快照：节点状态与失败原因、成员状态、尾部事件类型。"""
        state = await self.api.run.state(run_id)
        nodes = {
            node_id: [node.get("status"), node.get("failure_reason")]
            for node_id, node in _flatten_nodes(state).items()
        }
        members = [
            [row["context"], row["state"], row["stop_reason"]]
            for row in await self.api.run.session_infos(run_id)
        ]
        tail = [row["event_type"] for row in (await self.run_events(run_id))[-12:]]
        return json.dumps(
            {"status": state["status"], "nodes": nodes, "members": members, "events": tail},
            ensure_ascii=False,
        )

    async def node_states(self, run_id: str) -> dict[str, dict[str, Any]]:
        """驱动投影里的全部节点行。"""
        return _flatten_nodes(await self.api.run.state(run_id))

    async def node_archive(self, run_id: str, node_id: str) -> dict[str, Any]:
        """驱动快照里的节点档案：作用域映射与会话绑定随快照持久化。"""
        snapshot = await self.api.run.driver_snapshot(run_id)
        return ((snapshot or {}).get("state") or {}).get("nodes", {}).get(node_id) or {}

    async def run_events(self, run_id: str) -> list[dict[str, Any]]:
        """run 全量事件：run 键与成员打戳事件合并序，按页取尽。"""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor, page_size = await self.api.run.fetch_events(run_id, cursor, 1000)
            rows.extend(page)
            if len(page) < page_size:
                return rows

    async def session_events(self, session_id: str) -> list[Event]:
        """成员会话的持久化事件。"""
        return await self.orchestrator.event_router.get_events(session_id)

    def members(self, run_id: str) -> list[dict[str, str]]:
        """run 成员行：会话、角色、归属上下文与原生会话标识。"""
        return _query_db(
            self.db_path,
            "SELECT session_id, role, context, native_session_id FROM run_sessions"
            " WHERE run_id = ? ORDER BY attached_at",
            (run_id,),
        )

    def member(self, run_id: str, context: str) -> dict[str, str]:
        """按归属上下文取 run 成员行。"""
        return next(row for row in self.members(run_id) if row["context"] == context)

    async def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        """本 run 作用域树的全部检查点行，含子代理作用域（查询面只回根与直接子作用域）。"""
        rows = _query_db(
            self.db_path,
            "SELECT checkpoint_id, scope_id, scope_kind, parent_scope_id, trigger,"
            " barrier_quality, workspace_commit, dispatch_anchor, child_session_map,"
            " created_at"
            " FROM scope_checkpoints ORDER BY created_at, checkpoint_id",
        )
        tree = {f"{run_id}-root"}
        while (
            children := {row["scope_id"] for row in rows if row["parent_scope_id"] in tree} - tree
        ):
            tree |= children
        kept = [row for row in rows if row["scope_id"] in tree]
        for row in kept:
            for column in ("dispatch_anchor", "child_session_map"):
                row[column] = json.loads(row[column]) if row[column] else None
        return kept

    def repository(self, run_id: str) -> WorkspaceRepository:
        """run 的私有仓访问面。"""
        return WorkspaceRepository(self.work_dir, run_id)

    async def abandon_as_killed(self) -> None:
        """同进程模拟进程被强杀：先停在途协程，再杀 CLI 进程组，全程不走关停序。

        会话运行时只置停止位并取消，账面上的会话状态原样留下，等下次启动
        按崩溃遗留恢复；run 任务与宿主后台任务直接取消。
        """
        self.abandoned = True
        drivers = [
            session.driver
            for session in self.orchestrator.pool.list_sessions()
            if session.driver is not None
        ]
        for loop in list(self.orchestrator._loops.values()):
            loop.request_stop()
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith(_ABANDONED_TASK_PREFIXES)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for driver in drivers:
            await driver.kill()
        hook_server = self.orchestrator._hook_server
        if hook_server is not None:
            await hook_server.stop_server()
        await self.orchestrator.watchdog.stop()
        await self.orchestrator.bus.stop()


@asynccontextmanager
async def live_plane(project_dir: Path) -> AsyncIterator[LivePlane]:
    """启动真实内核，退出时关停；已被强杀模拟弃置的实例不再关停。"""
    plane = await LivePlane.start(project_dir)
    try:
        yield plane
    finally:
        if not plane.abandoned:
            with contextlib.suppress(Exception):
                await plane.orchestrator.shutdown()


async def wait_member(
    plane: LivePlane, run_id: str, context: str, timeout_s: float
) -> dict[str, str]:
    """等指定归属上下文的成员出生并绑定原生会话，返回成员行。"""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = [row for row in plane.members(run_id) if row["context"] == context]
        if rows and rows[0]["native_session_id"]:
            return rows[0]
        if time.monotonic() > deadline:
            pytest.fail(f"成员 {context} 未在 {timeout_s}s 内出生：{await plane.describe(run_id)}")
        await asyncio.sleep(0.5)


async def wait_tool_use(
    plane: LivePlane, session_id: str, tool_name: str, input_fragment: str, timeout_s: float
) -> None:
    """等成员会话发出指定工具调用（调用入参含给定片段）。"""
    deadline = time.monotonic() + timeout_s
    while True:
        sent = any(
            event.event_type is EventType.TOOL_USE
            and event.data.get("name") == tool_name
            and input_fragment in json.dumps(event.data.get("input") or {}, ensure_ascii=False)
            for event in await plane.session_events(session_id)
        )
        if sent:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"会话未在 {timeout_s}s 内调用 {tool_name}（入参含 {input_fragment!r}）")
        await asyncio.sleep(0.5)


async def assert_no_fault_events(plane: LivePlane, run_id: str) -> None:
    """断言 run 全程没有故障事件。"""
    faults = [
        row["event_type"]
        for row in await plane.run_events(run_id)
        if row["event_type"].startswith("fault.")
    ]
    assert faults == [], f"出现故障事件：{faults}"


def assert_workspaces_released(repo: WorkspaceRepository, *scope_keys: str) -> None:
    """断言各作用域工作区已释放，且工作区目录无残留。"""
    for scope_key in scope_keys:
        assert not repo.workspace_dir_for(scope_key).exists(), f"工作区未释放：{scope_key}"
    leftovers = (
        [entry.name for entry in repo.workspaces_dir.iterdir()]
        if repo.workspaces_dir.exists()
        else []
    )
    assert leftovers == [], f"工作区目录有残留：{leftovers}"


@dataclass
class HostHookTrace:
    """宿主侧屏障与子代理收尾回调的时间线。"""

    barriers: list[dict[str, Any]] = field(default_factory=list)
    settles: list[dict[str, Any]] = field(default_factory=list)


def trace_host_hooks(
    monkeypatch: pytest.MonkeyPatch,
    plane: LivePlane,
    trace: HostHookTrace,
    *,
    settle_delay_s: float = 0.0,
) -> None:
    """给编排器的屏障入口与子代理收尾入口包一层计时，成员会话激活时取到的是包装后的入口。

    收尾入口可注入一段等待，用来观测 CLI 是否仍等它返回才把结果交给父代理。
    """
    orchestrator = plane.orchestrator
    barrier = orchestrator._handle_dispatch_barrier
    settle = orchestrator._handle_subagent_result

    async def traced_barrier(session_id: str, data: dict[str, Any], *, kind: str) -> Any:
        """记下屏障到场的时刻、工具调用号与子代理来源标识后转交原入口。"""
        trace.barriers.append(
            {
                "at": time.time(),
                "kind": kind,
                "tool": data.get("tool_name"),
                "tool_use_id": data.get("tool_use_id") or "",
                "agent_id": data.get("agent_id") or "",
                "parent_tool_use_id": data.get("parent_tool_use_id") or "",
            }
        )
        return await barrier(session_id, data, kind=kind)

    async def traced_settle(session_id: str, data: dict[str, Any]) -> None:
        """记下收尾的开始与结束时刻，中间可注入等待。"""
        entry = {"agent_id": data.get("agent_id") or "", "started_at": time.time()}
        trace.settles.append(entry)
        try:
            if settle_delay_s:
                await asyncio.sleep(settle_delay_s)
            await settle(session_id, data)
        finally:
            entry["finished_at"] = time.time()

    monkeypatch.setattr(orchestrator, "_handle_dispatch_barrier", traced_barrier)
    monkeypatch.setattr(orchestrator, "_handle_subagent_result", traced_settle)


def report(evidence: dict[str, Any]) -> None:
    """打印实跑证据（耗时与时序），供修复日志引用。"""
    print("\n[traceback-live 证据] " + json.dumps(evidence, ensure_ascii=False, default=str))
