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

"""停用词过滤。"""

# 一份极简的英文停用词表。实际项目应按语种扩充，或从外部文件加载。
STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "to",
        "of",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "were",
        "it",
        "this",
        "that",
        "with",
        "as",
        "at",
        "by",
        "be",
        "from",
    }
)


def remove_stopwords(words):
    """从词序列中剔除停用词（大小写不敏感）。

    注意：counter.count_words 目前并不调用本函数 —— 停用词过滤尚未接入
    主流程（集成是待办项之一）。
    """
    return [w for w in words if w.lower() not in STOPWORDS]
