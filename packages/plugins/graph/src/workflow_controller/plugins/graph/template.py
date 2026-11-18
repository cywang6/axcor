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

"""``{{VAR}}`` 模板替换，支持嵌套 5 层。

匹配 ``{{OP}}`` 和 ``{{argv.OP}}`` 两种模式（后者提取变量名 OP）。
缺失变量保留原占位符（不抛错，便于调试）。
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"\{\{(?:argv\.)?(\w+)\}\}")
_MAX_DEPTH = 5


def render(text: str, variables: dict) -> str:
    for _ in range(_MAX_DEPTH):
        new_text = _PATTERN.sub(
            lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
            text,
        )
        if new_text == text:
            break
        text = new_text
    return text
