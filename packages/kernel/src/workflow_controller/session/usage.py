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

"""上下文用量读数解析：区分「缺失」与「明确为零」。"""

from __future__ import annotations

import math


def context_input_tokens(usage: object) -> int | None:
    """求输入占用（input + cache creation + cache read）；无有效读数返回 None。"""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if not isinstance(usage, dict) or not any(k in usage for k in keys):
        return None
    values = [usage.get(k, 0) for k in keys]
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in values
    ):
        return None
    return int(sum(values))
