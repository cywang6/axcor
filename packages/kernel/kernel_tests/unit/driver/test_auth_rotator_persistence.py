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

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from workflow_controller.core.auth_rotator import AuthMode
from workflow_controller.core.auth_rotator.persistence import (
    APIKeyStore,
    AuthStateSnapshot,
    AuthStateStore,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_api_key_store_returns_none_when_no_env_file(tmp_path: Path) -> None:
    store = APIKeyStore(tmp_path / ".env")
    assert store.get() is None


def test_api_key_store_reads_anthropic_api_key_line(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-abcdef123\n", encoding="utf-8")
    store = APIKeyStore(env_file)
    assert store.get() == "sk-ant-abcdef123"


def test_api_key_store_ignores_other_keys_in_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-deepseek-xyz\nANTHROPIC_API_KEY=sk-ant-target\nFOO=bar\n",
        encoding="utf-8",
    )
    store = APIKeyStore(env_file)
    assert store.get() == "sk-ant-target"


def test_api_key_store_strips_quotes_and_whitespace(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('ANTHROPIC_API_KEY = "sk-ant-quoted"\n', encoding="utf-8")
    store = APIKeyStore(env_file)
    assert store.get() == "sk-ant-quoted"


def test_api_key_store_set_writes_file_creating_dir(tmp_path: Path) -> None:
    env_file = tmp_path / "deep" / "nested" / ".env"
    store = APIKeyStore(env_file)
    store.set("sk-ant-new-key")
    assert env_file.read_text(encoding="utf-8").strip() == "ANTHROPIC_API_KEY=sk-ant-new-key"


def test_api_key_store_set_preserves_other_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-deepseek-xyz\nFOO=bar\n", encoding="utf-8")
    store = APIKeyStore(env_file)
    store.set("sk-ant-new")
    content = env_file.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-deepseek-xyz" in content
    assert "FOO=bar" in content
    assert "ANTHROPIC_API_KEY=sk-ant-new" in content


def test_api_key_store_set_replaces_existing_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=old-key\n", encoding="utf-8")
    store = APIKeyStore(env_file)
    store.set("new-key")
    content = env_file.read_text(encoding="utf-8")
    assert "old-key" not in content
    assert "ANTHROPIC_API_KEY=new-key" in content


# ---------------------------------------------------------------------------
# base_url + model getters/setters (Anthropic-compatible 3rd-party endpoint)
# ---------------------------------------------------------------------------


def test_api_key_store_get_base_url_when_absent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk\n", encoding="utf-8")
    store = APIKeyStore(env_file)
    assert store.get_base_url() is None
    assert store.get_model() is None


def test_api_key_store_set_base_url_and_model(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    store = APIKeyStore(env_file)
    store.set("sk-key")
    store.set_base_url("https://api.deepseek.com/anthropic")
    store.set_model("deepseek-chat")
    assert store.get() == "sk-key"
    assert store.get_base_url() == "https://api.deepseek.com/anthropic"
    assert store.get_model() == "deepseek-chat"


def test_api_key_store_set_base_url_to_none_clears_line(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    store = APIKeyStore(env_file)
    store.set_base_url("https://example.com")
    assert store.get_base_url() == "https://example.com"
    store.set_base_url(None)
    assert store.get_base_url() is None
    content = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_BASE_URL" not in content


def test_api_key_store_url_and_model_preserve_other_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-deepseek\nFOO=bar\n",
        encoding="utf-8",
    )
    store = APIKeyStore(env_file)
    store.set("sk-ant")
    store.set_base_url("https://api.deepseek.com/anthropic")
    store.set_model("deepseek-chat")
    content = env_file.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-deepseek" in content
    assert "FOO=bar" in content
    assert "ANTHROPIC_API_KEY=sk-ant" in content
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in content
    assert "ANTHROPIC_MODEL=deepseek-chat" in content


# ---------------------------------------------------------------------------
# AuthStateStore tests (Task 3)
# ---------------------------------------------------------------------------


def test_auth_state_store_returns_default_when_missing(tmp_path: Path) -> None:
    store = AuthStateStore(tmp_path / "auth_state.json")
    snapshot = store.load()
    assert snapshot.mode == AuthMode.API
    assert snapshot.reset_at is None
    assert snapshot.last_limit_event is None


def test_auth_state_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "auth_state.json"
    store = AuthStateStore(path)
    snap = AuthStateSnapshot(
        mode=AuthMode.API,
        reset_at=datetime(2026, 5, 25, 18, 30, tzinfo=UTC),
        last_limit_event={
            "detected_at": "2026-05-25T13:30:12+00:00",
            "scope": "5h",
            "source": "with_reset_time",
            "raw_message": "5-hour usage limit reached. Try again at 18:30",
        },
    )
    store.save(snap)
    loaded = store.load()
    assert loaded.mode == AuthMode.API
    assert loaded.reset_at == snap.reset_at
    assert loaded.last_limit_event == snap.last_limit_event


def test_auth_state_store_corrupted_file_returns_default(tmp_path: Path) -> None:
    path = tmp_path / "auth_state.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = AuthStateStore(path)
    snapshot = store.load()
    assert snapshot.mode == AuthMode.API
    assert snapshot.reset_at is None


def test_auth_state_store_unknown_mode_falls_back_to_api(tmp_path: Path) -> None:
    path = tmp_path / "auth_state.json"
    path.write_text('{"mode": "WAT", "reset_at": null}', encoding="utf-8")
    store = AuthStateStore(path)
    snapshot = store.load()
    assert snapshot.mode == AuthMode.API


def test_auth_state_store_provider_runtime_roundtrip(tmp_path: Path) -> None:
    """AuthProvider.runtime 随 auth_state.json 持久化——健康检查剥前缀依赖它。"""
    from workflow_controller.core.auth_rotator.persistence import (
        AuthProvider,
        AuthProviderType,
        KeyStatus,
    )

    path = tmp_path / "auth_state.json"
    store = AuthStateStore(path)
    snap = AuthStateSnapshot(
        mode=AuthMode.API,
        providers=[
            AuthProvider(
                provider_type=AuthProviderType.API_KEY,
                key="sk-test",
                model="zhipuai-coding-plan/glm-5.2",
                runtime="opencode",
                status=KeyStatus.ACTIVE,
            )
        ],
    )
    store.save(snap)
    loaded = store.load()
    assert loaded.providers[0].runtime == "opencode"


def test_auth_state_store_validation_roundtrip(tmp_path: Path) -> None:
    from workflow_controller.core.auth_rotator import ProviderValidation, ValidationStatus
    from workflow_controller.core.auth_rotator.persistence import AuthProvider, AuthProviderType

    state = AuthStateStore(tmp_path / "state.json")
    checked_at = datetime.now(tz=UTC)
    provider = AuthProvider(
        provider_type=AuthProviderType.API_KEY,
        key="sk-test",
        worker_validation=ProviderValidation(
            capability="worker/opencode",
            status=ValidationStatus.VALID,
            fingerprint="fingerprint",
            checked_at=checked_at,
        ),
        reviewer_validation=ProviderValidation(
            capability="anthropic_http",
            status=ValidationStatus.INVALID,
            fingerprint="fingerprint",
            checked_at=checked_at,
            error="bad route",
        ),
    )
    state.save(AuthStateSnapshot(providers=[provider]))

    loaded = state.load().providers[0]
    assert loaded.worker_validation.status == ValidationStatus.VALID
    assert loaded.worker_validation.fingerprint == "fingerprint"
    assert loaded.reviewer_validation.status == ValidationStatus.INVALID
    assert loaded.reviewer_validation.error == "bad route"
