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

"""End-to-end: driver translates an upstream limit/auth error into a
normalized RATE_LIMIT → LimitDetector consumes it → AuthRotator
rotates the key → the next spawn env contains the new key.

Exercises the full unified-signal chain (driver translation + single-path
detector + rotator), replacing the old three-branch detector probes.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from workflow_controller.core.auth_rotator import AuthMode, AuthRotator
from workflow_controller.core.auth_rotator.detector import LimitDetector
from workflow_controller.event.event_bus import EventBus
from workflow_controller.event.type_registry import EventTypeRegistry
from workflow_controller.store.event_router import EventStoreRouter

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_sdk_5h_signal_to_rotator_to_spawn_env_e2e(tmp_path: Path) -> None:
    # 1) Set up rotator with a single valid key
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-e2e-key\n", encoding="utf-8")
    rotator = AuthRotator(state_path=tmp_path / "state.json", env_path=env_file)
    assert rotator.get_mode() == AuthMode.API

    # 2) Wire detector to a bus
    store = EventStoreRouter(default_project_dir=tmp_path)
    bus = EventBus(store_router=store, type_registry=EventTypeRegistry())
    bus.start()
    LimitDetector(rotator=rotator, bus=bus)

    # 3) Simulate the SDK driver translating a 5h-limit exception into a
    #    normalized RATE_LIMIT (what _sdk_limit_events would emit).
    #    The detector reads limit_signal.kind=rejected → on_rate_limit.
    from workflow_controller.driver.claude_sdk import _sdk_limit_events

    for ev in _sdk_limit_events(
        "s1",
        1,
        RuntimeError("5-hour usage limit reached. Try again at 18:30"),
    ):
        await bus.emit(ev)

    await asyncio.sleep(0.05)

    # 4) Single key → mark EXHAUSTED, no rotation possible, mode stays API,
    #    and build_env still returns the (exhausted) key so the next spawn has it.
    assert rotator.get_mode() == AuthMode.API
    assert rotator.get_providers()[0].status.value == "exhausted"
    out_env = rotator.build_env({"PATH": "/usr/bin"})
    assert out_env["ANTHROPIC_API_KEY"] == "sk-ant-e2e-key"
    assert out_env["PATH"] == "/usr/bin"

    await bus.stop()


@pytest.mark.asyncio
async def test_opencode_401_signal_rotates_to_next_key_e2e(tmp_path: Path) -> None:
    """opencode 401 (error dict + statusCode) → driver translates to a
    RATE_LIMIT (auth_error) → detector → rotator invalidates only the active
    worker capability and rotates to the next key.

    This is the core of opencode multi-key alignment: the opencode driver owns
    the statusCode→signal translation (unit-tested in test_opencode.py), and
    the detector consumes the resulting signal just like every other driver.
    """
    from workflow_controller.core.auth_rotator.persistence import KeyStatus, ValidationStatus
    from workflow_controller.driver.opencode import _opencode_line_to_event

    # 2-key rotator via settings.conf: [bad, good]
    (tmp_path / "settings.conf").write_text(
        "api_keys:\n  - sk-bad-key\n  - sk-good-key\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    rotator = AuthRotator(
        state_path=tmp_path / "auth_state.json",
        env_path=tmp_path / ".env",
    )
    assert rotator.get_provider_count() == 2
    assert rotator.get_active_provider_index() == 0  # bad key active

    store = EventStoreRouter(default_project_dir=tmp_path)
    bus = EventBus(store_router=store, type_registry=EventTypeRegistry())
    bus.start()
    LimitDetector(rotator=rotator, bus=bus)

    # opencode-shaped 401 error line → driver parser yields
    # [RATE_LIMIT(auth_error), ERROR]; emit both (the ERROR is just
    # observability here, the signal drives the rotation).
    line = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_x",
            "error": {"name": "APIError", "data": {"statusCode": 401, "message": "Unauthorized"}},
        }
    )
    for ev in _opencode_line_to_event(line, "s1", 1):
        await bus.emit(ev)
    await asyncio.sleep(0.05)

    # Rotated to good key. The bad key remains quota-available for Reviewer,
    # while only its Worker validation is invalidated.
    assert rotator.get_active_provider_index() == 1
    assert rotator.get_providers()[0].status == KeyStatus.UNUSED
    assert rotator.get_providers()[0].worker_validation.status == ValidationStatus.INVALID
    assert rotator.get_providers()[0].reviewer_validation.status == ValidationStatus.UNTESTED
    env = rotator.build_env({})
    assert env["ANTHROPIC_API_KEY"] == "sk-good-key"

    await bus.stop()
