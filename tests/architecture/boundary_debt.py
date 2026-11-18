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

from __future__ import annotations

from tests.architecture.dependency_debt import Debt

BOUNDARY_DEBT: tuple[Debt, ...] = (
    # 仅剩组合根保留项：app.state.orchestrator 作为 Console 装配器的输入
    Debt(
        "web-no-state-orchestrator",
        "src/workflow_controller/web/app.py",
        "app.state.orchestrator",
        "orchestrator",
    ),
)
