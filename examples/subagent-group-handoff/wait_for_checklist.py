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

"""Keep the other two agents in flight until the checker finishes."""

import sys
import time
from pathlib import Path


def wait(root: Path, timeout: float = 540) -> None:
    deadline = time.monotonic() + timeout
    while not (root / "checklist.md").is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("checker did not finish within 9 minutes")
        time.sleep(1)


if __name__ == "__main__":
    try:
        wait(Path(__file__).resolve().parent)
    except TimeoutError as exc:
        sys.exit(str(exc))
