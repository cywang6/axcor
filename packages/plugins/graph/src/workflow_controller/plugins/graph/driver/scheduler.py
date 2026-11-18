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

"""调度决策（纯函数）：就绪集、失败传播、终局清扫、回滚数学与子图恢复策略。

只读 world（dag + state）产出决策或直接推进纯状态；停任务/停会话/读文件
等机制由宿主执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from workflow_controller.plugins.graph.driver.dag import (
    prefix_subgraph_nodes,
    subgraph_topology,
)
from workflow_controller.plugins.graph.template import render

if TYPE_CHECKING:
    from workflow_controller.plugins.graph.driver.dag import DAG
    from workflow_controller.plugins.graph.driver.state import GraphState

logger = logging.getLogger(__name__)


def derive_graph_status(snapshot: dict[str, Any]) -> str:
    """按全体节点状态推导 graph 状态：running > failed > paused > completed。

    只有 pending 而无更高优先级状态时保持当前值：graph 刚置 running 而
    节点尚未启动的窗口（如 agents 软链预备轮）不可降级回 pending。
    """
    nodes = snapshot.get("nodes", {})
    statuses = [info.get("status") for info in nodes.values()]
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "stopped" in statuses:
        return "paused"
    if statuses and all(s in ("passed", "skipped") for s in statuses):
        return "completed"
    return snapshot.get("status", "pending")


def derive_status_after_rollback(snapshot: dict[str, Any]) -> str:
    """回滚重置后的 graph 状态：缺省恢复 running（主循环还会重跑新一代）。"""
    statuses = [info.get("status") for info in snapshot["nodes"].values()]
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "stopped" in statuses:
        return "paused"
    return "running"


def _dependency_allows_run(state: GraphState, node_id: str) -> bool:
    return state.status(node_id) == "passed" or (
        state.status(node_id) == "skipped" and state.attempts(node_id) > 0
    )


def select_ready_nodes(dag: DAG, state: GraphState) -> list[dict]:
    """就绪节点：依赖全部终态、自身 pending；run_on_failure 节点放行。"""
    ready = dag.get_ready(state.terminal_ids())
    ready_nodes = []
    for n in ready:
        if not (
            state.status(n["id"]) == "pending"
            and (
                n.get("run_on_failure")
                or all(_dependency_allows_run(state, d) for d in (n.get("depends_on") or []))
            )
        ):
            continue
        ready_nodes.append(n)
    return ready_nodes


def propagate_failure_skips(dag: DAG, state: GraphState) -> bool:
    """失败沿普通节点传播：依赖已终态但并非全过的节点标 skipped。

    否则它们的 run_on_failure 下游永远等不到依赖变 skipped，失败无法
    传播。stopped 不在终态集，依赖它的节点不会被选中（暂停语义不受影响）。
    """
    progressed = False
    for n in dag.get_ready(state.terminal_ids()):
        if state.status(n["id"]) != "pending" or n.get("run_on_failure"):
            continue
        deps = n.get("depends_on") or []
        if all(_dependency_allows_run(state, d) for d in deps):
            continue
        state.set_node_status(n["id"], "skipped")
        progressed = True
    return progressed


def skip_pending_on_failure(dag: DAG, state: GraphState) -> None:
    """终局清扫：graph 失败后，未执行的节点一律 skipped。"""
    for nid in dag.all_ids():
        if state.status(nid) == "pending":
            state.set_node_status(nid, "skipped")


@dataclass
class RollbackPlan:
    """回滚执行计划：reset 集、需停的运行节点与待废弃的旧子图。"""

    target: str
    failed_node: str
    failed_gen: int
    new_gen: int
    reset_nodes: set[str]
    stop_nodes: set[str]
    parent_gen_node: str | None
    old_parent_gen: int
    discard_subgraphs: dict[str, list[str]] = field(default_factory=dict)


def plan_rollback(
    state: GraphState,
    dag: DAG,
    target: str,
    failed_node: str,
    failed_gen: int,
) -> RollbackPlan | None:
    """回滚决策：reset 集/新代/废弃子图一次算清；陈旧结果返回 None。

    陈旧判定——先应用的回滚可能已把失败节点 reset 到新代/pending，此时
    本结果是陈旧的，丢弃以免重复生成新代、记录错误的回滚边并归档一个
    从未执行过的实例。

    子图内回滚（target 是子图节点）：gen-sub（父 generator）也 reset 进
    新回滚栏，但编排完成不重新 run、状态保持 passed；主图下游一起 reset，
    子图重跑通过后这些下游要重新运行。
    """
    if state.status(failed_node) != "failed" or state.gen(failed_node) != failed_gen:
        logger.warning(
            "rollback: discard stale rollback result from %s (produced in gen %s, now %s/gen %s)",
            failed_node,
            failed_gen,
            state.status(failed_node),
            state.gen(failed_node),
        )
        return None
    new_gen = state.max_gen() + 1
    reset_nodes = dag.descendants(target) | {target, failed_node}
    parent_gen_node = state.subgraph_parent(target)
    old_parent_gen = 0
    if parent_gen_node:
        old_parent_gen = state.gen(parent_gen_node)
        reset_nodes.add(parent_gen_node)
    snap = state.snapshot()
    stop_nodes = {
        nid
        for nid in reset_nodes - {failed_node}
        if (snap["nodes"].get(nid, {}) or {}).get("status") == "running"
    }
    discard_subgraphs = {
        nid: old_ids
        for nid in reset_nodes
        if nid != parent_gen_node and (old_ids := state.subgraph_node_ids(nid))
    }
    return RollbackPlan(
        target=target,
        failed_node=failed_node,
        failed_gen=failed_gen,
        new_gen=new_gen,
        reset_nodes=reset_nodes,
        stop_nodes=stop_nodes,
        parent_gen_node=parent_gen_node,
        old_parent_gen=old_parent_gen,
        discard_subgraphs=discard_subgraphs,
    )


def apply_rollback(state: GraphState, dag: DAG, plan: RollbackPlan) -> None:
    """按计划落账：归档重置、废弃旧子图、记回滚边与预算。"""
    for nid in plan.reset_nodes:
        state.reset_rollback(nid, plan.new_gen)
    # 废弃旧子图节点：DAG + state 一起移除并清 node_ids——重新生成的子图
    # 才能重新合并（否则看到旧 ID 列表立即返回，系统仍执行旧拓扑）。
    for nid, old_ids in plan.discard_subgraphs.items():
        dag.remove_nodes(old_ids, parent_node_id=nid)
        state.remove_nodes(old_ids)
        state.set_subgraph_node_ids(nid, [])
        state.set_subgraph_nodes(nid, [])
    if plan.parent_gen_node:
        state.set_node_status(plan.parent_gen_node, "passed")
        state.add_rollback_edge(
            plan.parent_gen_node,
            plan.old_parent_gen,
            plan.parent_gen_node,
            plan.new_gen,
        )
    else:
        state.add_rollback_edge(
            plan.failed_node,
            plan.failed_gen,
            plan.target,
            plan.new_gen,
        )
    state.increment_rollback(plan.failed_node, plan.target)


@dataclass
class SubgraphRestore:
    """待恢复子图的定位信息（宿主据此读子 yaml）。"""

    node: dict
    sub_ids: list[str]
    output_rel: str


def _subgraph_shape_error(config: dict[str, Any], parent_wf_name: str) -> str | None:
    """基础形状校验：nodes 非空、子工作流与父不同名。"""
    if not (config.get("nodes") or []):
        return "subgraph yaml 缺少 nodes"
    # 同名递归只认显式命名：子 yaml 通常不写 workflow 键，双方缺省
    # （default）相同不构成递归证据；真自引用会被下方一层嵌套检查拦下。
    sub_wf = config.get("workflow")
    if sub_wf and parent_wf_name and sub_wf == parent_wf_name:
        return f"subgraph workflow 与父工作流同名: {parent_wf_name}"
    return None


def _subgraph_nesting_error(config: dict[str, Any]) -> str | None:
    """一层嵌套校验：子图内不得再出现 generator。"""
    for n in config.get("nodes") or []:
        if n.get("task_type") == "generator":
            return f"subgraph 节点 '{n.get('id')}' 是 generator：只支持 1 层嵌套"
    return None


def _build_subgraph_dag(config: dict[str, Any]) -> DAG | str:
    """构造子图 DAG（构造即校验无环、无未知依赖）；失败返回错误串。"""
    from workflow_controller.plugins.graph.driver.dag import DAG

    try:
        return DAG(config["nodes"])
    except Exception as e:
        return f"subgraph DAG 校验失败: {e}"


def _subgraph_rollback_error(config: dict[str, Any], dag: DAG) -> str | None:
    """rollback_to 校验：必须存在且是失败节点的祖先（与父图构造器一致）。"""
    # 否则合并后验收失败时回滚会 reset 不存在的节点或产生错误的重置范围
    for n in config.get("nodes") or []:
        acc = n.get("acceptance") or {}
        rt = acc.get("rollback_to", "") or ""
        if acc.get("on_exhaust") == "rollback" and rt:
            if rt not in dag.all_ids():
                return f"subgraph 节点 '{n.get('id')}' rollback_to '{rt}' 不存在"
            if rt not in dag.ancestors(n["id"]):
                return f"subgraph 节点 '{n.get('id')}' rollback_to '{rt}' 必须是其祖先"
    return None


def _subgraph_on_exhaust_error(config: dict[str, Any]) -> str | None:
    """on_exhaust 词表校验：归一后仍未入词表即拒绝，不静默当成 stop。"""
    from workflow_controller.plugins.graph.translator import ON_EXHAUST_VOCAB, _normalize_on_exhaust

    for n in config.get("nodes") or []:
        on_exhaust = (n.get("acceptance") or {}).get("on_exhaust")
        if on_exhaust is None:
            on_exhaust = _normalize_on_exhaust(n.get("on_exhaust"))
        if on_exhaust is not None and on_exhaust not in ON_EXHAUST_VOCAB:
            return (
                f"subgraph 节点 '{n.get('id')}' on_exhaust 值 {on_exhaust!r} "
                f"不在词表 {sorted(ON_EXHAUST_VOCAB)}"
            )
    return None


def validate_subgraph_config(config: Any, parent_wf_name: str) -> tuple[bool, str]:
    """硬校验子工作流配置：有 nodes/与父不同名/只一层嵌套/回滚目标合法。

    输入是已解析归一的配置 dict（读文件在宿主）；返回 (是否就绪, 原因)。
    """
    if not isinstance(config, dict):
        return False, "subgraph yaml 缺少 nodes"
    error = _subgraph_shape_error(config, parent_wf_name)
    if error is not None:
        return False, error
    error = _subgraph_nesting_error(config)
    if error is not None:
        return False, error
    dag = _build_subgraph_dag(config)
    if isinstance(dag, str):
        return False, dag
    error = _subgraph_rollback_error(config, dag)
    if error is not None:
        return False, error
    error = _subgraph_on_exhaust_error(config)
    if error is not None:
        return False, error
    return True, ""


def subgraph_restore_specs(
    dag: DAG,
    state: GraphState,
    variables: dict,
) -> list[SubgraphRestore]:
    """找出需要重建拓扑的 generator：已持久化子节点但不在当前 DAG。

    子图节点在运行时动态 add_nodes 合并进 DAG，持久化只存 subgraph_node_ids
    （id 列表，不含 depends_on）——后端重启后 DAG 只剩主图节点，须从子图
    yaml 重建带前缀节点补回。output 优先复用生成时持久化的实际路径，否则
    用完整工作流变量渲染兜底。
    """
    specs: list[SubgraphRestore] = []
    for n in dag.nodes:
        if n.get("task_type") != "generator":
            continue
        sub_ids = state.subgraph_node_ids(n["id"])
        if not sub_ids or any(sid in dag.all_ids() for sid in sub_ids):
            continue
        output_rel = state.subgraph_output(n["id"])
        if not output_rel:
            output_rel = render(
                (n.get("subgraph") or {}).get("output", ""),
                {**variables, "node_id": n["id"], "attempt": "0"},
            )
        specs.append(SubgraphRestore(node=n, sub_ids=sub_ids, output_rel=output_rel))
    return specs


def restored_subgraph_plan(
    generator_id: str,
    sub_ids: list[str],
    sub_config: Any,
) -> list[dict] | None:
    """校验恢复的子图配置：节点集与持久化 id 一致才返回待合并节点。

    文件可解析还不够：生成文件可能被另一次运行覆盖或改动——不一致时按
    当前文件重建会残留 pending 子节点永久挂起、或 state 缺失新节点导致
    调度抛错无法续跑。
    """
    if not isinstance(sub_config, dict) or not (sub_config.get("nodes") or []):
        return None
    prefixed = prefix_subgraph_nodes(sub_config, generator_id)
    if {pn["id"] for pn in prefixed} != set(sub_ids):
        return None
    return prefixed


def merge_restored_subgraph(
    state: GraphState,
    dag: DAG,
    generator_id: str,
    prefixed: list[dict],
) -> None:
    """恢复的子图拓扑快照与节点合回 DAG/state。"""
    state.set_subgraph_nodes(generator_id, subgraph_topology(prefixed))
    dag.add_nodes(prefixed, parent_node_id=generator_id)


def fail_unrestorable_subgraph(
    state: GraphState,
    dag: DAG,
    generator_id: str,
    sub_ids: list[str],
) -> None:
    """子图无法重建（yaml 丢失/不可解析/不一致）时的收尾。

    子图节点已全部 passed 时不影响执行，跳过；否则必须让 graph 失败——
    不能按原始 gen→downstream 继续调度（残留 pending 子节点永不执行）。
    同时废弃旧子图状态，使续跑能重跑 generator 重新编排。failed/skipped
    虽是终态，续跑会把它们重置为 pending 重新执行，故同样按需失败。
    """
    nodes = state.snapshot().get("nodes", {})
    pending = [sid for sid in sub_ids if (nodes.get(sid, {}) or {}).get("status") != "passed"]
    if not pending:
        return
    logger.warning(
        "cannot rebuild subgraph for generator %s (subgraph yaml missing), marking graph failed",
        generator_id,
    )
    dag.remove_nodes(sub_ids, parent_node_id=generator_id)
    state.remove_nodes(sub_ids)
    state.set_subgraph_generated(generator_id, False)
    state.set_subgraph_node_ids(generator_id, [])
    state.set_subgraph_nodes(generator_id, [])
    state.set_node_status(generator_id, "failed")
    state.set_failure_reason(generator_id, "子图 yaml 缺失，无法重建子图 DAG")
    state.set_graph_status("failed")
