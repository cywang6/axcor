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

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Debt:
    rule: str
    source: str
    target: str
    symbol: str
    expected: int = 1

    @property
    def debt_key(self) -> tuple[str, str, str, str]:
        return (self.rule, self.source, self.target, self.symbol)


# Entries are deliberately exact. Add, remove, or update the expected count in
# the same change that alters the underlying dependency occurrences.
# 内置插件绕过服务端口的导入债务当前清零；新增此类依赖应先补服务端口。
DEPENDENCY_DEBT: tuple[Debt, ...] = ()
