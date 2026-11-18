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

"""Clear this example's old run artifacts before starting a new Web session."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from handoff_demo_lib import configure_stdout_logging, remove_handoff_artifacts

logger = logging.getLogger(__name__)

RESET_FILE_NAMES = (
    "inputs",
    "progress",
    "result.md",
    "audit.md",
    "report.md",
    ".workflow-controller/handoffs",
    ".workflow-controller/handoff-agent-prompt.md",
)
RESET_GLOBS = ("*-handoff-*-doc", "*-handoff-*-doc.done")


def reset(root: Path) -> None:
    remove_handoff_artifacts(root, RESET_FILE_NAMES, RESET_GLOBS)


if __name__ == "__main__":
    configure_stdout_logging(logger)
    reset(Path(__file__).resolve().parent)
    logger.info("已清理 subagent-only-handoff 的上次运行产物")
