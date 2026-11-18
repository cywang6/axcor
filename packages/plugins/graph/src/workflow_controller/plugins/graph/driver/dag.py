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

"""DAG 构造 + Kahn 拓扑查环 + 就绪节点调度。

yaml schema 为 nodes: [{id, depends_on, ...}]；此处不校验 agents_dir /
executor 文件存在性，仅做结构校验 + 环检测。
"""

from __future__ import annotations


def prefix_subgraph_nodes(sub_config: dict, parent_node_id: str) -> list[dict]:
    """子图配置 nodes 转成带父节点前缀的节点列表。

    id / depends_on / acceptance.rollback_to 都带 `{parent}:` 前缀，供
    DAG.add_nodes 合并进父图。运行时编排和 DB 恢复（reload）重建拓扑都用它。
    """
    prefix = f"{parent_node_id}:"
    prefixed: list[dict] = []
    for sn in sub_config.get("nodes") or []:
        pn = dict(sn)
        pn["id"] = prefix + sn["id"]
        pn["depends_on"] = [prefix + d for d in (sn.get("depends_on") or [])]
        acc = pn.get("acceptance") or {}
        rt = acc.get("rollback_to", "")
        if rt:
            acc = dict(acc)
            acc["rollback_to"] = prefix + rt
            pn["acceptance"] = acc
        prefixed.append(pn)
    return prefixed


def subgraph_topology(prefixed_nodes: list[dict]) -> list[dict]:
    """从带前缀子图节点列表提取拓扑快照（id/depends_on/task_type/task_description）。

    供归档与恢复使用：``DAG.add_nodes`` 会把入口节点的 ``depends_on`` 改写为父
    generator，因此拓扑快照必须在 ``add_nodes`` 之前取，保留子图内部依赖
    （入口为 ``[]``）。
    """
    return [
        {
            "id": pn["id"],
            "depends_on": list(pn.get("depends_on") or []),
            "task_type": pn.get("task_type", "bash"),
            "task_description": pn.get("task_description", ""),
        }
        for pn in prefixed_nodes
    ]


class DAG:
    """节点依赖图。无环、无未知依赖、无重复 id。

    预建反向邻接表让 Kahn 拓扑排序降到 O(V+E)。重复 id 检测按 id 字段。
    """

    def __init__(self, nodes: list[dict]) -> None:
        if not nodes:
            raise ValueError("no nodes")

        ids = [n["id"] for n in nodes]
        seen: set[str] = set()
        dupes: set[str] = set()
        for i in ids:
            if i in seen:
                dupes.add(i)
            seen.add(i)
        if dupes:
            raise ValueError(f"duplicate node ids: {dupes}")

        self._nodes: dict[str, dict] = {n["id"]: n for n in nodes}
        self._deps: dict[str, set[str]] = {n["id"]: set(n.get("depends_on") or []) for n in nodes}
        self._dependents: dict[str, set[str]] = {nid: set() for nid in self._nodes}
        for nid, deps in self._deps.items():
            for d in deps:
                if d not in self._dependents:
                    raise ValueError(
                        f"node '{nid}' depends on unknown '{d}'",
                    )
                self._dependents[d].add(nid)

        self._validate()

    @property
    def nodes(self) -> list[dict]:
        return list(self._nodes.values())

    @property
    def edges(self) -> list[tuple[str, str]]:
        return [(d, nid) for nid, deps in self._deps.items() for d in deps]

    def get_ready(self, completed: set[str]) -> list[dict]:
        ready: list[dict] = []
        for nid, node in self._nodes.items():
            if nid in completed:
                continue
            if self._deps[nid] <= completed:
                ready.append(node)
        return ready

    def add_nodes(self, nodes: list[dict], parent_node_id: str) -> None:
        """运行时把子图节点合并进当前 DAG（子图是父图的一部分）。

        子图节点 id 已经带前缀（`gen-node:sub-node`），入口节点（depends_on
        为空）改为依赖 parent_node_id。同时把 parent_node_id 的下游主图节点
        （如 bad-node）的依赖重定向到子图出口节点——下游节点不感知子图存在，
        只是等它依赖的 node（现在是子图出口）完成就 run。

        先注册整批节点 id、再校验依赖：合法的子图不应因节点在 yaml 里的
        排列顺序（如 `b depends_on a` 但 `b` 写在 `a` 前）而失败。
        """
        for n in nodes:
            nid = n["id"]
            if nid in self._nodes:
                raise ValueError(f"duplicate node id: {nid}")

        new_ids = {n["id"] for n in nodes}
        known = set(self._nodes) | new_ids

        # 预计算每个新节点的依赖（入口节点改依赖 parent），整批校验后再落地，
        # 避免半途改坏 DAG。
        prepared: list[tuple[str, set[str]]] = []
        for n in nodes:
            nid = n["id"]
            deps = set(n.get("depends_on") or [])
            if not deps:
                deps = {parent_node_id}
            for d in deps:
                if d not in known:
                    raise ValueError(
                        f"node '{nid}' depends on unknown '{d}'",
                    )
            n["depends_on"] = list(deps)
            prepared.append((nid, deps))

        for n in nodes:
            self._nodes[n["id"]] = n
            self._dependents[n["id"]] = set()
        for nid, deps in prepared:
            self._deps[nid] = deps
            for d in deps:
                self._dependents[d].add(nid)

        # 子图出口节点 = 子图里没有被其他子图节点依赖的节点
        sub_ids = new_ids
        depended = set()
        for n in nodes:
            for d in n.get("depends_on") or []:
                if d in sub_ids:
                    depended.add(d)
        exit_ids = [n["id"] for n in nodes if n["id"] not in depended]
        # 重定向 parent_node_id 的下游主图节点（非子图节点）的依赖到子图出口
        for downstream in list(self._dependents.get(parent_node_id, set())):
            if downstream in sub_ids:
                continue
            self._deps[downstream].discard(parent_node_id)
            self._dependents[parent_node_id].discard(downstream)
            for eid in exit_ids:
                self._deps[downstream].add(eid)
                self._dependents[eid].add(downstream)
            self._nodes[downstream]["depends_on"] = list(self._deps[downstream])

    def remove_nodes(
        self, node_ids: list[str] | set[str], parent_node_id: str | None = None
    ) -> None:
        """从 DAG 移除一批节点（回滚 generator 时废弃旧子图节点）。

        被移除节点若有主图下游（父 generator 的 downstream 被 add_nodes 重定向
        到子图出口），恢复其对 parent_node_id 的依赖——generator 重新生成合并时
        会再次把下游重定向到新出口。
        """
        ids = set(node_ids)
        if parent_node_id is not None and parent_node_id in self._nodes:
            self._restore_downstream_deps(ids, parent_node_id)
        for nid in ids:
            if nid not in self._nodes:
                continue
            for d in self._deps.get(nid, set()):
                self._dependents.get(d, set()).discard(nid)
            for downstream in list(self._dependents.get(nid, set())):
                self._deps.get(downstream, set()).discard(nid)
            self._nodes.pop(nid, None)
            self._deps.pop(nid, None)
            self._dependents.pop(nid, None)

    def all_ids(self) -> set[str]:
        return set(self._nodes.keys())

    def descendants(self, node_id: str) -> set[str]:
        """node_id 的全部后裔（沿 depends_on 正向可达，不含自身）。"""
        seen: set[str] = set()
        stack = list(self._dependents.get(node_id, set()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self._dependents.get(cur, set()))
        return seen

    def ancestors(self, node_id: str) -> set[str]:
        """node_id 的全部祖先（沿 depends_on 反向可达，不含自身）。"""
        seen: set[str] = set()
        stack = list(self._deps.get(node_id, set()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self._deps.get(cur, set()))
        return seen

    def get_node(self, node_id: str) -> dict:
        return self._nodes[node_id]

    def _restore_downstream_deps(self, ids: set[str], parent_node_id: str) -> None:
        """被移除节点的主图下游：依赖改回父 generator 节点（新子图生成后再重定向）。"""
        for nid in list(ids):
            for downstream in list(self._dependents.get(nid, set())):
                if downstream in ids:
                    continue
                self._deps[downstream].discard(nid)
                self._deps[downstream].add(parent_node_id)
                self._dependents[parent_node_id].add(downstream)
                self._nodes[downstream]["depends_on"] = list(self._deps[downstream])

    def _validate(self) -> None:
        for nid, deps in self._deps.items():
            for d in deps:
                if d not in self._nodes:
                    raise ValueError(
                        f"node '{nid}' depends on unknown '{d}'",
                    )
        in_degree: dict[str, int] = {nid: len(deps) for nid, deps in self._deps.items()}
        queue: list[str] = [nid for nid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            cur = queue.pop()
            visited += 1
            for nxt in self._dependents[cur]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(self._nodes):
            raise ValueError("workflow contains a cycle")
