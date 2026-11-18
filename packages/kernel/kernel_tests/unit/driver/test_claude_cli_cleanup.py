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

from unittest.mock import MagicMock

from workflow_controller.driver import cli_base
from workflow_controller.driver.claude_cli import ClaudeCLIDriver


def test_arm_then_disarm_no_kill(monkeypatch):
    """Normal exit path: arming then disarming leaves no finalizer and never
    kills (the explicit kill in the query finally handles that).
    """
    killed: list = []
    monkeypatch.setattr(cli_base, "kill_proc_group", lambda p: killed.append(p))
    d = ClaudeCLIDriver()
    proc = MagicMock(pid=111)
    getattr(d, "_arm_cleanup")(proc)
    assert getattr(d, "_finalizer") is not None and getattr(d, "_finalizer").alive
    getattr(d, "_disarm_cleanup")()
    assert getattr(d, "_finalizer") is None
    assert killed == []


def test_finalizer_kills_when_not_disarmed(monkeypatch):
    """If the driver is GC'd / interpreter exits before a clean teardown,
    the finalizer kills its own subprocess tree.
    """
    killed: list = []
    monkeypatch.setattr(cli_base, "kill_proc_group", lambda p: killed.append(p.pid))
    d = ClaudeCLIDriver()
    proc = MagicMock(pid=222)
    getattr(d, "_arm_cleanup")(proc)
    getattr(d, "_finalizer")()  # simulate GC / atexit trigger
    assert killed == [222]


def test_instances_are_independent(monkeypatch):
    """Two drivers track separate subprocesses — no shared module-level state.
    One exiting cleanly must not affect the other's cleanup.
    """
    killed: list = []
    monkeypatch.setattr(cli_base, "kill_proc_group", lambda p: killed.append(p.pid))
    d1, d2 = ClaudeCLIDriver(), ClaudeCLIDriver()
    p1, p2 = MagicMock(pid=1), MagicMock(pid=2)
    getattr(d1, "_arm_cleanup")(p1)
    getattr(d2, "_arm_cleanup")(p2)
    getattr(d1, "_disarm_cleanup")()  # d1 exits cleanly
    getattr(d2, "_finalizer")()  # d2 dies abnormally -> only its proc killed
    assert killed == [2]
