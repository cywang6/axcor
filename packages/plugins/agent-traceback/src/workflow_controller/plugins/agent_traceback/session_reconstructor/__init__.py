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

"""从 cannbot session db 重建 Workflow Controller session 的子包。

模块组成::

    db_reader.py            — cannbot db 读取 + 时间线交错排序
    file_state_rebuilder.py — 重放 write/edit/bash + git 快照仓
    l2_transcriber.py       — Turn/ToolCall → opencode part/message 转写
    checkpoint_deriver.py   — task 调用边界推导检查点
    orchestrator.py         — 编排上述四步 + 注册 session

入口：``ReconstructOrchestrator.import_session(db_path, target_cwd)``。
"""

from __future__ import annotations
