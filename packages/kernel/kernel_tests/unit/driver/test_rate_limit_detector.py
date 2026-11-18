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

"""Fixture-driven tests for the claude_cli rate_limit_event → LimitDetector
chain. Loads each fixture file in tests/fixtures/rate_limit_events/ (raw CLI
stream-json lines), parses it through _cli_line_to_events (the CLI driver
translates rate_limit_info → a normalized limit_signal), feeds the resulting
RATE_LIMIT to LimitDetector, and asserts the expected post-state on a
fresh AuthRotator.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from workflow_controller.core.auth_rotator import AuthMode, AuthRotator
from workflow_controller.core.auth_rotator.detector import LimitDetector

from tests.support import find_upward

if TYPE_CHECKING:
    from workflow_controller.session.events import Event

FIXTURES = (
    find_upward(Path(__file__).resolve().parent, "fixtures/rate_limit_events")
    / "fixtures"
    / "rate_limit_events"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fresh_rotator(tmp_path: Path, with_key: bool = True) -> AuthRotator:
    env_file = tmp_path / ".env"
    if with_key:
        env_file.write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    return AuthRotator(state_path=tmp_path / "state.json", env_path=env_file)


def _to_event(raw: dict) -> Event:
    """Convert a raw stream-json dict into an Event using the CLI driver's parser.

    rate_limit_event lines produce exactly one Event; pull it out of the list
    returned by _cli_line_to_events.
    """
    from workflow_controller.driver.claude_cli import _cli_line_to_events

    return _cli_line_to_events(json.dumps(raw), session_id="ouro-test", iteration=1)[0]


# --- allowed: do nothing ----------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_five_hour_no_op(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    detector = LimitDetector(rotator=rotator)
    raw = _load("allowed_five_hour.json")
    ev = _to_event(raw)
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    assert rotator.get_mode() == AuthMode.API
    assert rotator.get_reset_at() is None


# --- allowed_warning: low utilization → no-op; high → fire warn callback ---


@pytest.mark.asyncio
async def test_allowed_warning_low_util_no_op(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    warnings: list[tuple] = []
    rotator.set_quota_warning_callback(
        lambda *, utilization, scope, reset_at: warnings.append((utilization, scope, reset_at))
    )
    detector = LimitDetector(rotator=rotator)
    ev = _to_event(_load("allowed_warning_low_util.json"))
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    assert rotator.get_mode() == AuthMode.API
    assert warnings == []


@pytest.mark.asyncio
async def test_allowed_warning_high_util_fires_callback(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    warnings: list[tuple] = []
    rotator.set_quota_warning_callback(
        lambda *, utilization, scope, reset_at: warnings.append((utilization, scope, reset_at))
    )
    detector = LimitDetector(rotator=rotator)
    ev = _to_event(_load("allowed_warning_seven_day.json"))
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    # Mode unchanged
    assert rotator.get_mode() == AuthMode.API
    # Callback fired once with util=0.76, scope=seven_day
    assert len(warnings) == 1
    util, scope, reset_at = warnings[0]
    assert util == pytest.approx(0.76)
    assert scope == "seven_day"
    assert reset_at is not None


# --- rejected: switch to API ------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_five_hour_switches_with_reset(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    detector = LimitDetector(rotator=rotator)
    ev = _to_event(_load("rejected_five_hour.json"))
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    assert rotator.get_mode() == AuthMode.API
    # reset_at is the epoch from fixture: 1906545600 (2030-06-01 12:00:00 UTC)
    assert rotator.get_reset_at() == datetime.fromtimestamp(1906545600, tz=UTC)


@pytest.mark.asyncio
async def test_rejected_seven_day_switches_no_reset(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    detector = LimitDetector(rotator=rotator)
    ev = _to_event(_load("rejected_seven_day.json"))
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    assert rotator.get_mode() == AuthMode.API
    # Weekly limit → no auto-back, reset_at stays None
    assert rotator.get_reset_at() is None


# --- unknown status: log + no change ---------------------------------------


@pytest.mark.asyncio
async def test_unknown_status_does_not_switch(tmp_path: Path) -> None:
    rotator = _fresh_rotator(tmp_path)
    detector = LimitDetector(rotator=rotator)
    ev = _to_event(_load("unknown_status.json"))
    assert ev is not None
    await getattr(detector, "_handle_signal")(ev)
    assert rotator.get_mode() == AuthMode.API
