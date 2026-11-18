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

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from workflow_controller.api.auth_api import AuthAPI, ProviderInUseError
from workflow_controller.config.settings import SessionConfig, SessionReviewerConfig
from workflow_controller.core.auth_rotator import AuthRotator
from workflow_controller.core.auth_rotator.settings import GlobalSettings
from workflow_controller.session.session import Session

if TYPE_CHECKING:
    from pathlib import Path


def _rotator(tmp_path: Path) -> AuthRotator:
    GlobalSettings(
        api_keys=[
            {"key": "sk-old", "model": "old", "base_url": "https://old.test"},
            {
                "key": "sk-keep",
                "model": "keep",
                "base_url": "https://keep.test",
                "runtime": "claude_cli",
            },
        ]
    ).save(tmp_path / "settings.conf")
    return AuthRotator(state_path=tmp_path / "auth_state.json", env_path=tmp_path / ".env")


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_delete_provider_does_not_validate_remaining_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deleted_index: int,
) -> None:
    rotator = _rotator(tmp_path)

    async def fail_probe(*args, **kwargs):
        raise AssertionError("deletion must not probe another provider")

    monkeypatch.setattr(rotator, "validate_provider", fail_probe)
    assert rotator.delete_provider(deleted_index)
    remaining = "sk-keep" if deleted_index == 0 else "sk-old"
    assert [entry["key"] for entry in GlobalSettings.load(tmp_path / "settings.conf").api_keys] == [
        remaining
    ]
    assert [provider.key for provider in rotator.get_providers()] == [remaining]
    assert rotator.get_active_provider_index() == 0


def test_delete_refuses_stale_config_index(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    settings = GlobalSettings.load(tmp_path / "settings.conf")
    settings.api_keys[0]["model"] = "changed-elsewhere"
    settings.save(tmp_path / "settings.conf")

    assert not rotator.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_refuses_any_session_index_that_would_move(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    session = Session(
        config=SessionConfig(
            name="retained",
            provider_index=1,
            reviewer=SessionReviewerConfig(provider_index=0),
        )
    )
    pool = SimpleNamespace(list_sessions=lambda: [session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(0)
    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(1)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_allows_unreferenced_later_provider(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="retained", provider_index=0))
    pool = SimpleNamespace(list_sessions=lambda: [session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(1)
    assert [provider.key for provider in rotator.get_providers()] == ["sk-old"]


async def test_delete_ignores_out_of_range_session_index(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="stale", provider_index=5))
    pool = SimpleNamespace(list_sessions=lambda: [session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [provider.key for provider in rotator.get_providers()] == ["sk-keep"]


async def test_delete_refuses_persisted_session_outside_pool(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    pool = SimpleNamespace(list_sessions=lambda: [])
    roster = [
        {
            "id": "ghost-1",
            "config_json": json.dumps(
                {
                    "name": "ghost",
                    "provider_index": 1,
                    "reviewer": {"provider_index": 0},
                }
            ),
        }
    ]
    event_router = SimpleNamespace(list_sessions_sync=lambda: roster)
    api = AuthAPI(
        SimpleNamespace(
            auth_rotator=rotator,
            pool=pool,
            event_router=event_router,
        )
    )

    with pytest.raises(ProviderInUseError, match="ghost-1"):
        await api.delete_provider(0)
    with pytest.raises(ProviderInUseError, match="ghost-1"):
        await api.delete_provider(1)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2
