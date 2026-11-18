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

"""examples 共享的 demo 支撑库（面向 demo 叙事的类/脚本式构造）。

各 demo 目录把 ``examples/common`` 加进 ``sys.path`` 后导入本模块。集中承载：

- 进程内 demo 的标准装配：日志、agent 定义播种、真实 REST 路由挂载、
  agent_traceback 插件挂载（services 走生产装配函数）。
- 假 opencode.db 的「对话剧本」式播种（`DemoOpencodeDb`）——先按叙事顺序
  登记 session / part / message，最后一次性落盘。
- source opencode.db schema 模板（`SourceOpencodeSchemaDb`）——确定性模式
  给重建 / 转写做 schema 源。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any


def configure_demo_logging(demo_logger: logging.Logger) -> None:
    """demo 日志：stdout 单行格式，INFO 级别。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    demo_logger.addHandler(handler)
    demo_logger.setLevel(logging.INFO)


def demo_banner(demo_logger: logging.Logger, title: str) -> None:
    """分节 banner（64 列分隔线包住标题）。"""
    demo_logger.info(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def seed_agent_defs(example_dir: Path, target: Path | None = None) -> Path:
    """把 agents/*.md 复制到 .opencode/agents/（opencode 的 agent 发现目录）。"""
    agents_dir = (target or example_dir) / ".opencode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for src in (example_dir / "agents").glob("*.md"):
        (agents_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return agents_dir


def mount_real_app(orchestrator: Any, config_yaml: Path) -> Any:
    """挂载真实 REST 路由到一个 FastAPI app（不起子进程 web，httpx 直连）。"""
    from fastapi import FastAPI

    from workflow_controller.api.core_api import create_core_api
    from workflow_controller.core.run_coordinator import RunCoordinator
    from workflow_controller.web.console import register_domain_routes, register_plugin_routers

    coordinator = RunCoordinator(
        orchestrator=orchestrator,
        run_store=orchestrator.run_store,
        bus=orchestrator.bus,
        driver_factories=orchestrator.plugin_registry.run_driver_factories(),
        affiliation=orchestrator.affiliation,
    )
    app = FastAPI()
    app.state.api = create_core_api(orchestrator, config_yaml, coordinator)
    app.state.orchestrator = orchestrator
    register_domain_routes(app)
    # agent_traceback 提供 /api/traceback/* 与 /api/sessions/{sid}/checkpoints/*。
    register_plugin_routers(app, orchestrator)
    return app


async def demo_http_get(app: Any, path: str) -> dict:
    """进程内 GET（httpx ASGITransport 直连 FastAPI app）。"""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        return (await ac.get(path)).json()


async def demo_http_post(app: Any, path: str, body: dict | None = None) -> dict:
    """进程内 POST（httpx ASGITransport 直连 FastAPI app）。"""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        return (await ac.post(path, json=body or {})).json()


def resolve_effective_subagent_agent(
    subagent_type: str, hints: dict | None, agents_dir: Path
) -> dict | None:
    """某 subagent_type 实际能读到的命名 agent 配置（镜像 opencode driver 的解析）。

    有 hint → ``OPENCODE_CONFIG_CONTENT`` 的该 agent 覆盖磁盘 def；无 hint →
    返回 None（回退磁盘 .md）。
    """
    from workflow_controller.driver.opencode_gate import build_config_content

    cfg = json.loads(build_config_content(subagent_hints=(hints or None), agents_dir=agents_dir))
    return cfg.get("agent", {}).get(subagent_type)


def attach_traceback_plugin(
    orchestrator: Any,
    example_dir: Path,
    work_dir: Path,
    *,
    snapshot_timeout_seconds: float,
    reconstruct: dict | None = None,
) -> Any:
    """按生产装配路径构造并 install agent_traceback 插件（返回插件实例）。

    services 走 ``_build_plugin_services``（与 PluginRegistry.load 同源）——
    插件经能力端口访问 pool / 事件库，未装配的空 services 会让快照 / 重放
    路径直接抛 RuntimeError。
    """
    from workflow_controller.plugin.base import PluginConfig
    from workflow_controller.plugin.context import PluginContext
    from workflow_controller.plugin.registry import _build_plugin_services
    from workflow_controller.plugins.agent_traceback.plugin import AgentTracebackPlugin

    extra: dict[str, Any] = {
        "snapshot_timeout_seconds": snapshot_timeout_seconds,
        "max_checkpoints": 200,
        "snapshot_dir": ".workflow-controller/snapshots",
        "ignore": [],
    }
    if reconstruct is not None:
        extra["reconstruct"] = reconstruct
    plugin = AgentTracebackPlugin(
        PluginConfig(name="agent_traceback", version="0.1.0", enabled=True, extra=extra),
        example_dir,
    )
    plugin.install(
        PluginContext(
            bus=orchestrator.bus,
            type_registry=orchestrator.bus.type_registry,
            hook_server=None,
            work_dir=Path(str(work_dir)).expanduser(),
            services=_build_plugin_services(orchestrator),
        )
    )
    return plugin


# 已知 schema 的其余小表（建出来避免 schema 漂移 warning 噪声）
_STUB_TABLES = [
    "todo",
    "event",
    "event_sequence",
    "session_input",
    "session_message",
    "session_share",
    "session_context_epoch",
]


class SourceOpencodeSchemaDb:
    """最小 source opencode.db（schema 模板）。

    确定性模式给重建 / 转写做 schema 源：``__drizzle_migrations`` 让
    opencode 不重跑 CREATE TABLE；live 模式可直接换成宿主真实 opencode.db。
    """

    def __init__(self, tmp_path: Path) -> None:
        self._src = tmp_path / "source_opencode" / "opencode.db"

    def build(self) -> Path:
        self._src.parent.mkdir(parents=True)
        con = sqlite3.connect(self._src)
        con.execute("CREATE TABLE __drizzle_migrations (id TEXT, hash TEXT, created_at INTEGER)")
        con.execute("INSERT INTO __drizzle_migrations VALUES ('0001', 'fake', 0)")
        con.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, "
            "slug TEXT, directory TEXT, title TEXT, version TEXT, share_url TEXT, "
            "summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER, "
            "summary_diffs TEXT, revert INTEGER, permission TEXT, time_created INTEGER, "
            "time_updated INTEGER, time_compacting INTEGER, time_archived INTEGER, "
            "workspace_id TEXT, path TEXT, agent TEXT, model TEXT, cost REAL, "
            "tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER, "
            "tokens_cache_read INTEGER, tokens_cache_write INTEGER, metadata TEXT)"
        )
        con.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        con.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        for stub in _STUB_TABLES:
            con.execute(f"CREATE TABLE {stub} (id TEXT PRIMARY KEY)")
        con.execute(
            "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT, vcs TEXT, "
            "icon_url TEXT, icon_color TEXT, time_created INTEGER, time_updated INTEGER, "
            "time_initialized INTEGER, sandboxes TEXT, commands TEXT, icon_url_override TEXT)"
        )
        con.commit()
        con.close()
        return self._src


class DemoOpencodeDb:
    """进程内 demo 的假 opencode.db——按「对话剧本」播种后一次性落盘。

    用法：``db = DemoOpencodeDb(xdg_root)``，依次 ``session()`` / ``text_part()``
    / ``task_part()`` / ``read_part()`` / ``user_message()`` / ``assistant_message()``
    登记剧本，最后 ``write()`` 落盘。
    """

    def __init__(self, xdg_root: Path) -> None:
        self._dbdir = xdg_root / "opencode"
        self._sessions: list[dict] = []
        self._parts: list[tuple] = []
        self._messages: list[tuple] = []

    def session(
        self,
        sid: str,
        *,
        title: str = "orig",
        directory: str = "",
        agent: str = "worker",
        model: str = "m",
    ) -> DemoOpencodeDb:
        """登记主 session 行（time_created/time_updated 始终为 100）。"""
        self._sessions.append(
            {
                "id": sid,
                "title": title,
                "directory": directory,
                "time_created": 100,
                "time_updated": 100,
                "agent": agent,
                "model": model,
            }
        )
        return self

    def text_part(self, sid: str, pid: str, tc: int, text: str) -> DemoOpencodeDb:
        """登记 text part（message_id 始终为 m_<pid>）。"""
        self._parts.append(
            (pid, f"m_{pid}", sid, tc, tc, json.dumps({"type": "text", "text": text}))
        )
        return self

    def task_part(self, sid: str, pid: str, tc: int, sub_id: str) -> DemoOpencodeDb:
        """登记 task 工具 part（state.output 含子 session id，re-call 定位用）。"""
        output = f"<task_result session_id='{sub_id}'>done</task_result>"
        part_data = {
            "type": "tool",
            "tool": "task",
            "callID": f"c_{pid}",
            "state": {"status": "completed", "input": {}, "output": output},
        }
        self._parts.append((pid, f"m_{pid}", sid, tc, tc, json.dumps(part_data)))
        return self

    def read_part(self, sid: str, pid: str, tc: int, abs_path: str) -> DemoOpencodeDb:
        """登记 read 工具 part（state.input.filePath 含绝对路径，路径重写回归用）。"""
        self._parts.append(
            (
                pid,
                f"m_{pid}",
                sid,
                tc,
                tc,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "read",
                        "callID": f"c_{pid}",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": abs_path},
                            "output": f"<path>{abs_path}</path>",
                            "metadata": {"filepath": abs_path},
                        },
                    }
                ),
            )
        )
        return self

    def user_message(self, sid: str, pid: str, tc: int) -> DemoOpencodeDb:
        """登记 user 角色 message 行。"""
        self._messages.append(
            (f"m_{pid}", sid, tc, tc, json.dumps({"role": "user", "time": {"created": tc}}))
        )
        return self

    def assistant_message(
        self, sid: str, pid: str, tc: int, parent: str | None = None
    ) -> DemoOpencodeDb:
        """登记 assistant 角色 message 行（可选 parentID 链）。"""
        data: dict[str, object] = {"role": "assistant", "time": {"created": tc}}
        if parent:
            data["parentID"] = parent
        self._messages.append((f"m_{pid}", sid, tc, tc, json.dumps(data)))
        return self

    def write(self) -> Path:
        """落盘剧本（session/part/message 三表），返回 db 路径。"""
        self._dbdir.mkdir(parents=True, exist_ok=True)
        db = self._dbdir / "opencode.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE session ("
            "id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT, "
            "time_created INTEGER, time_updated INTEGER, agent TEXT, model TEXT, version TEXT)"
        )
        insert_session = (
            "INSERT INTO session (id, title, directory, time_created, time_updated, agent, model)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        for row in self._sessions:
            con.execute(insert_session, tuple(row.values()))
        con.execute(
            "CREATE TABLE part ("
            "id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        for row in self._parts:
            con.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", row)
        con.execute(
            "CREATE TABLE message ("
            "id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, data TEXT)"
        )
        for row in self._messages:
            con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", row)
        con.commit()
        con.close()
        return db
