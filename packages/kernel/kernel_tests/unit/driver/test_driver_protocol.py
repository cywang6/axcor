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

from inspect import signature
from typing import TYPE_CHECKING

import pytest

from workflow_controller.driver.claude_cli import ClaudeCLIDriver
from workflow_controller.driver.claude_sdk import ClaudeSDKDriver
from workflow_controller.driver.codex_cli import CodexCLIDriver
from workflow_controller.driver.opencode import OpenCodeDriver

if TYPE_CHECKING:
    from workflow_controller.driver.protocol import Driver


@pytest.mark.parametrize(
    "driver_type",
    [ClaudeCLIDriver, ClaudeSDKDriver, CodexCLIDriver, OpenCodeDriver],
)
def test_drivers_share_permission_mode_fallback(driver_type: type[Driver]) -> None:
    assert getattr(driver_type(), "_permission_mode") == "bypassPermissions"
    assert getattr(driver_type(permission_mode="read-only"), "_permission_mode") == "read-only"


@pytest.mark.parametrize(
    "driver_type",
    [ClaudeCLIDriver, ClaudeSDKDriver, CodexCLIDriver, OpenCodeDriver],
)
def test_driver_query_permission_mode_signature_matches_base(
    driver_type: type[Driver],
) -> None:
    parameter = signature(driver_type.query).parameters["permission_mode"]
    assert parameter.annotation == "str"
    assert parameter.default == ""
