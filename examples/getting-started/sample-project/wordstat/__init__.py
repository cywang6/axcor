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

"""wordstat — 一个迷你词频统计工具。

供 Workflow Controller getting-started demo 探索：worker 读这些源文件、理解架构、
写一份 SUMMARY.md（功能 / 架构 / 问题 / 改进建议）。
"""

__version__ = "0.1.0"

__all__ = ["count_file", "count_words", "top_n"]

from wordstat.counter import count_file, count_words, top_n
