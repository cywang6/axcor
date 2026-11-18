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

"""钩子处理器（gate/Stop/PreToolUse）与 opencode driver 单测共享的装配样板。

- ``hook_orch_test_config``：三个钩子处理器测试文件共用的 AppConfig 构造。
- ``captured_echo_opencode_query`` / ``bare_echo_opencode_query``：opencode
  driver 读取 overlay（instructions/subagent_hints）测试共用的 query 样板，
  以 echo 充当 opencode 二进制，避免真实子进程。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from workflow_controller.config.settings import AppConfig, HookServerConfig, ReviewerConfig
from workflow_controller.driver.hooks import DriverHooks

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    import pytest


def hook_orch_test_config(project_dir: Path, *, reviewer_enabled: bool = True) -> AppConfig:
    """钩子处理器测试通用的 AppConfig：hook server 关闭，reviewer 可选开关。"""
    return AppConfig(
        project_dir=str(project_dir),
        harmony_output_path=str(project_dir),
        hook_server=HookServerConfig(enabled=False),
        reviewer=ReviewerConfig(enabled=reviewer_enabled),
    )


async def _async_none() -> None:
    return None


async def captured_echo_opencode_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overlays: dict[str, Any],
) -> dict[str, Any]:
    """带 overlay hooks 跑一次 echo query，返回 build_config_content 收到的 kwargs。

    _maybe_start_gate 被禁用（gate 不启动），system_prompt 固定为 "SP"，
    便于只考察 overlay 传递路径。
    """
    from workflow_controller.driver import opencode as oc_mod
    from workflow_controller.driver.opencode import OpenCodeDriver

    captured: dict[str, Any] = {}

    def fake_build(*args: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return json.dumps({"compaction": {"auto": False}})

    monkeypatch.setattr(oc_mod, "build_config_content", fake_build)
    monkeypatch.setattr(
        OpenCodeDriver,
        "_maybe_start_gate",
        lambda self, hooks, sid: _async_none(),
    )

    hooks = DriverHooks(session_id="s1", get_overlays=lambda: overlays)
    d = OpenCodeDriver(opencode_bin="echo")  # echo never spawns opencode
    async for _ in d.query(
        "p",
        session_id="s1",
        cwd=str(tmp_path),
        hooks=hooks,
        system_prompt="SP",
    ):
        pass
    return captured


async def bare_echo_opencode_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[bool]:
    """无 hooks、无 system_prompt 的裸 echo query；返回 build_config_content 调用记录。"""
    from workflow_controller.driver import opencode as oc_mod
    from workflow_controller.driver.opencode import OpenCodeDriver

    called: list[bool] = []
    original = oc_mod.build_config_content

    def spy(*args: Any, **kwargs: Any) -> str:
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(oc_mod, "build_config_content", spy)
    d = OpenCodeDriver(opencode_bin="echo")
    async for _ in d.query("p", session_id="s1", cwd=str(tmp_path)):
        pass
    return called
