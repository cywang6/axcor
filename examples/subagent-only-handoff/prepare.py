#!/usr/bin/env python3
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

"""Create reproducible long inputs for the Claude CLI child-only handoff demo."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def prepare(root: Path) -> list[Path]:
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for chunk, count in enumerate((175,) * 10, start=1):
        lines = [f"# 审阅输入 {chunk:02d}\n"]
        for row in range(1, count + 1):
            code = hashlib.sha256(f"{chunk}:{row}".encode()).hexdigest()[:20]
            lines.append(
                f"{row:03d}. 记录 {code}：该条用于模拟长文本审阅。输入必须是 UTF-8 Markdown；"
                "输出只包含标题、三条要点和来源文件名；文件不存在与内容为空时须给出清楚错误；"
                "处理过程不得访问网络，所有判断必须依据当前文件内容和已经核实的进度记录。\n"
            )
            if row == count * 3 // 4:
                lines.append(
                    f"ALERT-{chunk:02d}：第 {chunk:02d} 块的异常是来源文件名"
                    f"不得包含目录路径；核对码 {code}。\n"
                )
        target = inputs / f"chunk-{chunk:02d}.md"
        target.write_text("".join(lines), encoding="utf-8")
        generated.append(target)
    return generated


if __name__ == "__main__":
    _configure_logging()
    for path in prepare(Path(__file__).resolve().parent):
        logger.info(f"{path.name}: {path.stat().st_size} bytes")
