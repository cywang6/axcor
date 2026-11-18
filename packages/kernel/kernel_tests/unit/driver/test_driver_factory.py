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

from workflow_controller.driver import factory
from workflow_controller.driver.claude_cli import ClaudeCLIDriver
from workflow_controller.driver.claude_sdk import ClaudeSDKDriver
from workflow_controller.driver.codex_cli import CodexCLIDriver
from workflow_controller.driver.opencode import OpenCodeDriver


@pytest.fixture(autouse=True)
def _restore_runtime(monkeypatch):
    """Isolate global driver-runtime state and WORKFLOW_CONTROLLER_DRIVER across tests."""
    monkeypatch.delenv("WORKFLOW_CONTROLLER_DRIVER", raising=False)
    # Register the current value so set_config_runtime mutations are undone at teardown.
    monkeypatch.setattr(factory, "_config_runtime", getattr(factory, "_config_runtime"))


def test_default_runtime_is_claude_sdk():
    factory.set_config_runtime("claude_sdk")
    assert isinstance(factory.create_driver(None), ClaudeSDKDriver)


def test_create_driver_none_honors_config_runtime():
    factory.set_config_runtime("claude_cli")
    assert isinstance(factory.create_driver(None), ClaudeCLIDriver)


@pytest.mark.parametrize("runtime", ["claude_cli", "opencode", "codex_cli"])
def test_cpx_wraps_cli_runtime(runtime: str) -> None:
    """验证 cpx 只作为 CLI 命令前缀生效。"""
    driver = factory.create_driver(runtime, cli_wrapper="cpx")
    assert getattr(driver, "_command_prefix") == ["cpx"]


@pytest.mark.parametrize("runtime", ["claude_sdk", "claude_cli"])
def test_unknown_cli_wrapper_is_rejected(runtime: str) -> None:
    """验证未知 wrapper 被拒绝。"""
    with pytest.raises(ValueError, match="Unsupported CLI wrapper"):
        factory.create_driver(runtime, cli_wrapper="bogus")


def test_explicit_runtime_overrides_config():
    factory.set_config_runtime("claude_cli")
    assert isinstance(factory.create_driver("claude_sdk"), ClaudeSDKDriver)


def test_env_var_overrides_everything(monkeypatch):
    factory.set_config_runtime("claude_sdk")
    monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "claude_cli")
    assert isinstance(factory.create_driver(None), ClaudeCLIDriver)
    assert isinstance(factory.create_driver("claude_sdk"), ClaudeCLIDriver)


def test_unknown_runtime_raises():
    factory.set_config_runtime("bogus")
    with pytest.raises(ValueError):
        factory.create_driver(None)


def test_resolve_runtime_priority(monkeypatch):
    """resolve_runtime 与 create_driver 同规则（WORKFLOW_CONTROLLER_DRIVER > 显式 > 全局）：
    session 侧的 runtime 判定共用它，两边结论才能一致。
    """
    factory.set_config_runtime("claude_cli")
    assert factory.resolve_runtime(None) == "claude_cli"
    assert factory.resolve_runtime("opencode") == "opencode"
    assert factory.resolve_runtime("") == "claude_cli"
    monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "opencode")
    assert factory.resolve_runtime("claude_sdk") == "opencode"


def test_resolve_runtime_custom_default(monkeypatch):
    """default 供自带兜底的调用方（AuthRotator 的 self._runtime）复用同一
    优先级规则，不落回全局 _config_runtime。
    """
    monkeypatch.delenv("WORKFLOW_CONTROLLER_DRIVER", raising=False)
    factory.set_config_runtime("claude_sdk")
    assert factory.resolve_runtime(None, default="opencode") == "opencode"
    assert factory.resolve_runtime("claude_cli", default="opencode") == "claude_cli"
    monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "opencode")
    assert factory.resolve_runtime("claude_sdk", default="claude_cli") == "opencode"


def test_opencode_runtime_returns_opencode_driver():
    factory.set_config_runtime("opencode")
    assert isinstance(factory.create_driver(None), OpenCodeDriver)


def test_codex_cli_runtime_returns_codex_driver():
    factory.set_config_runtime("codex_cli")
    assert isinstance(factory.create_driver(None), CodexCLIDriver)


def test_codex_alias_is_rejected():
    with pytest.raises(ValueError, match="codex_cli"):
        factory.create_driver("codex")


def test_opencode_driver_accepts_reviewer_and_breather_kwargs():
    # reviewer_runner creates the driver with add_dirs=...; breather with
    # fork_session=True. OpenCodeDriver must accept these (parity with
    # ClaudeCLIDriver) or construction TypeErrors — which is what broke the
    # reviewer on the opencode runtime.
    d = factory.create_driver(
        "opencode",
        permission_mode="bypassPermissions",
        add_dirs=["/tmp/x"],
        fork_session=True,
    )
    assert isinstance(d, OpenCodeDriver)
