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

"""工作区隔离的 run 平面全链路：graph 引擎发工作区动作、协调器真实执行，

回执后 bash 进程在作用域工作区内执行、验收通过经真实 git 集成回根检出。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from workflow_controller.driver.hooks import BarrierProceed

from .run_plane_helpers import GraphRunPlaneEnv, await_run_status


def _write_traceback_workflow(tmp: Path, nodes: list[dict]) -> Path:
    """开启工作区隔离的节点工作流。"""
    path = tmp / "wf-traceback.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workflow": "traceback",
                "max_parallel": 2,
                "work_dir": ".",
                "traceback": {"enabled": True},
                "nodes": nodes,
            }
        ),
        encoding="utf-8",
    )
    return path


async def test_bash_run_writes_inside_scope_workspace(tmp_path: Path) -> None:
    """开启隔离的 bash run：产出经集成回根检出，作用域工作区默认释放。"""
    env = GraphRunPlaneEnv(tmp_path)
    yaml_path = _write_traceback_workflow(
        env.tmp, [{"id": "n1", "task_type": "bash", "command": "echo scoped > out.txt"}]
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)

    run = await await_run_status(env, run_id, {"completed"})
    assert run.status == "completed"

    work_root = env.tmp / "wd"
    assert (work_root / "out.txt").read_text(encoding="utf-8").strip() == "scoped"
    scopes_dir = work_root / ".workflow-controller" / "scopes" / run_id
    workspaces = scopes_dir / "workspaces"
    assert not (workspaces.exists() and any(workspaces.iterdir()))  # 集成后默认释放
    assert env.node_info(run_id, "n1")["status"] == "passed"


async def test_subgraph_child_with_colon_id_gets_valid_workspace_branch(
    tmp_path: Path,
) -> None:
    """子图子节点（ID 含冒号）建区：分支名转义后合法，run 完成且产出集成。"""
    env = GraphRunPlaneEnv(tmp_path)
    work_root = env.tmp / "wd"
    work_root.mkdir()
    (work_root / "sub.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": "sub",
                "max_parallel": 1,
                "work_dir": ".",
                "nodes": [
                    {
                        "id": "s1",
                        "task_type": "bash",
                        "command": "echo sub-done > sub-out.txt",
                        "acceptance": {"enabled": False},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [
            {
                "id": "gen",
                "task_type": "subgraph",
                "file": "sub.yaml",
                "acceptance": {"enabled": False},
            }
        ],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)

    run = await await_run_status(env, run_id, {"completed"})

    assert run.status == "completed"
    assert (work_root / "sub-out.txt").read_text(encoding="utf-8").strip() == "sub-done"
    assert env.node_info(run_id, "gen:s1")["status"] == "passed"
    # 子节点作用域分支按转义键留存：冒号编码为 %3A，为合法分支名
    repo_git = work_root / ".workflow-controller" / "scopes" / run_id / "repo.git"
    assert (repo_git / "refs" / "heads" / "scope" / "gen%3As1-w1").is_file()


_SUBGRAPH = {
    "workflow": "sub",
    "max_parallel": 1,
    "work_dir": ".",
    "nodes": [
        {
            "id": "s1",
            "task_type": "bash",
            "command": "echo sub-done > sub-out.txt",
            "acceptance": {"enabled": False},
        }
    ],
}


@pytest.mark.parametrize("generator_type", ["bash", "agent"])
async def test_generator_reads_subgraph_from_its_scope_workspace(
    tmp_path: Path, generator_type: str
) -> None:
    """开启隔离的 generator：子图 yaml 写在作用域工作区、尚未集成，generator 从该工作区读出。

    bash 进程型由生成命令落盘；agent 型由成员会话写进自己的工作目录。
    子图并入后子节点照常隔离执行，整图完成。
    """
    env = GraphRunPlaneEnv(tmp_path)
    subgraph_yaml = yaml.safe_dump(_SUBGRAPH)
    env.so.on_member_spawn = lambda cfg: (Path(cfg.cwd) / "sub.yaml").write_text(
        subgraph_yaml, encoding="utf-8"
    )
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [
            {
                "id": "gen",
                "task_type": "generator",
                "generator_type": generator_type,
                "command": f"cat > sub.yaml <<'EOF'\n{subgraph_yaml}EOF",
                "subgraph": {"output": "sub.yaml"},
                "acceptance": {"enabled": True, "type": "script", "script": "test -f sub.yaml"},
            }
        ],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)

    run = await await_run_status(env, run_id, {"completed", "failed"})

    assert run.status == "completed", env.node_info(run_id, "gen")["failure_reason"]
    work_root = env.tmp / "wd"
    assert (work_root / "sub-out.txt").read_text(encoding="utf-8").strip() == "sub-done"
    assert env.node_info(run_id, "gen")["status"] == "passed"
    assert env.node_info(run_id, "gen:s1")["status"] == "passed"


async def test_parallel_nodes_isolate_then_integrate_into_root(tmp_path: Path) -> None:
    """双节点并行写不相交路径：隔离期互不可见，依次集成后根检出含双方产出。"""
    env = GraphRunPlaneEnv(tmp_path)
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [
            {"id": "n1", "task_type": "bash", "command": "echo one > n1.txt"},
            {"id": "n2", "task_type": "bash", "command": "echo two > n2.txt"},
        ],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)

    run = await await_run_status(env, run_id, {"completed"})
    assert run.status == "completed"

    work_root = env.tmp / "wd"
    assert (work_root / "n1.txt").read_text(encoding="utf-8").strip() == "one"
    assert (work_root / "n2.txt").read_text(encoding="utf-8").strip() == "two"
    assert env.node_info(run_id, "n1")["status"] == "passed"
    assert env.node_info(run_id, "n2")["status"] == "passed"

    scopes_dir = work_root / ".workflow-controller" / "scopes" / run_id
    workspaces = scopes_dir / "workspaces"
    assert not (workspaces.exists() and any(workspaces.iterdir()))  # 双双默认释放

    # 打点落账：每节点 scope_start + terminal 各一行，编排状态引用非空
    with sqlite3.connect(env.store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT scope_id, trigger, workspace_commit, orchestration_state_ref"
            " FROM scope_checkpoints ORDER BY scope_id, trigger"
        ).fetchall()
    by_scope: dict[str, set[str]] = {}
    for row in rows:
        assert len(row["workspace_commit"]) == 40
        assert row["orchestration_state_ref"].startswith("driver-snapshot:")
        by_scope.setdefault(row["scope_id"], set()).add(row["trigger"])
    assert len(by_scope) == 2
    assert all(triggers == {"scope_start", "terminal"} for triggers in by_scope.values())


async def test_crash_recovery_rebuilds_workspace_tree_and_resumes(tmp_path: Path) -> None:
    """崩溃后重开：树运行时经深度恢复从作用域账本表重建后续跑。

    n1 已集成（账面重建保留）、n2 崩溃时在跑（重排走欠账清理：旧作用域
    废弃、新工作区重跑）；集成线与根检出原样，根代次不变。
    """
    env = GraphRunPlaneEnv(tmp_path)
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [
            {
                "id": "n1",
                "task_type": "bash",
                "command": "echo one > n1.txt",
                "acceptance": {"enabled": True, "type": "script", "script": "true"},
            },
            {
                "id": "n2",
                "task_type": "bash",
                "command": "sleep 300",
                "acceptance": {"enabled": False},
            },
        ],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)

    deadline = time.monotonic() + 20.0
    while True:
        n1 = env.node_info(run_id, "n1")
        n2 = env.node_info(run_id, "n2")
        if n1["status"] == "passed" and n2["status"] == "running":
            break
        if time.monotonic() > deadline:
            pytest.fail("崩溃前现场未就绪")
        await asyncio.sleep(0.05)
    old_scopes = {s["scope_id"]: s for s in await env.coord.list_run_scopes(run_id)}
    old_n1 = next(s["scope_id"] for s in old_scopes.values() if s["owner_ref"] == "n1")
    old_n2 = next(s["scope_id"] for s in old_scopes.values() if s["owner_ref"] == "n2")

    # 模拟 kill -9：弃置环境（无 stop 收尾），同库重装配后 boot 恢复
    rebooted = GraphRunPlaneEnv(tmp_path)
    await rebooted.recover()

    # 恢复后 n2 重排：旧作用域经欠账清理废弃、新作用域建区重跑
    deadline = time.monotonic() + 30.0
    while True:
        scopes = {s["scope_id"]: s for s in await rebooted.coord.list_run_scopes(run_id)}
        new_n2 = [s for sid, s in scopes.items() if s["owner_ref"] == "n2" and sid != old_n2]
        if new_n2 and scopes[old_n2]["state"] == "discarded":
            break
        if time.monotonic() > deadline:
            pytest.fail("恢复后未完成旧作用域废弃与新工作区重建")
        await asyncio.sleep(0.05)

    # 账面重建断言：n1 已集成的行保留、根代次不变、n2 旧作用域换代废弃
    assert scopes[old_n1]["state"] in ("integrated", "released")
    assert (
        scopes[old_n1]["integration_base_commit"] == old_scopes[old_n1]["integration_base_commit"]
    )
    assert scopes[f"{run_id}-root"]["root_epoch"] == 1
    assert scopes[old_n2]["scope_epoch"] == 2
    assert new_n2[0]["state"] == "active"

    await rebooted.coord.stop_run(run_id)
    # 集成线与根检出经恢复原样：n1 产出仍在
    work_root = tmp_path / "wd"
    assert (work_root / "n1.txt").read_text(encoding="utf-8").strip() == "one"


async def _wait_until(predicate, timeout_s: float, message: str) -> None:
    """轮询等待条件成立，超时按给定信息判失败。"""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(message)
        await asyncio.sleep(0.02)


async def test_cold_recovery_reused_member_session_integrates_into_root(tmp_path: Path) -> None:
    """进程重启后复用在途成员会话：终态提交 → 验收 → 集成照常走完。

    重启后工作区树不在内存，终态提交与集成按盘上配置重建树执行；节点
    产出进根检出、作用域工作区释放，下游从集成头建区读得到产出。
    """
    env = GraphRunPlaneEnv(tmp_path, auto_release=False)
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [
            {
                "id": "a",
                "task_type": "agent",
                "acceptance": {"enabled": True, "type": "script", "script": "test -f a.txt"},
            },
            {
                "id": "b",
                "task_type": "bash",
                "command": "test -f a.txt && echo ok > b.txt",
                "depends_on": ["a"],
            },
        ],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)
    await _wait_until(
        lambda: env.node_info(run_id, "a")["workflow_controller_session_id"],
        30.0,
        "崩溃前节点 a 的成员未出生",
    )
    crashed = env.node_info(run_id, "a")
    sid = crashed["workflow_controller_session_id"]
    scope_a = crashed["workspace_scope_id"]
    # 成员崩溃前已落在作用域工作区的产物（假会话不写文件，由测试代写）
    (Path(crashed["workspace_path"]) / "a.txt").write_text("from-a", encoding="utf-8")

    # 模拟 kill -9：弃置环境（无 stop 收尾）；重启后成员从持久层恢复为可续跑的暂停态
    rebooted = GraphRunPlaneEnv(tmp_path)
    rebooted.so.restore_paused_session(sid)
    await rebooted.recover()
    resumed = rebooted.so.sessions[sid]
    await _wait_until(lambda: resumed.state.value == "running", 30.0, "恢复后成员会话未被续跑")
    assert run_id not in rebooted.coord._workspace_trees  # 工作区树不在内存

    rebooted.so.release_all()
    await _wait_until(
        lambda: rebooted.node_info(run_id, "b")["status"] != "pending",
        60.0,
        "恢复后 a 未收口、下游 b 未起步",
    )
    work_root = tmp_path / "wd"
    assert (work_root / "a.txt").read_text(encoding="utf-8") == "from-a"
    scope_states = {
        scope["scope_id"]: scope["state"] for scope in await rebooted.coord.list_run_scopes(run_id)
    }
    assert scope_states.get(scope_a) in ("integrated", "released")
    assert not Path(crashed["workspace_path"]).exists()  # 集成后工作区释放

    run = await await_run_status(rebooted, run_id, {"completed"})
    assert run.status == "completed"
    assert (work_root / "b.txt").read_text(encoding="utf-8").strip() == "ok"
    assert rebooted.node_info(run_id, "a")["status"] == "passed"
    assert rebooted.node_info(run_id, "b")["status"] == "passed"
    with sqlite3.connect(rebooted.store.db_path) as conn:
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT trigger FROM scope_checkpoints WHERE scope_id = ?", (scope_a,)
            )
        }
    assert triggers == {"scope_start", "terminal"}


async def test_cold_recovery_dispatch_barrier_rebuilds_workspace_tree(
    tmp_path: Path, full_capability_runtime: str
) -> None:
    """进程重启后工作区树不在内存：成员会话的屏障重建树后执行，不静默直通。

    节点沿用原作用域续跑；task 派发拿到子工作区 cwd，副作用落组检查点，
    作用域查询带回节点属主（属主随账本行落表）。
    """
    env = GraphRunPlaneEnv(tmp_path, auto_release=False)
    yaml_path = _write_traceback_workflow(
        env.tmp,
        [{"id": "a", "task_type": "agent", "acceptance": {"enabled": False}}],
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path)
    await _wait_until(
        lambda: env.node_info(run_id, "a")["workflow_controller_session_id"],
        30.0,
        "崩溃前节点 a 的成员未出生",
    )
    crashed = env.node_info(run_id, "a")
    sid = crashed["workflow_controller_session_id"]

    # 模拟 kill -9：弃置环境（无 stop 收尾）；重启后成员从持久层恢复为可续跑的暂停态
    rebooted = GraphRunPlaneEnv(tmp_path)
    rebooted.so.restore_paused_session(sid)
    await rebooted.recover()
    resumed = rebooted.so.sessions[sid]
    await _wait_until(lambda: resumed.state.value == "running", 30.0, "恢复后成员会话未被续跑")
    assert run_id not in rebooted.coord._workspace_trees  # 工作区树不在内存

    dispatch = await rebooted.coord.submit_dispatch_barrier(
        sid,
        "dispatch",
        {"tool_name": "Task", "tool_use_id": "toolu-1", "prompt_id": "msg-1"},
        runtime=full_capability_runtime,
    )
    assert isinstance(dispatch, BarrierProceed)
    assert dispatch.child_cwd and Path(dispatch.child_cwd).is_dir()
    side_effect = await rebooted.coord.submit_dispatch_barrier(
        sid,
        "side_effect",
        {"tool_name": "Bash", "tool_use_id": "toolu-2", "prompt_id": "msg-2"},
        runtime=full_capability_runtime,
    )
    assert isinstance(side_effect, BarrierProceed)

    scope_a = crashed["workspace_scope_id"]
    group_rows = [
        (row["scope_id"], row["dispatch_anchor"]["tool_use_id"])
        for row in await rebooted.coord.list_run_checkpoints(run_id)
        if row["trigger"] == "before_child"
    ]
    assert group_rows == [(scope_a, "toolu-1"), (scope_a, "toolu-2")]
    scopes = {s["scope_id"]: s for s in await rebooted.coord.list_run_scopes(run_id)}
    assert (scopes[scope_a]["owner_ref"], scopes[scope_a]["state"]) == ("a", "active")
    await rebooted.coord.stop_run(run_id)
