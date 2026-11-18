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

"""架构门禁（边界/依赖）诊断报告的共享构建工具。"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from tests.architecture.boundaries import BoundaryOccurrence
    from tests.architecture.rules import DebtKey, RuleDefinition, Violation


def debt_gate_summary(
    items: Iterable[BoundaryOccurrence | Violation], expected: Mapping[DebtKey, int]
) -> tuple[
    Counter[DebtKey],
    Counter[DebtKey],
    Counter[DebtKey],
    Counter[DebtKey],
    dict[DebtKey, list[BoundaryOccurrence | Violation]],
]:
    """按 debt_key 汇总实际计数与基线，返回 (actual, allowed, increased, reduced, grouped)。"""
    actual = Counter(item.debt_key for item in items)
    allowed = Counter(expected)
    grouped: dict[DebtKey, list[BoundaryOccurrence | Violation]] = defaultdict(list)
    for item in items:
        grouped[item.debt_key].append(item)
    return actual, allowed, actual - allowed, allowed - actual, grouped


def increased_entry_tail(
    actual: Counter[DebtKey], allowed: Counter[DebtKey], key: DebtKey, definition: RuleDefinition
) -> list[str]:
    """“新增/增加债务”条目末尾的计数与修复建议两行。"""
    return [
        f"  count: actual={actual[key]}, allowed={allowed[key]}",
        f"  preferred fix: {definition.fix_guidance}",
    ]


def stale_debt_section(
    reduced: Counter[DebtKey],
    allowed: Counter[DebtKey],
    actual: Counter[DebtKey],
    subject: str,
    debt_module: str,
) -> list[str]:
    """“基线过期/债务减少”段落；subject 取 access 或 dependency。"""
    lines = ["", "STALE OR REDUCED DEBT — the code improved; tighten the baseline:"]
    for key in sorted(reduced):
        rule, source, target, symbol = key
        lines.extend(
            [
                f"- [{rule}] {source}",
                f"  {subject}: {target} ({symbol or '-'})",
                f"  count: baseline={allowed[key]}, actual={actual[key]}",
                "  preferred fix: reduce or remove the exact entry in "
                f"tests/architecture/{debt_module}.",
            ]
        )
    return lines
