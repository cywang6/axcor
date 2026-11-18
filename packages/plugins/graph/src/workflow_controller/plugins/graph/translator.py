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

"""工作流 yaml → 程序 IR 翻译器（插件私有，主结构不消费）。

集中承载：yaml 解析单点、xlsx 规范格式归一、变量合并渲染、DAG 构建与
回滚目标校验、work_dir 回退链、子图 yaml 读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from workflow_controller.plugins.graph.driver.dag import DAG, prefix_subgraph_nodes
from workflow_controller.plugins.graph.template import render

# ── yaml 解析单点 ────────────────────────────────────────────────────


def parse_yaml_text(text: str) -> Any:
    """解析 yaml 文本（插件内唯一的 yaml 解析入口）。"""
    return yaml.safe_load(text)


def parse_yaml_file(path: Path) -> Any:
    """读取并解析 yaml 文件。"""
    return parse_yaml_text(path.read_text(encoding="utf-8"))


# ── xlsx 规范格式 → 引擎格式 ─────────────────────────────────────────


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """顶层字段映射 + 逐节点归一，旧格式字段原样透传。

    - op_name → variables.OP（已有 OP 时外部传入优先，不覆盖）
    - max_rollback → rollback_budget（已有 rollback_budget 优先）
    - agents_dir 缺失时兜底 ../agents（workflows/ 上一级）
    - nodes 逐个过 normalize_node
    """
    cfg = dict(config)
    op_name = cfg.get("op_name")
    if op_name is not None:
        variables = dict(cfg.get("variables") or {})
        if "OP" not in variables:
            if isinstance(op_name, str) and op_name.startswith("{{"):
                inner = op_name.strip("{} ").strip()
                variables["OP"] = f"{{{{argv.{inner}}}}}"
            else:
                variables["OP"] = op_name
        cfg["variables"] = variables
    if "rollback_budget" not in cfg and "max_rollback" in cfg:
        cfg["rollback_budget"] = cfg["max_rollback"]
    cfg.setdefault("agents_dir", "../agents")
    cfg["nodes"] = [normalize_node(n) for n in (cfg.get("nodes") or [])]
    return cfg


def _normalize_on_exhaust(value: Any) -> Any:
    """历史词表值迁移：exit/fail → stop（翻译期仍拒绝真正词表外值）。"""
    return "stop" if value in ("exit", "fail") else value


def _normalize_task_type(n: dict[str, Any]) -> None:
    """task_type 族归一：normal→agent；subgraph→generator（file 挂载）或 agent。"""
    tt = n.get("task_type", "agent")
    if tt in ("normal", "subgraph"):
        # 保留原始类型供前端展示（节点副标题显示 yaml 里的 task_type），
        # 执行用内部类型。
        n["_orig_task_type"] = tt
    if tt == "normal":
        n["task_type"] = "agent"
    elif tt == "subgraph":
        file_path = n.get("file", "")
        if file_path:
            n["task_type"] = "generator"
            n["_xlsx_subgraph"] = True
            n["subgraph"] = {"output": file_path}
        else:
            n["task_type"] = "agent"


def _normalize_acceptance_field(n: dict[str, Any]) -> None:
    """acceptance 族归一：清单写法转 dict；engine dict 同步 verifier 并归一 on_exhaust。"""
    if isinstance(n.get("acceptance"), list) or (
        n.get("acceptance") is None and "max_retries" in n
    ):
        n["acceptance"] = _normalize_acceptance(n)
        return
    acc = n.get("acceptance")
    if isinstance(acc, dict):
        acc = dict(acc)
        # engine 格式子 yaml 的 verifier 存在 acceptance 内：同步到
        # 顶层供展示，exit/fail→stop 归一
        if acc.get("verifier") and not n.get("verifier"):
            n["verifier"] = acc.get("verifier")
        if "on_exhaust" in acc:
            acc["on_exhaust"] = _normalize_on_exhaust(acc["on_exhaust"])
        elif "on_exhaust" in n:
            acc["on_exhaust"] = _normalize_on_exhaust(n["on_exhaust"])
        n["acceptance"] = acc


def _normalize_descriptions(n: dict[str, Any]) -> None:
    """描述族归一：title/approach/out_of_scope → task_description；goal → task_goal。"""
    if "task_description" not in n and "title" in n:
        parts = [n.get("title", "")]
        if n.get("approach"):
            parts.append("达成路径：" + "；".join(n["approach"]))
        if n.get("out_of_scope"):
            parts.append("禁止事项：" + "；".join(n["out_of_scope"]))
        n["task_description"] = "\n".join(parts)
    if "task_goal" not in n and "goal" in n:
        g = n["goal"]
        n["task_goal"] = "\n".join(g) if isinstance(g, list) else str(g)


def normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """xlsx 节点 → 引擎节点，旧格式字段原样保留。

    - task_type: normal→agent；subgraph→generator（subgraph.output=file，
      带 _xlsx_subgraph 标记，跳过 LLM 生成直接挂载子 yaml）
    - acceptance 为 list 时：顶层有 verifier → agent 验收（清单拼
      verifier_prompt）；无 → script 验收（清单 && 拼接）
    - max_retries/on_exhaust/rollback_to 从顶层搬回 acceptance dict
    - on_exhaust: exit/fail → stop
    - title/approach/out_of_scope → task_description；goal → task_goal
    - acceptance dict 内的 verifier 同步到顶层，供 DAG/前端展示
    """
    n = dict(node)
    _normalize_task_type(n)
    _normalize_acceptance_field(n)
    _normalize_descriptions(n)
    return n


def _normalize_acceptance(node: dict[str, Any]) -> dict[str, Any]:
    """xlsx acceptance 清单 + 顶层控制字段 → 引擎 acceptance dict。"""
    acc_list = node.get("acceptance") or []
    verifier = node.get("verifier", "")
    max_retries = node.get("max_retries", 2)
    on_exhaust = _normalize_on_exhaust(node.get("on_exhaust", "stop"))
    rollback_to = node.get("rollback_to", "")

    if verifier:
        return {
            "enabled": True,
            "type": "agent",
            "verifier": verifier,
            "verifier_prompt": "\n".join(acc_list),
            "max_retries": max_retries,
            "on_exhaust": on_exhaust,
            "rollback_to": rollback_to,
        }
    script = " && ".join(acc_list) if acc_list else "true"
    return {
        "enabled": True,
        "type": "script",
        "script": script,
        "max_retries": max_retries,
        "on_exhaust": on_exhaust,
        "rollback_to": rollback_to,
    }


# ── 程序 IR 与主 yaml 翻译 ───────────────────────────────────────────


@dataclass
class GraphProgram:
    """程序 IR：主 yaml 经解析、归一、校验后的可执行工作流描述。"""

    workflow: str
    description: str
    config: dict[str, Any]  # 归一后的完整配置
    dag: DAG
    variables: dict[str, str]  # 合并并渲染后的变量表
    max_parallel: int
    rollback_budget: int
    agents_dir: Path | None  # 相对 yaml 目录已转绝对，目录不存在时为 None


def translate_program(
    yaml_path: Path,
    variables: dict[str, str] | None = None,
    agents_dir_base: Path | None = None,
) -> GraphProgram:
    """主 yaml 全量翻译：解析 → 格式归一 → 变量渲染 → DAG 构建与回滚校验。

    agents_dir_base 覆盖 agents_dir 的相对解析基准（缺省 yaml 所在目录）：
    执行冻结副本时传原始 yaml 目录，相对 agents 资源才解析得到。
    """
    config = normalize_config(parse_yaml_file(yaml_path) or {})
    dag = DAG(config["nodes"])
    _validate_rollback_targets(dag)
    _validate_on_exhaust(dag)
    return GraphProgram(
        workflow=config.get("workflow", "default"),
        description=config.get("description", ""),
        config=config,
        dag=dag,
        variables=_merge_variables(config, variables),
        max_parallel=config.get("max_parallel", 2),
        rollback_budget=int(config.get("rollback_budget", 2) or 2),
        agents_dir=_resolve_agents_dir(
            config.get("agents_dir"),
            agents_dir_base or yaml_path.parent,
        ),
    )


def _merge_variables(
    config: dict[str, Any],
    external_variables: dict[str, str] | None,
) -> dict[str, str]:
    """yaml 变量与外部变量合并（外部优先），逐个渲染嵌套引用。"""
    merged = dict(config.get("variables") or {})
    merged.update(external_variables or {})
    for key in list(merged):
        merged[key] = render(str(merged[key]), merged)
    return merged


def _resolve_agents_dir(
    agents_dir_str: Any,
    base_dir: Path,
) -> Path | None:
    """解析 agents_dir：相对路径以 base_dir 为基准，不存在则为 None。"""
    if not agents_dir_str:
        return None
    path = Path(agents_dir_str)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path if path.is_dir() else None


def _validate_rollback_targets(dag: DAG) -> None:
    """rollback_to 必须存在且是失败节点的严格祖先，否则合并后回滚会错乱。"""
    for node in dag.nodes:
        acc = node.get("acceptance") or {}
        target = acc.get("rollback_to", "") or ""
        if acc.get("on_exhaust") == "rollback" and target:
            if target not in dag.all_ids():
                raise ValueError(f"node '{node['id']}' rollback_to '{target}' does not exist")
            if target not in dag.ancestors(node["id"]):
                raise ValueError(f"node '{node['id']}' rollback_to '{target}' must be an ancestor")


ON_EXHAUST_VOCAB = frozenset({"continue", "rollback", "stop"})


def _validate_on_exhaust(dag: DAG) -> None:
    """on_exhaust 词表外值在翻译期拒绝（与导出 schema 枚举对齐，失败前移）。

    acceptance 缺席的「瘦节点」（仅顶层 on_exhaust、无 max_retries）不会被
    normalize_node 搬进 acceptance，须补查顶层；历史值 exit/fail 经归一映射
    为 stop 后再判。
    """
    for node in dag.nodes:
        on_exhaust = (node.get("acceptance") or {}).get("on_exhaust")
        if on_exhaust is None:
            on_exhaust = _normalize_on_exhaust(node.get("on_exhaust"))
        if on_exhaust is not None and on_exhaust not in ON_EXHAUST_VOCAB:
            raise ValueError(
                f"node '{node.get('id')}' on_exhaust 值 {on_exhaust!r} 不在词表 "
                f"{sorted(ON_EXHAUST_VOCAB)}"
            )


def resolve_work_dir(
    yaml_config: dict[str, Any],
    external_variables: dict[str, str] | None,
    external_work_dir: str | None,
    yaml_path: Path,
    project_dir: str | Path | None,
) -> Path:
    """work_dir 回退链：yaml work_dir（变量渲染）→ 外部 work_dir → 项目目录。

    相对的 yaml work_dir 以外部 work_dir 优先、yaml 所在目录兜底为基准
    （对齐「相对 launch CWD（项目根目录）」语义）。
    """
    yaml_work_dir = yaml_config.get("work_dir")
    if yaml_work_dir:
        merged = dict(yaml_config.get("variables") or {})
        merged.update(external_variables or {})
        wd = Path(render(yaml_work_dir, merged))
        if not wd.is_absolute():
            base = Path(external_work_dir) if external_work_dir else yaml_path.parent
            wd = base / wd
        return wd.resolve()
    if external_work_dir:
        return Path(external_work_dir).expanduser().resolve()
    return Path(project_dir or ".").expanduser().resolve()


# ── 子图 yaml 读取 ───────────────────────────────────────────────────


def load_subgraph_config(path: Path) -> dict[str, Any]:
    """子图 yaml 读取并归一（新旧格式通吃）。"""
    return normalize_config(parse_yaml_file(path) or {})


def load_prefixed_subgraph(path: Path, parent_node_id: str) -> list[dict]:
    """子图 yaml 读取归一，返回带父节点前缀的节点列表。"""
    return prefix_subgraph_nodes(load_subgraph_config(path), parent_node_id)
