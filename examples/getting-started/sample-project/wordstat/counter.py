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

"""词频统计核心。"""

import re
from collections import Counter
from pathlib import Path

from wordstat.filters import remove_stopwords


def count_words(text: str) -> Counter:
    """统计文本中每个词的出现次数。

    改进版本：
    - 使用正则表达式提取单词（排除标点符号）
    - 大小写归一化（统一转为小写）
    - 应用停用词过滤
    """
    # 使用正则表达式只匹配单词字符，自动排除标点
    words = re.findall(r"\b\w+\b", text.lower())
    # 应用停用词过滤
    filtered_words = remove_stopwords(words)
    return Counter(filtered_words)


def top_n(text: str, n: int = 10) -> list[tuple[str, int]]:
    """返回出现次数最多的前 n 个词（降序）。"""
    return count_words(text).most_common(n)


def count_file(path: str | Path) -> list[tuple[str, int]]:
    """读取文件内容并返回 top-10 词频。"""
    text = Path(path).read_text(encoding="utf-8")
    return top_n(text)
