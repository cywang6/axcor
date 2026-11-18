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

"""run 平面内部结构护栏（C3c）：组件簇私有化 + coord._ 耦合棘轮。

RunCoordinator 的按变更轴拆分（D46）是可读性分解而非架构解耦：四个组件类
以 `_` 前缀私有命名，仅限 run_coordinator.py 模块内使用；同模块内允许访问
协调器私有成员，但耦合总量只许降不许升（R4 命令队列落地时大幅下调，若日后
采纳显式注入则清零）。RunHost 例外——公共命名的注入式服务面，不在护栏内。
导出不设组件类（实现住 core/run_export.py，协调器惰性装配）。
"""

from __future__ import annotations

import ast
import re

from tests.architecture.layout import kernel_package_root, source_roots

SRC = kernel_package_root()
COORDINATOR = SRC / "core" / "run_coordinator.py"

_PRIVATE_COMPONENTS = (
    "_GroupControlEngine",
    "_AdmissionGate",
    "_TerminalSequencer",
    "_RecoveryService",
)

# 组件对协调器私有成员（coord._*）的耦合预算：棘轮只降不升，下降时同步调小
_COORD_PRIVATE_ACCESS_BUDGET = 41


def _coord_private_accesses(source: str) -> int:
    """代码访问计数（`coord._x` 别名与 `self._coord._x` 链），不含文档/注释文本。"""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        value = node.value
        is_coord_alias = isinstance(value, ast.Name) and value.id == "coord"
        is_self_coord_chain = (
            isinstance(value, ast.Attribute)
            and value.attr == "_coord"
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        )
        if is_coord_alias or is_self_coord_chain:
            count += 1
    return count


def test_component_classes_are_module_private() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")
    for name in _PRIVATE_COMPONENTS:
        assert re.search(rf"^class {name}\b", source, re.M), (
            f"{name} 应以 `_` 前缀私有命名定义于 run_coordinator.py"
        )


def test_private_components_have_no_external_consumers() -> None:
    offenders: list[str] = []
    for source_root in source_roots():
        for path in (source_root / "workflow_controller").rglob("*.py"):
            if path == COORDINATOR:
                continue
            for name in _PRIVATE_COMPONENTS:
                if name in path.read_text(encoding="utf-8"):
                    offenders.append(f"{path}:{name}")
    assert not offenders, (
        f"私有组件被模块外生产代码引用：{offenders}——"
        "外部消费面是 RunCoordinator 公共方法，不是其内部实现类"
    )


def test_coord_private_access_ratchet() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")
    count = _coord_private_accesses(source)
    assert count <= _COORD_PRIVATE_ACCESS_BUDGET, (
        f"组件对 coord._ 的耦合计数上升到 {count}（预算 {_COORD_PRIVATE_ACCESS_BUDGET}）："
        "新依赖请经构造注入或走协调器公共面；确需上调须评审并同步调大预算"
    )
