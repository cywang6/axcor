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

"""toggle 写回 config.yaml 时必须保留注释/结构——定点改顶层 ``enabled:``
行，不走 ``yaml.dump`` 整体重写（后者丢注释，对带说明性注释的模板
plugin 如 staged_pipeline 尤其伤）。覆盖纯函数 ``_flip_enabled_in_text``。
"""

from __future__ import annotations

from workflow_controller.api.plugin_api import _flip_enabled_in_text

# 真实 staged_pipeline config.yaml 的结构样本（多行注释 + stages），保注释回归用。
_STAGED_CONFIG = """\
name: staged_pipeline
version: "0.1.0"
enabled: false

# migbot canonical 5 阶段（参考模板；真实领域照抄改 stages 即可，零动核心）。
# 每阶段：artifact（文件或目录）+ gate（exists | non_placeholder | command）。
stages:
  - name: spec
    artifact: spec.md
    gate: non_placeholder
"""


def test_flip_false_to_true_preserves_comments_and_stages() -> None:
    out = _flip_enabled_in_text(_STAGED_CONFIG, True)

    assert "enabled: true\n" in out
    assert "enabled: false" not in out
    # 注释和 stages 逐字节保留
    assert "# migbot canonical 5 阶段" in out
    assert "    artifact: spec.md" in out
    assert "    gate: non_placeholder" in out
    # 行数不变（没多没少）
    assert out.count("\n") == _STAGED_CONFIG.count("\n")


def test_flip_true_to_false() -> None:
    text = _STAGED_CONFIG.replace("enabled: false", "enabled: true")
    out = _flip_enabled_in_text(text, False)

    assert "enabled: false\n" in out
    assert "enabled: true" not in out


def test_flip_preserves_trailing_inline_comment_on_enabled_line() -> None:
    text = "name: x\nenabled: false  # master switch\n"
    out = _flip_enabled_in_text(text, True)

    assert out == "name: x\nenabled: true  # master switch\n"


def test_flip_normalizes_scalar_casing() -> None:
    text = "name: x\nenabled: True\n"
    out = _flip_enabled_in_text(text, True)

    assert "enabled: true\n" in out


def test_flip_only_touches_top_level_enabled() -> None:
    # 缩进的 enabled（嵌套）和注释里的 enabled 都不该被改；只改顶层那一行。
    text = (
        "name: x\n"
        "enabled: false\n"
        "stages:\n"
        "  - name: spec\n"
        "    enabled: false  # 嵌套，不动\n"
        "# enabled: true  注释，不动\n"
    )
    out = _flip_enabled_in_text(text, True)

    assert out.startswith("name: x\nenabled: true\n")
    assert "    enabled: false  # 嵌套，不动\n" in out
    assert "# enabled: true  注释，不动\n" in out


def test_flip_adds_enabled_line_when_missing() -> None:
    text = 'name: x\nversion: "0.1.0"\n'
    out = _flip_enabled_in_text(text, True)

    assert "enabled: true" in out
    # 原内容保留
    assert "name: x\n" in out
    assert 'version: "0.1.0"\n' in out
