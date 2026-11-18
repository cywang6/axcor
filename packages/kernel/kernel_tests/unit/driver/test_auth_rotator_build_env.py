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

from typing import TYPE_CHECKING

import pytest

from workflow_controller.core.auth_rotator import AuthMode, AuthRotator
from workflow_controller.core.auth_rotator.persistence import (
    AuthProvider,
    AuthProviderType,
    KeyStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def rotator(tmp_path: Path) -> AuthRotator:
    return AuthRotator(
        state_path=tmp_path / "auth_state.json",
        env_path=tmp_path / ".env",
    )


def _state_file_rotator(tmp_path: Path) -> AuthRotator:
    """以 state.json 为状态文件的独立 AuthRotator（与 fixture 的文件名区分）。"""
    return AuthRotator(
        state_path=tmp_path / "state.json",
        env_path=tmp_path / ".env",
    )


def _append_codex_api_key_provider(rotator: AuthRotator, **fields: object) -> None:
    """向 rotator 追加一个 codex_cli 运行时的 API key provider（默认 key=secret）。"""
    payload = dict(
        provider_type=AuthProviderType.API_KEY,
        status=KeyStatus.UNUSED,
        key="secret",
        runtime="codex_cli",
    )
    payload.update(fields)
    getattr(rotator, "_providers").append(AuthProvider(**payload))


def test_default_mode_is_api(rotator: AuthRotator) -> None:
    assert rotator.get_mode() == AuthMode.API


def test_build_env_preserves_non_auth_keys(rotator: AuthRotator) -> None:
    out = rotator.build_env({"DEEPSEEK_API_KEY": "deepseek-untouched", "FOO": "bar"})
    assert out["DEEPSEEK_API_KEY"] == "deepseek-untouched"
    assert out["FOO"] == "bar"


def test_build_env_does_not_mutate_input(rotator: AuthRotator) -> None:
    base = {"PATH": "/x", "ANTHROPIC_API_KEY": "x"}
    rotator.build_env(base)
    assert base == {"PATH": "/x", "ANTHROPIC_API_KEY": "x"}


def test_set_api_key_persists_key_to_env_file(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    assert rotator.has_key() is False
    rotator.set_api_key("sk-ant-new")
    assert rotator.has_key() is True
    assert getattr(rotator, "_key_store").get() == "sk-ant-new"


def test_set_keys_with_per_key_model_and_url(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    rotator.set_keys(
        [
            {
                "key": "sk-glm",
                "model": "glm-5.2",
                "base_url": "https://open.bigmodel.cn/api/anthropic",
            },
            {"key": "sk-ant", "model": "claude-sonnet-4-6"},
        ]
    )
    keys = rotator.get_api_keys_for_ui()
    assert len(keys) == 2
    assert keys[0]["key"] == "sk-glm"
    assert keys[0]["model"] == "glm-5.2"
    assert keys[0]["base_url"] == "https://open.bigmodel.cn/api/anthropic"
    assert keys[1]["key"] == "sk-ant"
    assert keys[1]["model"] == "claude-sonnet-4-6"


def test_set_keys_empty_clears_all(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    rotator.set_keys([{"key": "sk-default", "model": "m1"}])
    assert rotator.get_api_keys_for_ui()

    rotator.set_keys([])

    assert rotator.get_api_keys_for_ui() == []
    assert getattr(rotator, "_key_store").get() is None
    out = rotator.build_env({"PATH": "/x"})
    assert "ANTHROPIC_API_KEY" not in out
    assert "ANTHROPIC_MODEL" not in out


def test_build_env_uses_per_key_base_url(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    rotator.set_keys(
        [
            {
                "key": "sk-glm",
                "model": "glm-5.2",
                "base_url": "https://open.bigmodel.cn/api/anthropic",
            },
        ]
    )
    out = rotator.build_env({"PATH": "/x"})
    assert out["ANTHROPIC_API_KEY"] == "sk-glm"
    assert out["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "sk-glm"
    assert out["ANTHROPIC_MODEL"] == "glm-5.2"


def test_build_env_native_key_no_base_url(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    rotator.set_keys([{"key": "sk-ant-native", "model": "claude-sonnet-4-6"}])
    out = rotator.build_env({"PATH": "/x"})
    assert out["ANTHROPIC_API_KEY"] == "sk-ant-native"
    assert out["ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
    assert "ANTHROPIC_BASE_URL" not in out
    assert "ANTHROPIC_AUTH_TOKEN" not in out


def test_build_env_falls_back_to_defaults(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    # Set global defaults
    rotator.set_global_settings(
        default_model="claude-sonnet-4-6",
        default_url="https://api.anthropic.com",
    )
    # Key without model or base_url
    rotator.set_keys([{"key": "sk-test"}])
    out = rotator.build_env({"PATH": "/x"})
    assert out["ANTHROPIC_API_KEY"] == "sk-test"
    assert out["ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
    assert out["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "sk-test"


def test_build_env_per_key_overrides_defaults(tmp_path: Path) -> None:
    rotator = _state_file_rotator(tmp_path)
    # Set global defaults
    rotator.set_global_settings(
        default_model="claude-sonnet-4-6",
        default_url="https://api.anthropic.com",
    )
    # Key with its own model and base_url
    rotator.set_keys(
        [
            {
                "key": "sk-glm",
                "model": "glm-5.2",
                "base_url": "https://open.bigmodel.cn/api/anthropic",
            }
        ]
    )
    out = rotator.build_env({"PATH": "/x"})
    assert out["ANTHROPIC_API_KEY"] == "sk-glm"
    assert out["ANTHROPIC_MODEL"] == "glm-5.2"
    assert out["ANTHROPIC_BASE_URL"] == "https://open.bigmodel.cn/api/anthropic"
    assert out["ANTHROPIC_AUTH_TOKEN"] == "sk-glm"


def test_env_for_driver_keeps_generic_protocol_for_codex_provider(
    rotator: AuthRotator,
) -> None:
    """通用 provider 环境由 Codex Driver 在查询内完成专用投影。"""
    _append_codex_api_key_provider(rotator, model="gpt-test", base_url="https://example.test/v1")

    env = rotator.env_for_driver(
        {
            "ANTHROPIC_BASE_URL": "https://host.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "host-secret",
        }
    )

    assert env == {
        "ANTHROPIC_API_KEY": "secret",
        "ANTHROPIC_AUTH_TOKEN": "secret",
        "ANTHROPIC_BASE_URL": "https://example.test/v1",
        "ANTHROPIC_MODEL": "gpt-test",
    }


def test_env_for_driver_does_not_apply_default_url_to_codex_provider(
    rotator: AuthRotator,
) -> None:
    rotator.set_global_settings(
        default_model="gpt-test",
        default_url="https://proxy.example/v1",
    )
    _append_codex_api_key_provider(rotator, model="gpt-test")

    env = rotator.env_for_driver(
        {
            "ANTHROPIC_BASE_URL": "https://host.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "host-secret",
        }
    )

    assert env == {
        "ANTHROPIC_API_KEY": "secret",
        "ANTHROPIC_MODEL": "gpt-test",
    }


def test_env_for_driver_does_not_apply_default_model_to_codex_provider(
    rotator: AuthRotator,
) -> None:
    rotator.set_global_settings(default_model="claude-sonnet-4-6")
    _append_codex_api_key_provider(rotator, base_url="https://example.test/v1")

    env = rotator.env_for_driver({"ANTHROPIC_MODEL": "claude-sonnet-4"})

    assert "ANTHROPIC_MODEL" not in env
