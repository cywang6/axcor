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

from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, order=True)
class HygieneViolation:
    rule: str
    source: str
    line: int
    detail: str

    def format(self) -> str:
        return f"{self.source}:{self.line}: {self.rule}: {self.detail}"


@dataclass(frozen=True)
class HygieneRule:
    name: str
    description: str


RULES = (
    HygieneRule("no-module-attribute-assignment", "use monkeypatch.setattr"),
    HygieneRule("no-os-environ-mutation", "use monkeypatch.setenv/delenv"),
)


def _attribute_root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _assignment_targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _is_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _is_os_environ(
    node: ast.AST,
    os_aliases: set[str],
    environ_aliases: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in environ_aliases
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in os_aliases
        and node.attr == "environ"
    )


def _plain_import_aliases(
    node: ast.Import, workflow_controller_imports: set[str], os_aliases: set[str]
) -> None:
    """`import x` 语句的 workflow_controller 名与 os 别名归一。"""
    for alias in node.names:
        if _is_package(alias.name, "workflow_controller"):
            workflow_controller_imports.add(alias.asname or alias.name.split(".")[0])
        elif alias.name == "os":
            os_aliases.add(alias.asname or "os")


def _workflow_controller_from_aliases(node: ast.ImportFrom, imports: set[str]) -> None:
    """`from workflow_controller... import x` 语句的本地名（跳过星号导入）。"""
    for alias in node.names:
        if alias.name != "*":
            imports.add(alias.asname or alias.name)


def _os_from_aliases(
    node: ast.ImportFrom, environ_aliases: set[str], os_mutator_aliases: set[str]
) -> None:
    """`from os import x` 语句的 environ 与 putenv/unsetenv 本地名。"""
    for alias in node.names:
        local_name = alias.asname or alias.name
        if alias.name == "environ":
            environ_aliases.add(local_name)
        elif alias.name in {"putenv", "unsetenv"}:
            os_mutator_aliases.add(local_name)


def _collect_import_aliases(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """本地名集合：workflow_controller 名、os 别名、environ 别名与 os 变更函数别名。"""
    workflow_controller_imports: set[str] = set()
    os_aliases: set[str] = set()
    environ_aliases: set[str] = set()
    os_mutator_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _plain_import_aliases(node, workflow_controller_imports, os_aliases)
        elif isinstance(node, ast.ImportFrom):
            if _is_package(node.module or "", "workflow_controller"):
                _workflow_controller_from_aliases(node, workflow_controller_imports)
            elif node.module == "os":
                _os_from_aliases(node, environ_aliases, os_mutator_aliases)
    return workflow_controller_imports, os_aliases, environ_aliases, os_mutator_aliases


def _assignment_violations(
    node: ast.AST,
    filename: str,
    workflow_controller_imports: set[str],
    os_aliases: set[str],
    environ_aliases: set[str],
) -> list[HygieneViolation]:
    """赋值目标上的两类违规：模块属性赋值与 os.environ 项写入。"""
    violations: list[HygieneViolation] = []
    for target in _assignment_targets(node):
        if (
            isinstance(target, ast.Attribute)
            and _attribute_root(target) in workflow_controller_imports
        ):
            violations.append(
                HygieneViolation(
                    "no-module-attribute-assignment",
                    filename,
                    node.lineno,
                    ast.unparse(target),
                )
            )
        mutates_environ_item = isinstance(target, ast.Subscript) and _is_os_environ(
            target.value, os_aliases, environ_aliases
        )
        mutates_environ_in_place = isinstance(node, ast.AugAssign) and _is_os_environ(
            target, os_aliases, environ_aliases
        )
        if mutates_environ_item or mutates_environ_in_place:
            violations.append(
                HygieneViolation(
                    "no-os-environ-mutation", filename, node.lineno, ast.unparse(target)
                )
            )
    return violations


def _call_violations(
    node: ast.Call,
    filename: str,
    os_aliases: set[str],
    environ_aliases: set[str],
    os_mutator_aliases: set[str],
) -> list[HygieneViolation]:
    """调用形态的 os.environ 变更：environ 方法与 os.putenv/unsetenv。"""
    violations: list[HygieneViolation] = []
    call_on_attribute = isinstance(node.func, ast.Attribute)
    if (
        call_on_attribute
        and _is_os_environ(node.func.value, os_aliases, environ_aliases)
        and node.func.attr in {"clear", "pop", "popitem", "setdefault", "update"}
    ):
        violations.append(
            HygieneViolation(
                "no-os-environ-mutation", filename, node.lineno, ast.unparse(node.func)
            )
        )
    os_module_attr_mutator = (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in os_aliases
        and node.func.attr in {"putenv", "unsetenv"}
    )
    os_module_name_mutator = isinstance(node.func, ast.Name) and (
        node.func.id in os_mutator_aliases
    )
    if os_module_attr_mutator or os_module_name_mutator:
        violations.append(
            HygieneViolation(
                "no-os-environ-mutation", filename, node.lineno, ast.unparse(node.func)
            )
        )
    return violations


def _delete_violations(
    node: ast.Delete,
    filename: str,
    os_aliases: set[str],
    environ_aliases: set[str],
) -> list[HygieneViolation]:
    """del 语句目标上的 os.environ 项删除违规。"""
    violations: list[HygieneViolation] = []
    for target in node.targets:
        if isinstance(target, ast.Subscript) and _is_os_environ(
            target.value, os_aliases, environ_aliases
        ):
            violations.append(
                HygieneViolation(
                    "no-os-environ-mutation", filename, node.lineno, ast.unparse(target)
                )
            )
    return violations


def scan_hygiene_source(source: str, filename: str = "<source>") -> list[HygieneViolation]:
    tree = ast.parse(source, filename=filename)
    workflow_controller_imports, os_aliases, environ_aliases, os_mutator_aliases = (
        _collect_import_aliases(tree)
    )
    violations: list[HygieneViolation] = []
    for node in ast.walk(tree):
        violations.extend(
            _assignment_violations(
                node, filename, workflow_controller_imports, os_aliases, environ_aliases
            )
        )
        if isinstance(node, ast.Delete):
            violations.extend(_delete_violations(node, filename, os_aliases, environ_aliases))
        if isinstance(node, ast.Call):
            violations.extend(
                _call_violations(node, filename, os_aliases, environ_aliases, os_mutator_aliases)
            )
    return sorted(set(violations))


def scan_hygiene_tree(root: Path) -> list[HygieneViolation]:
    violations: list[HygieneViolation] = []
    for path in sorted(root.rglob("*.py")):
        if "architecture" in path.parts or "support" in path.parts:
            continue
        with tokenize.open(path) as source_file:
            violations.extend(scan_hygiene_source(source_file.read(), path.as_posix()))
    return sorted(violations)
