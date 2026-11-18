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

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from workflow_controller.core.auth_rotator import AuthMode, AuthRotator
from workflow_controller.core.auth_rotator.detector import LimitDetector
from workflow_controller.session.events import Event, EventType

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def rotator(tmp_path: Path) -> AuthRotator:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk\n", encoding="utf-8")
    return AuthRotator(state_path=tmp_path / "state.json", env_path=env_file)


def _signal_event(
    kind: str,
    *,
    scope: str | None = None,
    reset_at: str | None = None,
    utilization: float | None = None,
    source: str = "test",
) -> Event:
    """A RATE_LIMIT carrying a normalized limit_signal — the single
    contract every driver emits and LimitDetector consumes.
    """
    return Event.create(
        session_id="s1",
        iteration=1,
        event_type=EventType.RATE_LIMIT,
        data={
            "limit_signal": {
                "kind": kind,
                "scope": scope,
                "reset_at": reset_at,
                "utilization": utilization,
                "source": source,
            },
            "raw": {},
        },
    )


# --- rejected → on_rate_limit ------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_5h_calls_on_rate_limit(rotator: AuthRotator) -> None:
    detector = LimitDetector(rotator=rotator)
    reset = "2030-06-01T12:00:00+00:00"
    await getattr(detector, "_handle_signal")(
        _signal_event("rejected", scope="5h", reset_at=reset, source="sdk:regex:x"),
    )
    assert rotator.get_reset_at() == datetime.fromisoformat(reset)
    assert rotator.get_mode() == AuthMode.API


@pytest.mark.asyncio
async def test_rejected_weekly_calls_on_rate_limit_no_reset(rotator: AuthRotator) -> None:
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(_signal_event("rejected", scope="weekly"))
    # Weekly limit → API with NO auto-back schedule (reset_at stays None)
    assert rotator.get_mode() == AuthMode.API
    assert rotator.get_reset_at() is None


# --- auth_error → on_limit_detected ------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_calls_on_limit_detected() -> None:
    rotator = MagicMock()
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(
        _signal_event("auth_error", scope="auth_error", source="opencode:APIError:401"),
    )
    rotator.on_limit_detected.assert_called_once()
    match = rotator.on_limit_detected.call_args.args[0]
    assert match.scope == "auth_error"
    assert match.source == "opencode:APIError:401"
    assert rotator.on_limit_detected.call_args.kwargs["reset_at"] is None
    rotator.on_rate_limit.assert_not_called()


# --- warning → on_quota_warning (threshold-gated) ----------------------------


def _quota_cb(warnings: list[tuple]):
    return lambda *, utilization, scope, reset_at: warnings.append((utilization, scope, reset_at))


@pytest.mark.asyncio
async def test_warning_high_util_fires_quota_callback(rotator: AuthRotator) -> None:
    warnings: list[tuple] = []
    rotator.set_quota_warning_callback(_quota_cb(warnings))
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(
        _signal_event(
            "warning", scope="seven_day", utilization=0.76, reset_at="2030-06-01T12:00:00"
        ),
    )
    assert len(warnings) == 1
    util, scope, _ = warnings[0]
    assert util == pytest.approx(0.76)
    assert scope == "seven_day"


@pytest.mark.asyncio
async def test_warning_low_util_no_op(rotator: AuthRotator) -> None:
    warnings: list[tuple] = []
    rotator.set_quota_warning_callback(_quota_cb(warnings))
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(
        _signal_event("warning", scope="seven_day", utilization=0.5),
    )
    assert warnings == []


# --- allowed / unknown → no-op -----------------------------------------------


@pytest.mark.asyncio
async def test_allowed_is_noop(rotator: AuthRotator) -> None:
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(_signal_event("allowed", scope="five_hour"))
    assert rotator.get_mode() == AuthMode.API
    assert rotator.get_reset_at() is None


@pytest.mark.asyncio
async def test_unknown_kind_is_noop() -> None:
    rotator = MagicMock()
    detector = LimitDetector(rotator=rotator)
    await getattr(detector, "_handle_signal")(
        _signal_event("unknown", source="cli:rate_limit_event:throttled"),
    )
    rotator.on_rate_limit.assert_not_called()
    rotator.on_limit_detected.assert_not_called()
    rotator.on_quota_warning.assert_not_called()
