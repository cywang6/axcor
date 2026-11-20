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

"""graph 在 run 平面上的行为面：工厂注册/全链路启停查/成员归属/驱动行为。

启动唯一通道是 run 平面（coord.start_run）；驱动行为断言经 run 平面 API
与插件 run_id 键驱动注册表访问。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from workflow_controller.core.run_coordinator import RunConfig
from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugins.graph.plugin import GRAPH_FORMAT, GraphPlugin

from .run_plane_helpers import (
    FakeAgentSession,
    GraphRunPlaneEnv,
)
from .run_plane_helpers import (
    await_graph_status as _await_graph_status,
)
from .run_plane_helpers import (
    await_run_status as _await_run_status,
)

if TYPE_CHECKING:
    from pathlib import Path

# 装配环境的历史名（本文件内既有用例沿用）
SwitchEnv = GraphRunPlaneEnv


def _direct_gid(yaml_path: Path) -> str:
    """无 label 直连 run 的 graph_id（程序位置摘要派生）。"""
    digest = hashlib.sha256(str(yaml_path).encode()).hexdigest()[:8]
    return f"{yaml_path.stem}_{digest}"


def _flaky_counter_node() -> dict:
    """计次重试节点：第二次验收通过（事件回放/无 graphs 表用例共用）。"""
    return {
        "id": "flaky",
        "task_type": "bash",
        "command": (
            "CNT=$(cat cnt 2>/dev/null || echo 0); CNT=$((CNT+1)); "
            "echo $CNT > cnt; echo attempt-$CNT"
        ),
        "acceptance": {
            "enabled": True,
            "type": "script",
            "script": "test $(cat cnt) -ge 2",
            "max_retries": 1,
        },
        "depends_on": [],
    }


def _generator_nodes() -> list[dict]:
    """generator + 下游 bad 的双节点清单（子图重执行用例共用）。"""
    return [
        {
            "id": "gen",
            "task_type": "generator",
            "generator_type": "bash",
            "command": "true",
            "subgraph": {"output": "sub.yaml"},
            "depends_on": [],
        },
        {"id": "bad", "task_type": "bash", "command": "true", "depends_on": ["gen"]},
    ]


def _write_sub_yaml(wd: Path) -> None:
    """子图 yaml 落盘（s1→s2 拓扑，输出 sub.yaml）。"""
    (wd / "sub.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": "sub",
                "nodes": [
                    {"id": "s1", "task_type": "bash", "command": "true", "depends_on": []},
                    {"id": "s2", "task_type": "bash", "command": "true", "depends_on": ["s1"]},
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_wd_yaml(env: SwitchEnv, name: str, extra: dict) -> Path:
    """写单 only-agent 节点的工作流 yaml，extra 并入顶层（work_dir 链用例）。"""
    doc = {
        "workflow": name,
        **extra,
        "nodes": [{"id": "only", "task_type": "agent", "task_description": "干活"}],
    }
    path = env.tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


async def _direct_start_run(env: SwitchEnv, yaml_path: Path, work_dir: Path | None = None) -> Any:
    """无 label 直连启动（显式 RunConfig；缺省 work_dir 用 env.tmp/wd）。"""
    return await env.coord.start_run(
        RunConfig(
            format=GRAPH_FORMAT,
            work_dir=str(work_dir or env.tmp / "wd"),
            program_ref=str(yaml_path),
        ),
        yaml_path,
    )


async def _await_node_running(
    env: SwitchEnv,
    run_id: str,
    node_id: str,
    *,
    want_session: bool = True,
    fail_msg: str = "节点未进入 running",
    timeout_s: float = 5.0,
) -> None:
    """轮询等待节点进入 running（可选等成员 session 绑定）。"""
    deadline = time.monotonic() + timeout_s
    while True:
        info = env.node_info(run_id, node_id)
        ok = info.get("status") == "running" and (
            not want_session or info.get("workflow_controller_session_id")
        )
        if ok:
            return
        if time.monotonic() > deadline:
            pytest.fail(fail_msg)
        await asyncio.sleep(0.02)


# 投影协议 v2 冻结的顶层字段集与节点字段集（test_projection_shape_frozen 用）
_PROJECTION_TOP_KEYS = frozenset(
    {
        "schema_version",
        "graph_id",
        "workflow",
        "description",
        "status",
        "nodes",
        "subgraphs",
        "retired_instances",
        "edges",
        "rollback_edges",
        "work_dir",
        "model",
        "provider_index",
    }
)

_PROJECTION_NODE_KEYS = frozenset(
    {
        "id",
        "depends_on",
        "task_type",
        "display_type",
        "task_description",
        "status",
        "attempts",
        "gen",
        "workflow_controller_session_id",
        "started_at",
        "finished_at",
        "failure_reason",
        "output",
        "subgraph_generated",
        "subgraph_node_ids",
    }
)


class TestRunPlaneSwitch:
    async def test_boot_prunes_only_opted_in_terminal_members(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        graph_run = await env.store.create("graph.yaml", GRAPH_FORMAT)
        other_run = await env.store.create("other.yaml", "other@1")
        for sid, run_id, reason in (
            ("graph-done", graph_run.run_id, "reviewer_complete"),
            ("graph-paused", graph_run.run_id, "user_pause"),
            ("other-done", other_run.run_id, "reviewer_complete"),
        ):
            session = FakeAgentSession(sid, asyncio.Event())
            session.state.value = "idle"
            session.loop_status = "no"
            session.stop_reason = reason
            env.so.sessions[sid] = session
            await env.affiliation.attach(sid, run_id, role="worker")

        await getattr(env.coord, "_discard_restored_terminal_sessions")()

        assert set(env.so.sessions) == {"graph-paused", "other-done"}
        assert env.so.pool.discard.call_args.args == ("graph-done",)

    async def test_factory_registers_graph_yaml_format(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        factories = env.plugin.get_run_driver_factories()
        assert set(factories) == {GRAPH_FORMAT}
        assert factories[GRAPH_FORMAT].format == GRAPH_FORMAT

    async def test_member_spawn_uses_spec_and_metadata_param(self, tmp_path: Path):
        """成员出生的规格化断言：spec 组装 + 溯源随参数 + 无名册私有直调。"""
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}],
            label="g1",
        )
        await _await_run_status(env, run_id, {"completed"})
        assert env.so.spawn_calls, "成员 session 未出生"
        spawn = env.so.spawn_calls[0]
        cfg = spawn["cfg"]
        assert cfg.name == "graph:only"
        assert cfg.run_id == run_id
        assert cfg.role == "executor"
        assert cfg.initial_prompt
        # 溯源 metadata 随出生参数携带（事后直改与名册私有直调消灭）
        assert spawn["plugin_metadata"] == {
            "graph": {"graph_id": "g1", "node_id": "only"},
        }
        getattr(env.so.pool, "_persist_session").assert_not_called()

    async def test_agent_graph_full_chain_completes(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}],
            label="g1",
        )
        run = await _await_run_status(env, run_id, {"completed"})
        assert run.labels.get("name") == "g1"
        # 成员 session 归入 workflow run（隐式 run 噪声终止）
        sessions = await env.coord.list_run_sessions(run_id)
        assert [s.role for s in sessions] == ["executor"]
        # yaml 冻结进 run 根 program/（入口按 .workflow-controller-entry 清单定位，
        # 程序目录全树冻结下面不假设首个 iterdir 项是入口文本）
        frozen = env.coord.run_data_root(run_id) / "program"
        entry = (frozen / ".workflow-controller-entry").read_text(encoding="utf-8").strip()
        assert entry == "wf.yaml"
        assert (frozen / entry).read_text(encoding="utf-8")

    async def test_node_terminal_discards_session_after_snapshot(self, tmp_path: Path):
        env = SwitchEnv(tmp_path, auto_release=False)
        seen_before_birth: list[str] = []
        start_session = env.so.start_session_with_config

        async def inspect_birth(cfg: Any, **kwargs: Any) -> Any:
            blob = await env.coord.load_driver_snapshot(cfg.run_id)
            seen_before_birth.append(blob["state"]["nodes"]["only"]["status"])
            return await start_session(cfg, **kwargs)

        env.so.start_session_with_config = inspect_birth
        run_id = await env.start_graph_run(
            [
                {
                    "id": "only",
                    "task_type": "agent",
                    "task_description": "干活",
                }
            ]
        )
        deadline = time.monotonic() + 5.0
        while not env.so.sessions:
            if time.monotonic() > deadline:
                pytest.fail("成员 session 未出生")
            await asyncio.sleep(0.02)
        sid = next(iter(env.so.sessions))
        await getattr(env.coord, "_host").discard_run_session(run_id, sid)
        env.so.pool.discard.assert_not_called()
        env.so.release_all()
        await _await_run_status(env, run_id, {"completed"})

        members = await env.coord.list_run_sessions(run_id)
        assert len(members) == 1
        assert seen_before_birth == ["running"]
        env.so.pool.discard.assert_called_once_with(sid)
        assert await getattr(env.coord, "_run_store").session_in_run(run_id, sid)

    async def test_state_projection_reconciles_missing_member_through_run(
        self,
        tmp_path: Path,
    ):
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        deadline = time.monotonic() + 5.0
        while not env.so.sessions or not env.node_info(run_id, "only").get(
            "workflow_controller_session_id"
        ):
            if time.monotonic() > deadline:
                pytest.fail("节点成员未绑定")
            await asyncio.sleep(0.02)
        sid, session = next(iter(env.so.sessions.items()))
        env.so.sessions.pop(sid)

        # 读修复经通用驱动通道（面板在非终态投影到达时触发）：
        # 丢出内存池的成员按停止收尾并随事件流落库
        await env.coord.request_driver_op(run_id, "reconcile_projection")
        state = await env.coord.run_state(run_id)
        blob = await env.coord.load_driver_snapshot(run_id)
        assert state["driver"]["nodes"][0]["status"] == "stopped"
        assert blob["state"]["nodes"]["only"]["status"] == "stopped"
        assert blob["state"]["nodes"]["only"]["attempts"] == 1
        # 读修复的状态迁移随事件流落库：事件消费者可见 running→stopped
        rows = env.so.event_router.default_store.get_run_events_sync(run_id)
        types = {r["event_type"] for r in rows}
        assert {"node_state", "node_history", "graph_paused", "projection_updated"} <= types
        assert any(
            r["event_type"] == "node_state" and r["data"].get("status") == "stopped" for r in rows
        )
        session.state.value = "idle"
        session.stop_reason = "user_pause"
        session.loop_status = "no"

        def restore(old_sid: str) -> Any:
            assert old_sid == sid
            env.so.sessions[old_sid] = session
            return session

        env.so.pool.restore_from_store.side_effect = restore
        await env.coord.resume_run(run_id)
        deadline = time.monotonic() + 5.0
        while (
            env.node_info(run_id, "only")["status"] != "running" or session.state.value != "running"
        ):
            if time.monotonic() > deadline:
                pytest.fail("丢出内存池的旧成员没有续跑")
            await asyncio.sleep(0.02)
        assert env.driver_of(run_id).state.attempts("only") == 1
        assert len(env.so.spawn_calls) == 1
        getattr(session, "_gate").set()
        await _await_run_status(env, run_id, {"completed"})

    async def test_bash_graph_completes_via_process_events(self, tmp_path: Path):
        # sleep 压过旧复查窗口：bash 尾段的完成上报由进程事件与完成信号驱动，
        # 账面不再停留 running 等用户 stop
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "b", "task_type": "bash", "command": "sleep 1 && echo hi"}]
        )
        run = await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)
        assert not run.error
        await _await_graph_status(env, run_id, {"completed"})

    async def test_failed_chain_reaches_run_on_failure_without_new_event(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {"id": "a", "task_type": "bash", "command": "false"},
                {"id": "b", "task_type": "bash", "command": "true", "depends_on": ["a"]},
                {"id": "c", "task_type": "bash", "command": "true", "depends_on": ["b"]},
                {
                    "id": "cleanup",
                    "task_type": "bash",
                    "command": "touch cleaned",
                    "depends_on": ["c"],
                    "run_on_failure": True,
                },
            ]
        )

        await _await_run_status(env, run_id, {"failed"}, timeout_s=10.0)
        assert (tmp_path / "wd" / "cleaned").exists()
        states = env.driver_of(run_id).state.snapshot()["nodes"]
        assert states["b"]["status"] == states["c"]["status"] == "skipped"
        assert states["cleanup"]["status"] == "passed"

    async def test_bash_graph_events_and_process_rows_in_run_events(
        self,
        tmp_path: Path,
    ):
        # 伪 id 清零：图事件与 bash 输出落 run_events（带 run 维度），
        # 守护进程库不出现以 graph_id 为 session_id 的事件行
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {
                    "id": "prep",
                    "task_type": "bash",
                    "command": "echo prepare-line",
                    "acceptance": {"enabled": True, "type": "script", "script": "true"},
                    "depends_on": [],
                },
            ],
            label="g1",
        )
        await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)
        store = env.so.event_router.default_store
        assert store.get_events_sync("g1") == []
        rows = store.get_run_events_sync(run_id)
        types = {r["event_type"] for r in rows}
        assert {"process_exit", "graph_started", "graph_completed"} <= types
        # 投影变更信号随事件流落库：消费端据此按需拉取最新投影
        assert "projection_updated" in types
        contexts = {r["context"] for r in rows if r["event_type"] == "process_exit"}
        assert any(c and c.startswith("node=") for c in contexts)

    async def test_pause_kills_bash_process_and_resume_reruns(self, tmp_path: Path):
        # 暂停即终止 run 键进程（终止不是挂起）：bash 节点按停止收尾，
        # 续跑重置后重新执行，命令按落盘标记跳过挂死段完成
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {
                    "id": "b",
                    "task_type": "bash",
                    "command": ("if [ -f done ]; then echo skipped; else touch done; sleep 30; fi"),
                    "depends_on": [],
                },
            ]
        )
        await _await_node_running(
            env, run_id, "b", want_session=False, fail_msg="bash 节点未进入 running"
        )
        paused = await env.coord.pause_run(run_id)
        assert paused.status == "paused"
        # 决策循环随暂停冻结：组杀的退出通知滞留 FIFO，节点态在恢复后消费
        # 时落停（终态语义由恢复路径闭合，见后续断言）
        resumed = await env.coord.resume_run(run_id)
        assert resumed.status == "running"
        run = await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)
        assert not run.error
        snap = env.driver_of(run_id).state.snapshot()
        assert snap["nodes"]["b"]["status"] == "passed"
        # 续跑重置 attempts 从头计数（既有语义），命令按落盘标记跳过挂死段
        assert snap["nodes"]["b"]["attempts"] == 1

    async def test_verify_script_retry_via_run_events_replay(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run([_flaky_counter_node()])
        run = await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)
        assert not run.error
        # 节点历史 = run_events 的 node_history 事件回放（同 (gen, attempt) 取末条）
        folded: dict[tuple[int, int], dict[str, Any]] = {}
        for row in env.so.event_router.default_store.get_run_events_sync(run_id):
            if row["event_type"] != "node_history":
                continue
            data = row["data"]
            if data.get("node_id") != "flaky":
                continue
            folded[(int(data.get("gen") or 0), int(data.get("attempt") or 0))] = data
        history = sorted(
            folded.values(),
            key=lambda d: (int(d.get("gen") or 0), int(d.get("attempt") or 0)),
        )
        assert [(r["attempt"], r["status"]) for r in history] == [
            (1, "failed"),
            (2, "passed"),
        ]
        assert "验收脚本失败 (exit 1)" in history[0]["verify_reason"]
        assert history[1]["verify_reason"].startswith("验收通过")
        assert env.driver_of(run_id).state.snapshot()["nodes"]["flaky"]["output"] == "attempt-2\n"

    async def test_same_label_concurrent_runs_are_independent(self, tmp_path: Path):
        # graph_id 降级标签后无占用守卫：同 label/同程序的两个 run 并行
        # 各自执行（驱动注册表按 run_id 键，互不顶占）
        env = SwitchEnv(tmp_path)
        nodes = [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        yaml_path = env.write_workflow(nodes)
        first = await env.start_graph_run(yaml_path=yaml_path, label="same")
        second = await env.start_graph_run(yaml_path=yaml_path, label="same")
        assert second != first
        await _await_run_status(env, first, {"completed"})
        await _await_run_status(env, second, {"completed"})
        # 两个 run 都是真执行：各有自己的成员 session
        for rid in (first, second):
            assert [s.role for s in await env.coord.list_run_sessions(rid)] == [
                "executor",
            ]

    async def test_restart_after_terminal_creates_new_run(self, tmp_path: Path):
        env = SwitchEnv(tmp_path)
        nodes = [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        yaml_path = env.write_workflow(nodes)
        first_run = await env.start_graph_run(yaml_path=yaml_path, label="rerun")
        await _await_run_status(env, first_run, {"completed"})
        second_run = await env.start_graph_run(yaml_path=yaml_path, label="rerun")
        assert second_run != first_run
        await _await_run_status(env, second_run, {"completed"})
        # 全新 run 真执行：第二次有自己的成员 session
        assert [s.role for s in await env.coord.list_run_sessions(second_run)] == [
            "executor",
        ]

    async def test_graph_resume_reopens_failed_run_and_preserves_passed_node(
        self,
        tmp_path: Path,
    ):
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {"id": "good", "task_type": "bash", "command": "true"},
                {
                    "id": "flaky",
                    "task_type": "bash",
                    "command": "if test -e retry-ok; then true; else touch retry-ok; false; fi",
                },
            ]
        )
        failed = await _await_run_status(env, run_id, {"failed"})
        assert failed.status == "failed"
        before = env.driver_of(run_id).state.snapshot()["nodes"]
        assert before["good"]["status"] == "passed"
        assert before["flaky"]["status"] == "failed"
        assert before["flaky"]["attempts"] == 1

        reopened = await env.coord.restart_run(run_id)
        assert reopened.status == "running"
        final_run = await _await_run_status(env, run_id, {"completed"})
        assert final_run.run_id == run_id
        after = env.driver_of(run_id).state.snapshot()["nodes"]
        assert after["good"]["status"] == "passed"
        assert after["good"]["attempts"] == before["good"]["attempts"]
        assert after["flaky"]["status"] == "passed"
        # 续跑保留 failed 节点的 attempt 计数：重跑为第 2 次尝试，历史不覆盖
        assert after["flaky"]["attempts"] == before["flaky"]["attempts"] + 1

    async def test_failed_graph_resume_after_service_restart_uses_snapshot(
        self,
        tmp_path: Path,
    ):
        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [
                {
                    "id": "flaky",
                    "task_type": "bash",
                    "command": "if test -e retry-ok; then true; else touch retry-ok; false; fi",
                }
            ]
        )
        run_id = await env.start_graph_run(yaml_path=yaml_path)
        await _await_run_status(env, run_id, {"failed"})

        rebooted = SwitchEnv(tmp_path)
        await rebooted.recover()
        assert run_id not in getattr(rebooted.plugin, "_drivers")
        reopened = await rebooted.coord.restart_run(run_id)
        assert reopened.status == "running"

        final_run = await _await_run_status(rebooted, run_id, {"completed"})
        assert final_run.run_id == run_id
        assert rebooted.driver_of(run_id).state.status("flaky") == "passed"

    async def test_new_run_after_terminal_reexecutes_subgraph(
        self,
        tmp_path: Path,
    ):
        # 带 generator 的终态图再起全新 run：每 run 独立驱动实例（无旧拓扑
        # 复用），子图重新合并且全部 passed
        env = SwitchEnv(tmp_path)
        wd = tmp_path / "wd"
        wd.mkdir()
        _write_sub_yaml(wd)
        yaml_path = env.write_workflow(_generator_nodes())
        first_run = await env.start_graph_run(yaml_path=yaml_path, label="sub")
        await _await_run_status(env, first_run, {"completed"})
        assert env.driver_of(first_run).state.subgraph_node_ids("gen") == ["gen:s1", "gen:s2"]

        second_run = await env.start_graph_run(yaml_path=yaml_path, label="sub")
        await _await_run_status(env, second_run, {"completed"})

        # 第二轮是重新执行的子图：节点重新合并且全部 passed
        snap = env.driver_of(second_run).state.snapshot()
        assert all(info["status"] == "passed" for info in snap["nodes"].values())
        assert env.driver_of(second_run).state.subgraph_node_ids("gen") == ["gen:s1", "gen:s2"]

    async def test_restart_run_restores_driver_via_executor_thread(
        self,
        tmp_path: Path,
    ):
        # restart_run 的工厂调用走 decide 线程池工作线程：适配器不能再在
        # 构造函数里抓 running loop
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_run_status(env, run_id, {"completed"})
        members_before = await env.coord.list_run_sessions(run_id)
        reopened = await env.coord.restart_run(run_id)
        assert reopened.status == "running"
        run = await _await_run_status(env, run_id, {"completed"})
        # 重开续跑的语义固化：已完成的图无活可干，不出生新成员
        assert len(await env.coord.list_run_sessions(run_id)) == len(members_before)
        assert not run.error

    async def test_restart_run_without_label_restores_by_snapshot(
        self,
        tmp_path: Path,
    ):
        # 无 label 直连：restore 的 graph_id 取快照 blob 的权威值，
        # 不再落空串把原 completed run 污染成 unrecoverable failed

        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        run = await _direct_start_run(env, yaml_path)
        await _await_run_status(env, run.run_id, {"completed"})
        reopened = await env.coord.restart_run(run.run_id)
        assert reopened.status == "running"
        final = await _await_run_status(env, run.run_id, {"completed"})
        assert not final.error

    async def test_direct_run_snapshot_restorable_after_restart(
        self,
        tmp_path: Path,
    ):
        # 直连 run 的 graph 状态随驱动快照持久化：重启（同库重装配）后
        # restart_run 按快照重建实例，全程零 graphs 行

        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        run = await _direct_start_run(env, yaml_path)
        await _await_run_status(env, run.run_id, {"completed"})
        gid = _direct_gid(yaml_path)
        blob = await env.coord.load_driver_snapshot(run.run_id)
        assert blob is not None and blob["graph_id"] == gid
        assert blob["status"] == "completed"

        rebooted = SwitchEnv(tmp_path)
        await rebooted.recover()  # 终态 run 不重建——实例由 restart 按快照恢复
        assert run.run_id not in getattr(rebooted.plugin, "_drivers")
        reopened = await rebooted.coord.restart_run(run.run_id)
        assert reopened.status == "running"
        assert rebooted.driver_of(run.run_id).projection()["graph_id"] == gid
        final = await _await_run_status(rebooted, run.run_id, {"completed"})
        assert not final.error

    async def test_crash_recovery_resumes_run_with_driver_state(
        self,
        tmp_path: Path,
    ):
        # kill -9 模拟：run 中途崩溃（run 账面 running、成员在跑）→ 重启 →
        # boot 恢复按快照重建驱动与 run 任务，续跑完成且驱动状态一致

        env = SwitchEnv(tmp_path, auto_release=False)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        run = await _direct_start_run(env, yaml_path)
        runner = env.driver_of(run.run_id)
        await _await_node_running(env, run.run_id, "only", fail_msg="崩溃前节点未进入 running")
        crashed_sid = runner.state.snapshot()["nodes"]["only"]["workflow_controller_session_id"]

        # 模拟 kill -9：直接弃置环境（无 stop 收尾），同库重装配为新进程
        rebooted = SwitchEnv(tmp_path)
        blob = await rebooted.coord.load_driver_snapshot(run.run_id)
        assert blob is not None
        crashed_node = blob["state"]["nodes"]["only"]
        assert crashed_node["status"] == "running"
        assert crashed_node["workflow_controller_session_id"] == crashed_sid
        assert crashed_node["attempts"] == 1

        await rebooted.recover()
        ledger = await rebooted.store.get(run.run_id)
        assert ledger.status == "running"
        assert run.run_id in getattr(rebooted.plugin, "_drivers")

        release = await _await_run_status(rebooted, run.run_id, {"completed"})
        assert not release.error
        final = rebooted.driver_of(run.run_id).state.snapshot()
        assert final["nodes"]["only"]["status"] == "passed"
        # 携带崩溃前 attempts=1，重跑计到 2（状态经快照恢复而非从零）
        assert final["nodes"]["only"]["attempts"] == 2
        # 崩溃前的旧成员仍在账（历史事实），续跑新出生了自己的成员
        roles = [s.role for s in await rebooted.coord.list_run_sessions(run.run_id)]
        assert roles == ["executor", "executor"]

    async def test_crash_recovery_bash_run_with_empty_roster(self, tmp_path: Path):
        # 纯 bash run 崩溃后重启：bash 走进程桥不落 session 名册，名册恒空
        # ——boot 恢复不得以名册空跳过（判定以账面 runs 表为准）
        env = SwitchEnv(tmp_path)
        (tmp_path / "wd").mkdir()
        yaml_path = env.write_workflow(
            [{"id": "b", "task_type": "bash", "command": "sleep 30", "depends_on": []}],
        )
        run = await _direct_start_run(env, yaml_path)
        await _await_node_running(
            env, run.run_id, "b", want_session=False, fail_msg="崩溃前 bash 节点未进入 running"
        )

        # 模拟 kill -9：弃置环境，同库重装配；重启后名册空（零 session 环境）
        rebooted = SwitchEnv(tmp_path)
        rebooted.so.pool.list_sessions.return_value = []
        await rebooted.recover()

        ledger = await rebooted.store.get(run.run_id)
        assert ledger.status == "running"
        assert run.run_id in getattr(
            rebooted.plugin, "_drivers"
        )  # roster 空守卫在此早退：恢复被整体跳过
        deadline = time.monotonic() + 10.0
        while True:
            attempts = rebooted.driver_of(run.run_id).state.snapshot()["nodes"]["b"]["attempts"]
            if attempts >= 2:
                break
            if time.monotonic() > deadline:
                pytest.fail("恢复后 bash 节点未重跑")
            await asyncio.sleep(0.02)

        await rebooted.coord.stop_run(run.run_id)
        await _await_run_status(rebooted, run.run_id, {"stopped"})

    async def test_delete_run_backed_graph_no_revive_after_restart(
        self,
        tmp_path: Path,
    ):
        # 删除 run-backed 图必须级联删 run 账面与驱动快照：
        # 只 stop 会留残账，重启后 restart 按快照复活已删图
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_run_status(env, run_id, {"completed"})

        await env.coord.delete_run(run_id)
        assert await env.coord.load_driver_snapshot(run_id) is None
        assert all(r.run_id != run_id for r in await env.coord.list_runs())

        # 重启（同库重装配）：boot 恢复与重开都不得让已删图复活
        rebooted = SwitchEnv(tmp_path)
        await rebooted.recover()
        assert run_id not in getattr(rebooted.plugin, "_drivers")
        from workflow_controller.core.runs import RunNotFoundError

        with pytest.raises(RunNotFoundError):
            await rebooted.coord.restart_run(run_id)

    async def test_delete_live_run_evicts_members_and_ledger(
        self,
        tmp_path: Path,
    ):
        # 删除活图：run 级联停机后账面/快照/成员名册全删，池内成员驱逐——
        # 旧删除端口的成员清理职责由级联单事务承接
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_node_running(env, run_id, "only")
        sid = env.node_info(run_id, "only")["workflow_controller_session_id"]

        await env.coord.delete_run(run_id)

        assert await env.coord.load_driver_snapshot(run_id) is None
        assert all(r.run_id != run_id for r in await env.coord.list_runs())
        assert sid not in env.so.sessions

    async def test_delete_live_graph_cascades_and_suppresses_late_writes(
        self,
        tmp_path: Path,
    ):
        # 活图删除走 run 级联：账面/快照/事件/run 根全删；删除后零写回
        # （决策循环随 run 任务退出、驱动无旁路事件面；残余进程输出由
        # 宿主删除抑制拦在账面外）
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {"id": "b", "task_type": "bash", "command": "echo line && sleep 30"},
            ]
        )
        await _await_node_running(env, run_id, "b", want_session=False)

        def _run_event_rows() -> int:
            conn = sqlite3.connect(env.tmp / ".workflow-controller" / "events.db")
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            finally:
                conn.close()

        await asyncio.sleep(0.1)
        assert _run_event_rows() > 0  # 管道确实在写（防空断言）

        await env.coord.delete_run(run_id)
        assert _run_event_rows() == 0  # 级联清账
        await asyncio.sleep(0.1)
        assert _run_event_rows() == 0  # 删除后零写回（含残余输出采集）
        assert await env.coord.load_driver_snapshot(run_id) is None

    async def test_delete_terminal_run_backed_graph_after_restart(
        self,
        tmp_path: Path,
    ):
        # 先重启再删除：终态 run 不进恢复链（插件注册表恒空），按 run_id
        # 级联删除，残账为零
        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_run_status(env, run_id, {"completed"})

        rebooted = SwitchEnv(tmp_path)
        await rebooted.recover()
        assert run_id not in getattr(rebooted.plugin, "_drivers")

        await rebooted.coord.delete_run(run_id)
        assert await rebooted.coord.load_driver_snapshot(run_id) is None
        assert all(r.run_id != run_id for r in await rebooted.coord.list_runs())

        # 再重启一次：残账为零，重开不再复活
        rebooted2 = SwitchEnv(tmp_path)
        await rebooted2.recover()
        from workflow_controller.core.runs import RunNotFoundError

        with pytest.raises(RunNotFoundError):
            await rebooted2.coord.restart_run(run_id)

    async def test_graphs_tables_not_created_across_run_lifecycle(
        self,
        tmp_path: Path,
    ):
        # graphs 族表随删除链退役不再建库（残表建库时 DROP）：整个 run
        # 生命周期（启动/重试/完成）状态与历史分别由驱动快照与事件流承载

        env = SwitchEnv(tmp_path)
        db = tmp_path / ".workflow-controller" / "events.db"

        def _table_exists(table: str) -> bool:
            with sqlite3.connect(db) as conn:
                return (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    is not None
                )

        assert not _table_exists("graphs")
        assert not _table_exists("graph_node_history")

        run_id = await env.start_graph_run([_flaky_counter_node()])
        await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)

        assert not _table_exists("graphs")
        assert not _table_exists("graph_node_history")

    async def test_projection_shape_frozen(self, tmp_path: Path):
        # 投影协议 v2 带 description；顶层形状与节点字段集固定
        # （消费面：/state 驱动投影透传）
        from workflow_controller.plugins.graph.projection import PROJECTION_SCHEMA_VERSION

        env = SwitchEnv(tmp_path)
        run_id = await env.start_graph_run(
            [
                {"id": "b", "task_type": "bash", "command": "sleep 3 && echo hi", "depends_on": []},
            ]
        )
        # 活 run：驱动投影经应答通道取回
        state = await env.coord.run_state(run_id)
        assert state["driver"]["schema_version"] == PROJECTION_SCHEMA_VERSION == 2

        await _await_run_status(env, run_id, {"completed"}, timeout_s=20.0)
        detail = await env.coord.run_state(run_id)
        assert detail is not None
        projection = detail["driver"]
        assert projection["schema_version"] == 2
        assert set(projection) == _PROJECTION_TOP_KEYS
        assert projection["status"] == "completed"
        assert [n["id"] for n in projection["nodes"]] == ["b"]
        assert set(projection["nodes"][0]) == _PROJECTION_NODE_KEYS
        assert projection["nodes"][0]["status"] == "passed"
        assert projection["edges"] == []
        assert projection["rollback_edges"] == []
        assert projection["subgraphs"] == []
        assert projection["retired_instances"] == []

    async def test_paused_run_survives_restart_and_resumes(
        self,
        tmp_path: Path,
    ):
        # 暂停 run 跨重启存活：boot 恢复重建驱动并保持挂起，组恢复续跑
        # 同一 run（不另起新 run），驱动状态一致

        env = SwitchEnv(tmp_path, auto_release=False)
        first_run = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_node_running(env, first_run, "only")
        paused = await env.coord.pause_run(first_run)
        assert paused.status == "paused"
        await _await_graph_status(env, first_run, {"paused"})

        rebooted = SwitchEnv(tmp_path, auto_release=False)
        await rebooted.recover()
        assert (await rebooted.store.get(first_run)).status == "paused"
        assert first_run in getattr(rebooted.plugin, "_drivers")

        resumed = await rebooted.coord.resume_run(first_run)
        assert resumed.status == "running"
        # 续跑重执行 stopped 节点：等新成员出生后放行
        deadline = time.monotonic() + 5.0
        while not rebooted.so.sessions:
            if time.monotonic() > deadline:
                pytest.fail("续跑未出生新成员 session")
            await asyncio.sleep(0.02)
        rebooted.so.release_all()
        run = await _await_run_status(rebooted, first_run, {"completed"})
        assert not run.error
        final = rebooted.driver_of(first_run).state.snapshot()
        assert final["nodes"]["only"]["status"] == "passed"

    async def test_new_run_after_stop_executes_fresh(self, tmp_path: Path):
        # stop 终态后同程序新 run：全新驱动实例从头执行（无跨 run 复用）
        env = SwitchEnv(tmp_path, auto_release=False)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        first_run = await env.start_graph_run(yaml_path=yaml_path, label="after-stop")
        await _await_node_running(env, first_run, "only")
        stopped = await env.coord.stop_run(first_run)
        assert stopped.status == "stopped"

        second = await env.start_graph_run(yaml_path=yaml_path, label="after-stop")
        # 新 run 重跑 stopped 节点：等第二个成员出生后再放行
        deadline = time.monotonic() + 5.0
        while len(env.so.sessions) < 2:
            if time.monotonic() > deadline:
                pytest.fail("新 run 未出生成员 session")
            await asyncio.sleep(0.02)
        env.so.release_all()
        run = await _await_run_status(env, second, {"completed"})
        assert not run.error

    async def test_direct_start_resolves_agents_dir_from_program_ref(
        self,
        tmp_path: Path,
    ):
        from workflow_controller.api.run_api import RunAPI

        env = SwitchEnv(tmp_path)
        agents = tmp_path / "agents"
        agents.mkdir()
        yaml_path = env.tmp / "wf.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "workflow": "ag",
                    "agents_dir": "agents",
                    "nodes": [{"id": "only", "task_type": "agent", "task_description": "干活"}],
                }
            ),
            encoding="utf-8",
        )
        api = RunAPI(env.coord)
        run = await api.start(
            "",
            yaml_path,
            fmt=GRAPH_FORMAT,
            labels={"name": "agents-direct"},
            work_dir=str(tmp_path / "wd"),
        )
        # program_ref 缺省回落原始程序路径（agents_dir 相对基准）
        assert run.program_ref == str(yaml_path)
        await _await_run_status(env, run.run_id, {"completed"})
        driver = env.driver_of(run.run_id)
        # 冻结副本目录下无相对 agents 资源：解析仍指向原始 yaml 同级目录
        assert driver.agents_dir == agents

    async def test_yaml_work_dir_chain_survives_run_plane_start(
        self,
        tmp_path: Path,
    ):
        # yaml 声明的 work_dir（变量渲染/相对）在 run 平面启动链上优先生效，
        # run.work_dir 只是外部回退——旧 load_graph 的回退链不随入口换代丢失
        env = SwitchEnv(tmp_path)
        external = tmp_path / "wd"

        async def _start(yaml_path: Path) -> str:
            run = await env.coord.start_run(
                RunConfig(
                    format=GRAPH_FORMAT,
                    work_dir=str(external),
                    program_ref=str(yaml_path),
                ),
                yaml_path,
            )
            await _await_run_status(env, run.run_id, {"completed"})
            return run.run_id

        declared = _write_wd_yaml(
            env,
            "wd-vars",
            {
                "variables": {"BASE": str(tmp_path / "declared")},
                "work_dir": "{{BASE}}/run-area",
            },
        )
        assert (
            env.driver_of(await _start(declared)).work_dir
            == (tmp_path / "declared" / "run-area").resolve()
        )

        relative = _write_wd_yaml(env, "wd-rel", {"work_dir": "sub-area"})
        assert env.driver_of(await _start(relative)).work_dir == (external / "sub-area").resolve()

        undeclared = _write_wd_yaml(env, "wd-none", {})
        assert env.driver_of(await _start(undeclared)).work_dir == external.resolve()

    async def test_direct_starts_without_label_derive_stable_graph_id(
        self,
        tmp_path: Path,
    ):
        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        member_runs = []
        for _ in range(2):
            run = await _direct_start_run(env, yaml_path)
            await _await_run_status(env, run.run_id, {"completed"})
            member_runs.append(run.run_id)
        # 同一程序重复直连：graph_id 稳定（label 派生，不随 run_id 漂移）
        expected = _direct_gid(yaml_path)
        assert env.driver_of(member_runs[0]).projection()["graph_id"] == expected
        assert env.driver_of(member_runs[1]).projection()["graph_id"] == expected
        # 第二次是全新 run 的真执行：各有自己的成员 session，而非零执行空转
        for rid in member_runs:
            assert [s.role for s in await env.coord.list_run_sessions(rid)] == [
                "executor",
            ]

    async def test_direct_rerun_discards_stale_instance(
        self,
        tmp_path: Path,
    ):
        # 全新 run 丢弃上一实例（终态实例全 passed 无就绪节点，复用即零执行
        # 空转）：反复直连重跑每次真正执行
        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )

        async def _direct_start() -> Any:
            return await _direct_start_run(env, yaml_path)

        first = await _direct_start()
        await _await_run_status(env, first.run_id, {"completed"})
        first_driver = env.driver_of(first.run_id)
        assert first_driver.state.snapshot()["nodes"]["only"]["status"] == "passed"
        second = await _direct_start()
        run = await _await_run_status(env, second.run_id, {"completed"})
        assert not run.error
        # 第二个 run 是全新实例从头跑（非终态空转）
        driver = env.driver_of(second.run_id)
        assert driver is not first_driver
        snap = driver.state.snapshot()
        assert snap["nodes"]["only"]["status"] == "passed"
        assert snap["nodes"]["only"]["attempts"] == 1

    async def test_direct_rerun_retranslates_edited_program(
        self,
        tmp_path: Path,
    ):
        # 编辑程序后重跑：全新直连 run 的执行锚定本轮冻结副本，
        # 不能复用上一轮终态实例的旧翻译 IR（改动被静默丢弃）

        env = SwitchEnv(tmp_path)
        yaml_path = env.write_workflow(
            [{"id": "only1", "task_type": "agent", "task_description": "干活"}]
        )

        async def _direct_start() -> Any:
            return await _direct_start_run(env, yaml_path)

        first = await _direct_start()
        await _await_run_status(env, first.run_id, {"completed"})
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "workflow": "switch",
                    "max_parallel": 2,
                    "work_dir": ".",
                    "nodes": [{"id": "only2", "task_type": "agent", "task_description": "干活"}],
                }
            ),
            encoding="utf-8",
        )

        second = await _direct_start()
        await _await_run_status(env, second.run_id, {"completed"})
        runner = env.driver_of(second.run_id)
        snap = runner.state.snapshot()
        # 本轮程序真正生效：执行节点是 only2，且全 passed（真执行非空转）
        assert set(snap["nodes"]) == {"only2"}
        assert all(info["status"] == "passed" for info in snap["nodes"].values())
        assert [s.role for s in await env.coord.list_run_sessions(second.run_id)] == [
            "executor",
        ]
        # 快照的程序定位随本轮冻结程序刷新（yaml_path 指向 run 2 的冻结副本）
        blob = await env.coord.load_driver_snapshot(second.run_id)
        frozen = env.coord.run_data_root(second.run_id) / "program" / yaml_path.name
        assert blob["yaml_path"] == str(frozen)

    async def test_direct_rerun_generator_program_second_round(
        self,
        tmp_path: Path,
    ):
        # 直连无 label 的 generator 程序第二轮：每 run 全新实例（无旧行
        # 水合），重跑 generator 重新合并子图不撞 duplicate node id

        env = SwitchEnv(tmp_path)
        wd = tmp_path / "wd"
        wd.mkdir()
        _write_sub_yaml(wd)
        yaml_path = env.write_workflow(_generator_nodes())

        async def _direct_start() -> Any:
            return await _direct_start_run(env, yaml_path, wd)

        first = await _direct_start()
        await _await_run_status(env, first.run_id, {"completed"})
        assert env.driver_of(first.run_id).state.subgraph_node_ids("gen") == [
            "gen:s1",
            "gen:s2",
        ]

        second = await _direct_start()
        run = await _await_run_status(env, second.run_id, {"completed", "failed"})
        assert run.status == "completed"
        assert not run.error
        # 第二轮是重新执行的子图：全新实例上重新合并且全部 passed
        runner = env.driver_of(second.run_id)
        snap = runner.state.snapshot()
        assert runner.state.subgraph_node_ids("gen") == ["gen:s1", "gen:s2"]
        assert all(info["status"] == "passed" for info in snap["nodes"].values())

    async def test_driver_op_update_model_and_reload(self, tmp_path: Path):
        """驱动操作通道：活 run 上 reload/update_model 在 run 任务内串行执行并落账。"""
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        runner = env.driver_of(run_id)
        await _await_node_running(env, run_id, "only")

        result = await env.coord.request_driver_op(
            run_id,
            "update_model",
            {"model": "m2", "provider_index": 1},
        )

        assert result == {"updated": True, "model": "m2", "provider_index": 1}
        # 热切换即时生效（新出生成员用新模型）
        assert runner.runtime_options()["model"] == "m2"

        reloaded = await env.coord.request_driver_op(run_id, "reload")

        assert reloaded == {"reloaded": True}
        assert runner.runtime_options()["model"] == "m2", "reload 应从最新快照重读并保持热切换值"

        env.so.release_all()
        run = await _await_run_status(env, run_id, {"completed"})
        assert not run.error
        # 热切换随快照边界持久化：终态快照携带新模型（恢复/重开不丢）
        blob = await env.coord.load_driver_snapshot(run_id)
        assert blob["options"]["model"] == "m2"
        assert blob["options"]["provider_index"] == 1

    async def test_driver_op_unknown_op_rejected(self, tmp_path: Path):
        """未知操作经应答通道回异常原语，run 不因此中毒。"""
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        await _await_node_running(env, run_id, "only")

        with pytest.raises(NotImplementedError, match="未知驱动操作"):
            await env.coord.request_driver_op(run_id, "no-such-op")
        run = await env.store.get(run_id)
        assert run.status == "running", "操作异常不构成驱动失效"
        env.so.release_all()
        await _await_run_status(env, run_id, {"completed"})

    async def test_pause_resume_roundtrip_via_run_plane(self, tmp_path: Path):
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        deadline = time.monotonic() + 5.0
        while not env.so.sessions or not all(
            s.loop_status == "yes" for s in env.so.sessions.values()
        ):
            if time.monotonic() > deadline:
                pytest.fail("成员 session 未进入 running")
            await asyncio.sleep(0.02)
        paused = await env.coord.pause_run(run_id)
        assert paused.status == "paused"
        resumed = await env.coord.resume_run(run_id)
        assert resumed.status == "running"
        env.so.release_all()
        run = await _await_run_status(env, run_id, {"completed"})
        assert run.status == "completed"

    async def test_member_level_pause_resume_syncs_node_state(self, tmp_path: Path):
        """成员级控制两层断言：run 平面成员操作 → 驱动侧节点态与 graph 态随行。"""
        env = SwitchEnv(tmp_path, auto_release=False)
        run_id = await env.start_graph_run(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        driver = env.driver_of(run_id)
        await _await_node_running(env, run_id, "only")
        sid = env.node_info(run_id, "only")["workflow_controller_session_id"]

        await env.coord.pause_run_session(run_id, sid)
        await _await_graph_status(env, run_id, {"paused"})
        assert driver.state.snapshot()["nodes"]["only"]["status"] == "stopped"

        await env.coord.resume_run_session(run_id, sid)
        deadline = time.monotonic() + 5.0
        while driver.state.snapshot()["nodes"]["only"]["status"] != "running":
            if time.monotonic() > deadline:
                pytest.fail("成员恢复未拉回节点运行态")
            await asyncio.sleep(0.02)
        assert driver.state.snapshot()["status"] == "running"

        env.so.release_all()
        run = await _await_run_status(env, run_id, {"completed"})
        assert run.status == "completed"

    async def test_factory_resolves_reloaded_plugin_instance(
        self,
        tmp_path: Path,
    ):
        # 工厂被协调器 boot 期冻结：插件 toggle 重载换新实例后，
        # create/restore 须落到 registry 当前活跃实例，
        # 不能仍登记进已 uninstall 的旧实例（对 run 平面不可见且不可控）

        env = SwitchEnv(tmp_path)
        old = env.plugin
        # 模拟 reload_plugin（toggle off→on）：旧实例 uninstall，registry 换新
        new = GraphPlugin(
            PluginConfig(name="graph", version="0.1.0", enabled=True),
            tmp_path / "config.yaml",
        )
        ctx = getattr(old, "_ctx")
        old.uninstall()
        new.install(ctx)
        env.so.plugins[:] = [new]
        env.plugin = new

        yaml_path = env.write_workflow(
            [{"id": "only", "task_type": "agent", "task_description": "干活"}]
        )
        run = await _direct_start_run(env, yaml_path)
        await _await_run_status(env, run.run_id, {"completed"})
        # 驱动实例登记在新实例上；旧实例零登记（uninstall 清空）
        assert run.run_id in getattr(new, "_drivers")
        assert not getattr(old, "_drivers")
