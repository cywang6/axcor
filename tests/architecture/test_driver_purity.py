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

"""graph 驱动目录纯度门禁：决策代码禁 I/O 原语。

驱动是纯决策组件（可脱离宿主单测）；异步函数、文件/数据库/子进程等
原语任一命中即为越界——执行机制归宿主执行服务。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


import pytest

from tests.architecture.layout import plugin_package_dir

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("async def", re.compile(r"\basync\s+def\b")),
    # 负向 lookbehind 而非 [^.\w] 字符类：字符类要求前导字符，列 0 的裸 open( 会漏
    ("open()", re.compile(r"(?<![.\w])open\s*\(")),
    ("sqlite3", re.compile(r"\bsqlite3\b")),
    ("subprocess", re.compile(r"\bsubprocess\b")),
)


def _pattern_violations(path: Path, driver_dir: Path) -> list[str]:
    """单文件命中的禁用原语（相对路径:行号: 标签: 行文本）。"""
    source = path.read_text(encoding="utf-8")
    hits: list[str] = []
    for label, pattern in FORBIDDEN_PATTERNS:
        for lineno, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(driver_dir)}:{lineno}: {label}: {line.strip()}")
    return hits


def test_graph_driver_directory_contains_no_io_primitives() -> None:
    graph_root = plugin_package_dir("graph")
    if graph_root is None:
        pytest.skip("graph 插件不在本仓")
    driver_dir = graph_root / "driver"
    assert driver_dir.is_dir(), f"driver 目录不存在: {driver_dir}"
    violations: list[str] = []
    for path in sorted(driver_dir.rglob("*.py")):
        violations.extend(_pattern_violations(path, driver_dir))
    assert not violations, "driver/ 出现 I/O 原语:\n" + "\n".join(violations)


def test_open_pattern_catches_column_zero_call() -> None:
    open_pattern = dict(FORBIDDEN_PATTERNS)["open()"]
    assert open_pattern.search("open(path)")
    assert not open_pattern.search("self.open(path)")
    assert not open_pattern.search("xopen(path)")
