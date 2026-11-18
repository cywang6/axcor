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

"""内核 CLI 隔离门禁：kernel-only 导入链无 Web 依赖，web 子命令缺席时可诊断。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support import subprocess_python_paths

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 子进程脚本：装 web 导入守卫后（1）导入内核 CLI 不得触碰 web 模块；
# （2）执行 web 子命令应得到可诊断错误而非裸 traceback。
_PROBE_SCRIPT = """
import json, sys
from tests.support.headless_acceptance import WebImportBlocker

sys.meta_path.insert(0, WebImportBlocker())
import workflow_controller.__main__ as cli_main

assert "workflow_controller.web" not in sys.modules, "kernel CLI import chain pulled in web"

from click.testing import CliRunner

result = CliRunner().invoke(cli_main.cli, ["web", "--no-open"])
print(json.dumps({"exit_code": result.exit_code, "output": result.output}))
"""


def test_kernel_cli_imports_without_web_and_web_command_is_diagnosable() -> None:
    env = dict(os.environ)
    python_path = subprocess_python_paths(Path(__file__).resolve().parent)
    if inherited := env.get("PYTHONPATH"):
        python_path.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(python_path)

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["exit_code"] != 0, "web 缺席时不得静默成功"
    assert "kernel-only" in report["output"], report["output"]
