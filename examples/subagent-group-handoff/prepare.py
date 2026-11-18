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

"""Generate deterministic input large enough for the checker context probe."""

from pathlib import Path


def prepare(root: Path) -> None:
    inputs = root / "inputs"
    inputs.mkdir(exist_ok=True)
    for chunk in range(1, 5):
        lines = [f"# 检查输入 {chunk:02d}\n"]
        for row in range(1, 176):
            lines.append(
                f"{row:03d}. 检查项 {chunk:02d}-{row:03d}：核对 brief.md 中的目标、约束和验收条件；"
                "记录来源文件名和证据，区分已经验证的工作与尚未执行的步骤。"
                "如果需要交接，下一名代理必须先读自己的交接文档，再从未完成步骤继续。\n"
            )
        (inputs / f"chunk-{chunk:02d}.md").write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parent)
