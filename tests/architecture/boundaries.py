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

from tests.architecture.gate_report import (
    debt_gate_summary,
    increased_entry_tail,
    stale_debt_section,
)
from tests.architecture.rules import DebtKey, RuleDefinition

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    _ScopeNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, order=True)
class BoundaryOccurrence:
    rule: str
    source: str
    line: int
    target: str
    symbol: str

    @property
    def debt_key(self) -> tuple[str, str, str, str]:
        return (self.rule, self.source, self.target, self.symbol)

    def format(self) -> str:
        return f"{self.source}:{self.line}: {self.rule}: {self.target} ({self.symbol})"


BOUNDARY_RULES = {
    "web-no-state-orchestrator": RuleDefinition(
        problem="Web code accesses Orchestrator through app.state instead of CoreAPI.",
        fix_guidance=(
            "Expose the capability through CoreAPI and read request.app.state.api instead."
        ),
    ),
    "builtin-plugin-direct-orchestrator-access": RuleDefinition(
        problem="A built-in plugin accesses the host Orchestrator through PluginContext.",
        fix_guidance=(
            "Use PluginContext.services; add or extend a capability port for the operation."
        ),
    ),
    "builtin-plugin-no-orchestrator-shim": RuleDefinition(
        problem="A built-in plugin reads a private Orchestrator compatibility shim.",
        fix_guidance=(
            "Use PluginContext.services and remove the private Orchestrator compatibility shim."
        ),
    ),
    "builtin-plugin-no-host-private-access": RuleDefinition(
        problem="A built-in plugin accesses a private member of the host Orchestrator.",
        fix_guidance=(
            "Expose the operation through PluginContext.services instead of calling a private "
            "host member."
        ),
    ),
}

_DIRECT_ORCHESTRATOR_PATHS = frozenset(
    {
        ("ctx", "orchestrator"),
        ("self", "_ctx", "orchestrator"),
    }
)
_SHIM_ORCHESTRATOR_PATHS = frozenset(
    {
        ("self", "_orchestrator"),
        ("plugin", "_orchestrator"),
    }
)
_HOST_ORCHESTRATOR_PREFIXES = (
    *_DIRECT_ORCHESTRATOR_PATHS,
    *_SHIM_ORCHESTRATOR_PATHS,
    ("orchestrator",),
)


def _attribute_parts(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.NamedExpr):
            # (name := expr).attr 等价于沿 expr 的链继续取属性
            current = current.value
        else:
            break
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _starts_with(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return parts[: len(prefix)] == prefix


def _is_host_orchestrator_chain(parts: tuple[str, ...]) -> bool:
    return any(_starts_with(parts, prefix) for prefix in _HOST_ORCHESTRATOR_PREFIXES)


def _is_app_state_chain(parts: tuple[str, ...]) -> bool:
    return parts[-2:] == ("app", "state")


_ASSIGN_NODES = (ast.Assign, ast.AnnAssign, ast.NamedExpr)
_SCOPE_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _assigned_chains(
    node: ast.Assign | ast.AnnAssign | ast.NamedExpr,
) -> list[tuple[str, ...]]:
    """赋值目标的属性段（Name 目视为单段链，self._state 等为长链）。"""
    targets = [node.target] if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) else node.targets
    chains: list[tuple[str, ...]] = []
    for target in targets:
        if isinstance(target, ast.Name):
            chains.append((target.id,))
        elif isinstance(target, ast.Attribute):
            parts = _attribute_parts(target)
            if parts:
                chains.append(parts)
    return chains


@dataclass(frozen=True)
class _ScopeBindings:
    """单个词法作用域内生效的 app.state 别名链与字符串常量绑定。"""

    aliases: frozenset[tuple[str, ...]]
    constants: Mapping[str, str]


_NO_BINDINGS = _ScopeBindings(frozenset(), {})


def _iter_own_nodes(scope: _ScopeNode) -> Iterator[ast.AST]:
    """作用域直属节点：嵌套函数/类的体属于各自作用域，不在此展开。"""
    stack: list[ast.AST] = list(reversed(scope.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_NODES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _resolve_scope_bindings(scope: _ScopeNode, inherited: _ScopeBindings) -> _ScopeBindings:
    """并入本作用域源码顺序的赋值：同名取末次绑定，不再沿用早前的别名/常量。"""
    assignments = sorted(
        (
            node
            for node in _iter_own_nodes(scope)
            if isinstance(node, _ASSIGN_NODES) and node.value is not None
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    final_values: dict[tuple[str, ...], tuple[str, ...]] = {}
    final_exprs: dict[str, ast.expr] = {}
    for node in assignments:
        value = (
            _attribute_parts(node.value)
            if isinstance(node.value, (ast.Attribute, ast.Name))
            else ()
        )
        chains = _assigned_chains(node)
        for chain in chains:
            final_values[chain] = value
        if len(chains) == 1 and len(chains[0]) == 1:
            final_exprs[chains[0][0]] = node.value
    local_names = {chain[0] for chain in final_values if len(chain) == 1}
    aliases = {
        chain for chain in inherited.aliases if len(chain) > 1 or chain[0] not in local_names
    }
    # 只认末次绑定：早前是别名/常量、末次不再是的，不得继续生效
    changed = True
    while changed:
        changed = False
        for chain, value_parts in final_values.items():
            if not (_is_app_state_chain(value_parts) or value_parts in aliases):
                continue
            if chain not in aliases:
                aliases.add(chain)
                changed = True
    constants = {
        name: value for name, value in inherited.constants.items() if name not in local_names
    }
    for name, expr in final_exprs.items():
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            constants[name] = expr.value
    return _ScopeBindings(frozenset(aliases), constants)


def _fill_scope_bindings(
    scope: _ScopeNode,
    inherited: _ScopeBindings,
    bindings_by_scope: dict[ast.AST, _ScopeBindings],
) -> None:
    own = _resolve_scope_bindings(scope, inherited)
    bindings_by_scope[scope] = own
    for node in _iter_own_nodes(scope):
        if isinstance(node, _SCOPE_NODES):
            _fill_scope_bindings(node, own, bindings_by_scope)


def _scope_owners(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """每个节点到最近词法作用域节点的映射（装饰器等头部表达式随所属定义）。"""
    owners: dict[ast.AST, ast.AST] = {tree: tree}
    stack = [tree]
    while stack:
        parent = stack.pop()
        for child in ast.iter_child_nodes(parent):
            owners[child] = parent if isinstance(parent, _SCOPE_NODES) else owners[parent]
            stack.append(child)
    return owners


def _state_getattr_symbol(node: ast.Call, bindings: _ScopeBindings) -> str | None:
    """app.state（或其别名）上的 getattr 违规属性名；合规返回 None。

    字符串字面量与可归一的常量名按值判定；其余变量形态无法排除
    ``orchestrator``，一律按违规计。
    """
    if not isinstance(node.func, ast.Name) or node.func.id != "getattr" or len(node.args) < 2:
        return None
    receiver = _attribute_parts(node.args[0])
    if not (_is_app_state_chain(receiver) or receiver in bindings.aliases):
        return None
    attribute = node.args[1]
    if isinstance(attribute, ast.Constant):
        return "orchestrator" if attribute.value == "orchestrator" else None
    if isinstance(attribute, ast.Name) and attribute.id in bindings.constants:
        return "orchestrator" if bindings.constants[attribute.id] == "orchestrator" else None
    return "getattr"


def _call_occurrence(
    node: ast.Call, source: str, bindings: _ScopeBindings
) -> BoundaryOccurrence | None:
    """app.state 上的 getattr 违规；合规调用返回 None。"""
    symbol = _state_getattr_symbol(node, bindings)
    if symbol is None:
        return None
    return BoundaryOccurrence(
        "web-no-state-orchestrator",
        source,
        node.lineno,
        "app.state.orchestrator",
        symbol,
    )


def _attribute_occurrence(
    node: ast.Attribute,
    source: str,
    is_builtin_plugin: bool,
    bindings: _ScopeBindings,
) -> BoundaryOccurrence | None:
    """属性访问形态的边界违规；合规访问返回 None。"""
    parts = _attribute_parts(node)
    if parts[-1:] == ("orchestrator",) and (
        _is_app_state_chain(parts[:-1]) or parts[:-1] in bindings.aliases
    ):
        return BoundaryOccurrence(
            "web-no-state-orchestrator",
            source,
            node.lineno,
            "app.state.orchestrator",
            "orchestrator",
        )
    if is_builtin_plugin and parts in _DIRECT_ORCHESTRATOR_PATHS:
        return BoundaryOccurrence(
            "builtin-plugin-direct-orchestrator-access",
            source,
            node.lineno,
            "PluginContext.orchestrator",
            "orchestrator",
        )
    if is_builtin_plugin and parts in _SHIM_ORCHESTRATOR_PATHS:
        return BoundaryOccurrence(
            "builtin-plugin-no-orchestrator-shim",
            source,
            node.lineno,
            "Plugin compatibility shim",
            "_orchestrator",
        )
    if (
        is_builtin_plugin
        and node.attr.startswith("_")
        and _is_host_orchestrator_chain(_attribute_parts(node.value))
    ):
        return BoundaryOccurrence(
            "builtin-plugin-no-host-private-access",
            source,
            node.lineno,
            "PluginContext.orchestrator",
            node.attr,
        )
    return None


def _scan_file(path: Path, root: Path, repo_root: Path) -> list[BoundaryOccurrence]:
    """单个源码文件的边界违规（保持 ast.walk 顺序）。"""
    source = path.relative_to(repo_root).as_posix()
    with tokenize.open(path) as source_file:
        source_text = source_file.read()
    tree = ast.parse(source_text, filename=str(path))
    is_builtin_plugin = path.is_relative_to(root / "plugins")
    owners = _scope_owners(tree)
    bindings_by_scope: dict[ast.AST, _ScopeBindings] = {}
    _fill_scope_bindings(tree, _NO_BINDINGS, bindings_by_scope)
    occurrences: list[BoundaryOccurrence] = []
    for node in ast.walk(tree):
        bindings = bindings_by_scope[owners[node]]
        if isinstance(node, ast.Call):
            occurrence = _call_occurrence(node, source, bindings)
        elif isinstance(node, ast.Attribute):
            occurrence = _attribute_occurrence(node, source, is_builtin_plugin, bindings)
        else:
            continue
        if occurrence is not None:
            occurrences.append(occurrence)
    return occurrences


def scan_boundaries(root: Path, repo_root: Path) -> list[BoundaryOccurrence]:
    occurrences: list[BoundaryOccurrence] = []
    for path in sorted(root.rglob("*.py")):
        occurrences.extend(_scan_file(path, root, repo_root))
    return sorted(occurrences)


def format_boundary_diagnostics(
    occurrences: list[BoundaryOccurrence], expected: Mapping[DebtKey, int]
) -> str:
    actual, allowed, increased, reduced, grouped = debt_gate_summary(occurrences, expected)

    lines = ["Architecture boundary gate failed."]
    if increased:
        lines.extend(
            [
                "",
                "NEW OR INCREASED VIOLATIONS — fix the access, do not blindly expand debt:",
            ]
        )
        for key in sorted(increased):
            rule, _source, target, symbol = key
            definition = BOUNDARY_RULES[rule]
            details = "; ".join(item.format() for item in sorted(set(grouped[key])))
            lines.extend(
                [
                    f"- {details}",
                    f"  problem: {definition.problem}",
                    f"  access: {target} ({symbol or '-'})",
                    *increased_entry_tail(actual, allowed, key, definition),
                ]
            )

    if reduced:
        lines.extend(stale_debt_section(reduced, allowed, actual, "access", "boundary_debt.py"))

    lines.extend(
        [
            "",
            "Boundary debt baseline changes require an intentional architecture decision. "
            "Prefer repairing the access path.",
        ]
    )
    return "\n".join(lines)
