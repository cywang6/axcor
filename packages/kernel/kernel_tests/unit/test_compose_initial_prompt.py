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

"""初始 prompt 组装测试：插件 prompt_prefix 依序前置、空白贡献被忽略。"""

from __future__ import annotations

import types

from workflow_controller.plugin.base import compose_initial_prompt


class _PrefixContributor:
    def __init__(self, txt: str | None) -> None:
        self.txt = txt

    def prompt_prefix(self, config) -> str | None:
        return self.txt


def test_compose_initial_prompt_prepends_plugin_prefixes():
    cfg = types.SimpleNamespace(initial_prompt="做任务")
    assert compose_initial_prompt([_PrefixContributor("纪律")], cfg) == "纪律\n\n做任务"
    assert (
        compose_initial_prompt([_PrefixContributor(None), _PrefixContributor("   ")], cfg)
        == "做任务"
    )
    assert (
        compose_initial_prompt([_PrefixContributor("A"), _PrefixContributor("B")], cfg)
        == "A\n\nB\n\n做任务"
    )
    assert compose_initial_prompt([], cfg) == "做任务"
