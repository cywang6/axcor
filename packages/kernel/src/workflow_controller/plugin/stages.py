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

"""阶段契约的共享求值面：声明校验、artifact 规范化、门禁求值与阶段派生。

stages 声明在项目 workflow-controller.yaml、随 SessionConfig.stages 进 session——是项目配置
契约而非插件私有协议。orchestrator 的边界 review 文案、reviewer 的产物真值表、
进度 API 与 staged_pipeline 门禁在此同源求值；核心与插件 import 本模块都合法。
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_VALID_GATES = frozenset({"exists", "non_placeholder", "command"})


def declared_stages(stages_raw: list[Any]) -> list[dict[str, Any]]:
    """过滤出有效阶段声明（dict 且 name/artifacts 非空）。"""
    return [s for s in stages_raw if isinstance(s, dict) and s.get("name") and s.get("artifacts")]


# ── artifact 规范化与展示 ──────────────────────────────────────────────


def _artifact_gate(entry: dict[str, Any]) -> tuple[str, str] | None:
    """校验 entry 的 gate/command 组合；非法或 command 门缺失时返回 None。"""
    gate = str(entry.get("gate", "")).strip()
    if gate not in _VALID_GATES:
        return None
    command = str(entry.get("command", "")).strip()
    if gate == "command" and not command:
        return None
    return gate, command


def _expand_artifact(artifact: Any, gate: str, command: str) -> list[dict[str, str]]:
    """把 entry 的 artifact 字段展开为统一 entry（字符串单条 / 列表逐条）。"""
    if isinstance(artifact, str) and artifact.strip():
        return [{"artifact": artifact.strip().rstrip("/"), "gate": gate, "command": command}]
    if not isinstance(artifact, list):
        return []
    expanded: list[dict[str, str]] = []
    for item in artifact:
        if isinstance(item, str) and item.strip():
            expanded.append(
                {"artifact": item.strip().rstrip("/"), "gate": gate, "command": command}
            )
    return expanded


def normalize_artifacts(stage: dict[str, Any]) -> list[dict[str, str]]:
    """从 stage['artifacts'] 展开为统一格式的 artifact entry 列表。

    每条 entry 格式: ``{"artifact": str, "gate": str, "command": str}``

    支持缩略写法：entry 内 ``artifact`` 可以是字符串列表，展开为多条记录
    共享同一 ``gate``/``command``。校验规则：
    - ``artifact`` 必填（字符串非空 或 列表非空）
    - ``gate`` 必填且必须是 ``exists``/``non_placeholder``/``command`` 之一
    - ``gate == "command"`` 时 ``command`` 必填
    - 不满足的 entry 静默跳过
    """
    raw = stage.get("artifacts")
    if not isinstance(raw, list):
        return []

    result: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        gate_cmd = _artifact_gate(entry)
        if gate_cmd is None:
            continue
        gate, command = gate_cmd
        result.extend(_expand_artifact(entry.get("artifact"), gate, command))
    return result


def artifacts_display(stage: dict[str, Any]) -> str:
    """反馈文案用的 artifact 路径拼接展示。"""
    entries = normalize_artifacts(stage)
    if not entries:
        return "?"
    paths = [e["artifact"] for e in entries]
    if len(paths) == 1:
        return paths[0]
    if len(paths) <= 3:
        return ", ".join(paths)
    return f"{', '.join(paths[:2])} 等 {len(paths)} 个产物"


# ── 门禁求值 ───────────────────────────────────────────────────────────

# 占位信号：文件 body 是这些 token（大小写不敏感）或体积过小 → 视为占位空壳。
_STUB_TOKENS = ("placeholder", "todo", "tbd", "占位", "stub", "fixme", "wip")
_MIN_BYTES = 32
_COMMAND_TIMEOUT = 120


def _is_placeholder_file(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return True
    if size < _MIN_BYTES:
        return True
    try:
        body = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    except OSError:
        return True
    # 整段（去空白）就是 stub token
    return body in _STUB_TOKENS


def gate_exists(cwd: Path, artifact: str) -> bool:
    return (cwd / artifact.rstrip("/")).exists()


def gate_non_placeholder(cwd: Path, artifact: str) -> bool:
    p = cwd / artifact.rstrip("/")
    if not p.exists():
        return False
    if p.is_dir():
        return any(f.is_file() and f.stat().st_size > 0 for f in p.rglob("*"))
    return not _is_placeholder_file(p)


def gate_command(cwd: Path, command: str) -> bool:
    # command 来自项目作者写的 workflow-controller.yaml stages；固定 bash -c 保留 YAML 命令语义。
    if not command:
        return False
    try:
        r = subprocess.run(
            ["/bin/bash", "-c", command],
            shell=False,
            cwd=cwd,
            timeout=_COMMAND_TIMEOUT,
            capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def evaluate_gate(cwd: Path, stage: dict[str, Any]) -> bool:
    """stage 全部 artifact 通过各自门禁才返回 True；无有效 artifact 则 False。"""
    entries = normalize_artifacts(stage)
    if not entries:
        return False
    for entry in entries:
        artifact = entry["artifact"]
        mode = entry["gate"]
        if mode == "exists":
            if not gate_exists(cwd, artifact):
                return False
        elif mode == "command":
            if not gate_command(cwd, entry["command"]):
                return False
        else:
            if not gate_non_placeholder(cwd, artifact):
                return False
    return True


# ── 阶段派生与跳步检测（无状态、确定性，每次从磁盘现推） ────────────────


def completed_prefix(stages: list[dict[str, Any]], cwd: Path) -> int:
    """从头开始连续满足门禁的阶段数（遇到第一个未满足即停）。"""
    n = 0
    for stage in stages:
        if evaluate_gate(cwd, stage):
            n += 1
        else:
            break
    return n


def _any_artifact_exists(stage: dict[str, Any], cwd: Path) -> bool:
    """检查 stage 中任一 artifact 在磁盘存在（非门禁、仅存在性）。"""
    return any((cwd / entry["artifact"]).exists() for entry in normalize_artifacts(stage))


def detect_jump(
    stages: list[dict[str, Any]],
    cwd: Path,
    *,
    prefix: int | None = None,
) -> tuple[str, str] | None:
    """跳步检测：current 阶段门禁未过，但更后面阶段的产物已出现 → (current, jumped)。

    全满足或无跳步 → None。用 artifact「存在」（非门禁通过）判定跳步——后置产物
    提前落盘本身就是顺序违规信号。prefix 复用调用方已算好的 completed_prefix
    （command 门求值要起 shell，避免逐分支重复求值）。
    """
    n = prefix if prefix is not None else completed_prefix(stages, cwd)
    if n >= len(stages):
        return None
    current = stages[n]
    for later in stages[slice(n + 1, None)]:
        if _any_artifact_exists(later, cwd):
            return (str(current["name"]), str(later["name"]))
    return None
