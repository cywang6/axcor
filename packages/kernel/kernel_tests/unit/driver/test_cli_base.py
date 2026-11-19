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

import pytest

from workflow_controller.driver.cli_base import is_stderr_error_line


@pytest.mark.parametrize(
    "line",
    [
        "ERROR failed",
        "ERROR: failed",
        "ERROR=failed",
        "2026-11-17T00:00:00Z ERROR failed",
        "Error: failed",
        "error: failed",
        "2026-11-17T00:00:00Z Error: failed",
        "[ERROR] failed",
        "level=ERROR failed",
        "2026-11-17T00:00:00Z level=ERROR failed",
        "[cpx-cli] ERROR: launch failed",
        "[cpx-cli] error: launch failed",
    ],
)
def test_stderr_error_line_matches_explicit_markers(line: str) -> None:
    assert is_stderr_error_line(line)


@pytest.mark.parametrize(
    "line",
    [
        "a normal error occurred",
        "warning: retry failed",
        "some error without marker",
    ],
)
def test_stderr_error_line_ignores_plain_error_word(line: str) -> None:
    assert not is_stderr_error_line(line)
