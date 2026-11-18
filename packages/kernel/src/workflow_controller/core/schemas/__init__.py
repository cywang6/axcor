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

"""workflow-bundle v1 JSON Schema——导出侧权威，随包分发。

本模块是单一权威：导出自校验与打包进 bundle 读同一份文本，杜绝
workflow_controller / insight 各自手写校验漂移。insight 用包内 `workflow.schema.json`
同源校验，不在任何地方镜像或从服务拉取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("workflow-bundle.v1.schema.json")

WORKFLOW_BUNDLE_SCHEMA_TEXT = _SCHEMA_PATH.read_text(encoding="utf-8")
WORKFLOW_BUNDLE_SCHEMA: dict[str, Any] = json.loads(WORKFLOW_BUNDLE_SCHEMA_TEXT)
