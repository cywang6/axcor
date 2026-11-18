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

"""正规驱动提示词的长度与重试上下文契约。"""

from __future__ import annotations

from workflow_controller.plugins.graph.driver.prompts import build_retry_context


def test_retry_context_keeps_enough_verifier_feedback():
    reason = "x" * 3000 + "FAILED"
    context = build_retry_context({}, {"attempt": "2"}, reason)
    feedback = context.split("上一轮验收反馈：", 1)[1].split("\n", 1)[0]
    assert feedback == reason[:2000]
