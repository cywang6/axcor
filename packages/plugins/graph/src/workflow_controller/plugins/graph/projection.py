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

"""graph 驱动投影：状态快照 + DAG 的只读组装（schema_version=2）。

消费面两条：RunsAPI /state 的驱动投影透传与旧 /api/graph/{id} 详情——同一
形状、同一版本号；子图 rollup、归档代拓扑与回滚边是形状的一部分，改动即
升 schema_version。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

PROJECTION_SCHEMA_VERSION = 2


def _sub_parent_index(state_snapshot: Mapping[str, Any]) -> dict[str, str]:
    """子节点 → 父 generator 的归属索引（当前代 nodes + 归档代 retired）。"""
    sub_parent: dict[str, str] = {}
    for gid, ginfo in state_snapshot["nodes"].items():
        for sid in ginfo.get("subgraph_node_ids") or []:
            sub_parent[sid] = gid
    for rinfo in (state_snapshot.get("retired_instances") or {}).values():
        for sid in rinfo.get("subgraph_node_ids") or []:
            sub_parent.setdefault(sid, rinfo.get("id") or "")
    return sub_parent


def _strip_prefix(nid: str, sub_parent: Mapping[str, str]) -> str:
    """子图节点 id 去掉父 generator 前缀；主图节点原样。"""
    parent = sub_parent.get(nid)
    return nid[slice(len(parent) + 1, None)] if parent is not None else nid


def _sub_status(statuses: list[str]) -> str:
    """子图整体状态：按当前代成员状态聚合。"""
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "stopped" in statuses:
        return "paused"
    if statuses and all(s in ("passed", "skipped") for s in statuses):
        return "completed"
    return "pending"


def _main_node_entry(
    nid: str,
    info: Mapping[str, Any],
    dag_node: Mapping[str, Any],
    sub_parent: Mapping[str, str],
) -> dict[str, Any]:
    """主图节点的投影行。"""
    return {
        "id": nid,
        "depends_on": [sub_parent.get(d, d) for d in (dag_node.get("depends_on") or [])],
        # task_type 保持内部类型（前端子图按钮等逻辑依赖 generator）；
        # display_type 是 xlsx 规范的原始类型（normal/subgraph），节点副标题展示用。
        "task_type": dag_node.get("task_type", "bash"),
        "display_type": dag_node.get("_orig_task_type") or dag_node.get("task_type", "bash"),
        "task_description": dag_node.get("task_description", "")[:80],
        "status": info.get("status", "pending"),
        "attempts": info.get("attempts", 0),
        "gen": info.get("gen", 0),
        "workflow_controller_session_id": info.get("workflow_controller_session_id"),
        "started_at": info.get("started_at"),
        "finished_at": info.get("finished_at"),
        "failure_reason": info.get("failure_reason"),
        "output": info.get("output"),
        "subgraph_generated": info.get("subgraph_generated", False),
        "subgraph_node_ids": info.get("subgraph_node_ids") or None,
    }


def _subgraph_topology(
    nid: str,
    info: Mapping[str, Any],
    state_snapshot: Mapping[str, Any],
) -> tuple[dict[tuple[int, str], dict[str, Any]], set[str]]:
    """generator 的子图拓扑快照（(gen, id) 键）与归档代子节点集。

    拓扑按 (gen, id) 保存：同一子节点 ID 在不同 generation 可能改了
    depends_on，扁平化按 id 取会让归档代吃到当前代拓扑，历史连线失真。
    """
    topo: dict[tuple[int, str], dict[str, Any]] = {}
    archived_ids: set[str] = set()
    for rinfo in (state_snapshot.get("retired_instances") or {}).values():
        if (rinfo.get("id") or "") == nid:
            rgen = rinfo.get("gen", 0)
            for sn in rinfo.get("subgraph_nodes") or []:
                topo.setdefault((rgen, sn["id"]), sn)
            archived_ids.update(rinfo.get("subgraph_node_ids") or [])
    for sn in info.get("subgraph_nodes") or []:
        topo[(info.get("gen", 0), sn["id"])] = sn
    return topo, archived_ids


def _sub_node_entry(
    rid: str,
    sinfo: dict[str, Any],
    topo: Mapping[tuple[int, str], dict[str, Any]],
    dag_nodes: Mapping[str, Any],
    sub_parent: Mapping[str, str],
) -> dict[str, Any]:
    """子图节点的投影行。"""
    sn_top = topo.get((sinfo.get("gen", 0), rid), {})
    sdag = dag_nodes.get(rid, {})
    # 拓扑快照里 depends_on 可能为空列表（入口节点），不能用 `or`
    # 回退到当前 DAG——否则归档代的入口节点会吃到当前代的依赖。
    deps_src = sn_top["depends_on"] if "depends_on" in sn_top else sdag.get("depends_on") or []
    deps = [_strip_prefix(d, sub_parent) for d in deps_src if d in sub_parent]
    return {
        "id": _strip_prefix(rid, sub_parent),
        "depends_on": deps,
        "task_type": sn_top.get("task_type") or sdag.get("task_type", "bash"),
        "task_description": (sn_top.get("task_description") or sdag.get("task_description", ""))[
            :80
        ],
        "status": sinfo.get("status", "pending"),
        "attempts": sinfo.get("attempts", 0),
        "gen": sinfo.get("gen", 0),
        "workflow_controller_session_id": sinfo.get("workflow_controller_session_id"),
        "started_at": sinfo.get("started_at"),
        "finished_at": sinfo.get("finished_at"),
        "failure_reason": sinfo.get("failure_reason"),
        "output": sinfo.get("output"),
    }


def _build_subgraph(
    nid: str,
    sub_ids: list[str],
    archived_ids: set[str],
    topo: Mapping[tuple[int, str], dict[str, Any]],
    state_snapshot: Mapping[str, Any],
    dag_nodes: Mapping[str, Any],
    sub_parent: Mapping[str, str],
) -> dict[str, Any]:
    """generator 的子图投影：当前代 + 各归档代成员与边。"""
    sub_nodes = []
    for sid in sub_ids:
        sub_nodes.append(
            _sub_node_entry(sid, state_snapshot["nodes"].get(sid, {}), topo, dag_nodes, sub_parent)
        )
    for vk, rinfo in (state_snapshot.get("retired_instances") or {}).items():
        rid = rinfo.get("id") or vk.split("#")[0]
        if rid in archived_ids:
            sub_nodes.append(_sub_node_entry(rid, rinfo, topo, dag_nodes, sub_parent))
    sub_edges = [{"from": d, "to": sn["id"]} for sn in sub_nodes for d in sn["depends_on"]]
    return {
        "parent_node": nid,
        "nodes": sub_nodes,
        "edges": sub_edges,
        "status": _sub_status(
            [
                sn["status"]
                for sn in sub_nodes
                if sn["gen"] == (max((x["gen"] for x in sub_nodes), default=0))
            ]
        ),
    }


def _retired_entries(
    state_snapshot: Mapping[str, Any],
    dag_nodes: Mapping[str, Any],
    sub_parent: Mapping[str, str],
) -> list[dict[str, Any]]:
    """归档代主图节点的投影行（子图节点不单列）。"""
    retired_out = []
    for vk, info in (state_snapshot.get("retired_instances") or {}).items():
        rid = info.get("id") or vk.split("#")[0]
        if rid in sub_parent:
            continue
        dag_node = dag_nodes.get(rid, {})
        retired_out.append(
            {
                "id": rid,
                "depends_on": [sub_parent.get(d, d) for d in (dag_node.get("depends_on") or [])],
                "task_type": dag_node.get("task_type", "bash"),
                "status": info.get("status", "pending"),
                "attempts": info.get("attempts", 0),
                "gen": info.get("gen", 0),
                "subgraph_generated": info.get("subgraph_generated", False),
                "subgraph_node_ids": info.get("subgraph_node_ids") or None,
            }
        )
    return retired_out


def _main_node_rows(
    state_snapshot: Mapping[str, Any],
    dag_nodes: Mapping[str, Any],
    sub_parent: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """主图节点行与 generator 的子图投影行。"""
    nodes_out = []
    subgraphs_out = []
    for nid, info in state_snapshot["nodes"].items():
        if nid in sub_parent:
            continue
        nodes_out.append(_main_node_entry(nid, info, dag_nodes.get(nid, {}), sub_parent))
        # generator 节点的子图：含当前代（nodes）+ 各归档代（retired），
        # 拓扑从 subgraph_nodes 快照还原，strip 前缀输出。
        sub_ids = info.get("subgraph_node_ids") or []
        topo, archived_ids = _subgraph_topology(nid, info, state_snapshot)
        if sub_ids or archived_ids:
            subgraphs_out.append(
                _build_subgraph(
                    nid, sub_ids, archived_ids, topo, state_snapshot, dag_nodes, sub_parent
                )
            )
    return nodes_out, subgraphs_out


def build_projection(
    state_snapshot: Mapping[str, Any],
    dag: Any,
    *,
    workflow: str,
    description: str,
    work_dir: str,
    model: str | None,
    provider_index: int | None,
) -> dict[str, Any]:
    """状态快照 → 投影结构（纯函数；dag 只读拓扑与节点静态字段）。"""
    graph_id = state_snapshot.get("graph_id")
    # 子图节点合并进父图 DAG/state，id 带 ':' 前缀（`gen-node:sub-node`）。
    # 子图归属显式记录在 generator 的 subgraph_node_ids（当前代 + 归档代），
    # 不能靠 node id 里的 ':' 推断——主图节点 id 允许含 ':'（如 prep:db）。
    dag_nodes = {n["id"]: n for n in dag.nodes}
    sub_parent = _sub_parent_index(state_snapshot)
    nodes_out, subgraphs_out = _main_node_rows(state_snapshot, dag_nodes, sub_parent)
    retired_out = _retired_entries(state_snapshot, dag_nodes, sub_parent)
    # rollback_edges：子图回滚的边是 gen-sub（父节点）→ gen-sub，主图 DAG 画红线；
    # 子图内部回滚边（子图节点）从 subgraphs 字段输出。
    main_rollback_edges = [
        re_
        for re_ in (state_snapshot.get("rollback_edges") or [])
        if not (re_.get("from", "") in sub_parent and re_.get("to", "") in sub_parent)
    ]
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "graph_id": graph_id,
        "workflow": workflow,
        "description": description,
        "status": state_snapshot.get("status"),
        "nodes": nodes_out,
        "subgraphs": subgraphs_out,
        "retired_instances": retired_out,
        "edges": [
            {"from": sub_parent.get(f, f), "to": t} for f, t in dag.edges if t not in sub_parent
        ],
        "rollback_edges": main_rollback_edges,
        "work_dir": work_dir,
        "model": model,
        "provider_index": provider_index,
    }
