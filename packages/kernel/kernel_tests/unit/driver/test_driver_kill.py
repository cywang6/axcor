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

"""Driver.kill() — stop 之后强制 reap 底层子进程组。

背景:claude_sdk runtime 下,claude_agent_sdk 起的 `_bundled/claude` 会 fork 孙进程
(node / mcp server)。SDK 的 `close()` SIGTERM 只打到主进程,孙进程变孤儿继续吃
CPU(用户 stop session 后 worker 仍 10% CPU 僵死的根因)。`kill()` 照搬 claude_cli
的 setsid + killpg 思路,在 `stop()` 优雅关闭之后兜底强杀整个进程组。

Driver.kill() 默认 pass(不强制所有 runtime 实现——opencode 等可先继承默认,
后补)。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from workflow_controller.driver.protocol import Driver


class _MinimalDriver(Driver):
    """最小具体 Driver,测 kill() 默认实现。"""

    @property
    def is_running(self) -> bool:
        return False

    def query(self, *a, **kw):  # type: ignore[override]
        ...

    async def stop(self) -> None:
        pass


class TestDriverKillProtocol:
    @staticmethod
    def test_default_kill_is_noop() -> None:
        """Driver.kill 默认 pass,不强制 runtime 实现。"""
        asyncio.run(_MinimalDriver().kill())


class TestSessionStopCallsKill:
    async def test_stop_calls_driver_kill_after_stop(self, monkeypatch) -> None:
        from workflow_controller.session.session import Session

        s = Session(name="t")
        driver = MagicMock()
        driver.stop = AsyncMock()
        driver.kill = AsyncMock()
        monkeypatch.setattr(s, "_driver", driver)

        await s.stop()

        driver.stop.assert_called_once()
        driver.kill.assert_called_once()  # stop 优雅关闭后兜底 kill

    async def test_stop_no_driver_safe(self, monkeypatch) -> None:
        from workflow_controller.session.session import Session

        s = Session(name="t")
        monkeypatch.setattr(s, "_driver", None)
        await s.stop()  # 无 driver 不崩


class TestClaudeCLIKill:
    @staticmethod
    def test_kill_invokes_kill_proc(monkeypatch) -> None:
        from workflow_controller.driver import cli_base
        from workflow_controller.driver.claude_cli import ClaudeCLIDriver

        d = ClaudeCLIDriver.__new__(ClaudeCLIDriver)  # 跳过 __init__
        proc = MagicMock(returncode=None, pid=12345)
        monkeypatch.setattr(d, "_proc", proc, raising=False)

        called: list = []
        monkeypatch.setattr(cli_base, "kill_proc_group", lambda p: called.append(p))

        asyncio.run(d.kill())
        assert called == [proc]

    @staticmethod
    def test_kill_no_proc_safe(monkeypatch) -> None:
        from workflow_controller.driver.claude_cli import ClaudeCLIDriver

        d = ClaudeCLIDriver.__new__(ClaudeCLIDriver)
        monkeypatch.setattr(d, "_proc", None, raising=False)
        asyncio.run(d.kill())  # 无 proc 不崩


class TestClaudeSDKKill:
    @staticmethod
    def test_kill_kills_process_group(monkeypatch) -> None:
        """有 transport + _process 时,kill 整个进程组(SIGKILL 孙进程)。"""
        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        d = ClaudeSDKDriver.__new__(ClaudeSDKDriver)
        proc = MagicMock(returncode=None, pid=999)
        transport = MagicMock()
        monkeypatch.setattr(transport, "_process", proc)
        monkeypatch.setattr(d, "_transport", transport, raising=False)

        killed: list[int] = []
        monkeypatch.setattr("os.getpgid", lambda pid: 7777)
        monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append((pgid, sig)))

        asyncio.run(d.kill())
        assert killed == [(7777, 9)]  # SIGKILL=9,整组

    @staticmethod
    def test_kill_no_transport_safe(monkeypatch) -> None:
        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        d = ClaudeSDKDriver.__new__(ClaudeSDKDriver)
        monkeypatch.setattr(d, "_transport", None, raising=False)
        asyncio.run(d.kill())  # 无 transport 不崩

    @staticmethod
    def test_kill_dead_process_safe(monkeypatch) -> None:
        from workflow_controller.driver.claude_sdk import ClaudeSDKDriver

        d = ClaudeSDKDriver.__new__(ClaudeSDKDriver)
        proc = MagicMock(returncode=0, pid=999)  # 已退出
        transport = MagicMock()
        monkeypatch.setattr(transport, "_process", proc)
        monkeypatch.setattr(d, "_transport", transport, raising=False)

        monkeypatch.setattr("os.getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError))
        asyncio.run(d.kill())  # 进程已死/找不到,不崩
