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

"""Source-only scanner for statically determinable Python imports.

Literal ``__import__`` and ``importlib.import_module`` calls are included, but
runtime-computed module names are intentionally out of scope. Only a bare
``TYPE_CHECKING`` or ``typing.TYPE_CHECKING`` condition suppresses a branch.
When ``source_root`` is the repository's ``src`` directory, record paths are
relative to the repository root so they retain the ``src/...`` debt-key format.
"""

from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, order=True)
class ImportRecord:
    source: str
    line: int
    source_module: str
    target_module: str
    symbol: str
    kind: str

    @property
    def source_package(self) -> str:
        """Return the package boundary shown in violation diagnostics.

        Built-in plugins retain their plugin name (for example,
        ``workflow_controller.plugins.breather``); other modules collapse to their top-level
        Workflow Controller package (for example, ``workflow_controller.api``).
        """
        parts = self.source_module.split(".")
        if len(parts) == 1:
            return parts[0]
        if parts[1] == "plugins" and len(parts) >= 3:
            return ".".join(parts[:3])
        return ".".join(parts[:2])


def _module_name(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, *, path: Path, source_module: str, is_package: bool) -> None:
        self.path = path
        self.source_module = source_module
        self.is_package = is_package
        self.records: list[ImportRecord] = []

    @staticmethod
    def _is_type_checking(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "typing"
            and node.attr == "TYPE_CHECKING"
        )

    def visit_if(self, node: ast.If) -> None:
        if self._is_type_checking(node.test):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(node, alias.name, alias.name, "import")

    def visit_import_from(self, node: ast.ImportFrom) -> None:
        target = node.module or ""
        if node.level:
            target = self._resolve_relative(node.level, node.module)
        for alias in node.names:
            self._add(node, target, alias.name, "from")

    def visit_call(self, node: ast.Call) -> None:
        target: str | None = None
        kind = ""
        importlib_import_module = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
        )
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            kind = "__import__"
        elif importlib_import_module:
            kind = "importlib"
        if kind and node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            if isinstance(value, str):
                target = value
        if target:
            self._add(node, target, "", kind)
        self.generic_visit(node)

    visit_If = visit_if
    visit_Import = visit_import
    visit_ImportFrom = visit_import_from
    visit_Call = visit_call

    def _resolve_relative(self, level: int, module: str | None) -> str:
        package = self.source_module if self.is_package else self.source_module.rpartition(".")[0]
        parts = package.split(".") if package else []
        keep = max(0, len(parts) - level + 1)
        prefix = parts[:keep]
        if module:
            prefix.extend(module.split("."))
        return ".".join(prefix)

    def _add(
        self,
        node: ast.Import | ast.ImportFrom | ast.Call,
        target: str,
        symbol: str,
        kind: str,
    ) -> None:
        self.records.append(
            ImportRecord(
                source=self.path.as_posix(),
                line=node.lineno,
                source_module=self.source_module,
                target_module=target,
                symbol=symbol,
                kind=kind,
            )
        )


def scan_imports(path: Path, source_root: Path) -> list[ImportRecord]:
    module = _module_name(path, source_root)
    visitor = _ImportVisitor(
        path=path.relative_to(source_root.parent),
        source_module=module,
        is_package=path.name == "__init__.py",
    )
    with tokenize.open(path) as source_file:
        source = source_file.read()
    visitor.visit(ast.parse(source, filename=str(path)))
    return visitor.records


def scan_tree(source_root: Path) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for path in sorted(source_root.rglob("*.py")):
        records.extend(scan_imports(path, source_root))
    return records
