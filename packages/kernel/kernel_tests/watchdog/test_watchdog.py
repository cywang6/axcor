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

"""Watchdog 检测规则订阅者与 fault.* 告警事件测试。"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from workflow_controller.api.watchdog_api import WatchdogAPI
from workflow_controller.config.settings import WatchdogConfig
from workflow_controller.session.events import Event, EventType
from workflow_controller.watchdog.core import (
    FileStallRule,
    KeywordRule,
    SubagentTimeoutRule,
    WatchdogCore,
)
from workflow_controller.watchdog.fault import fault_event_type
from workflow_controller.watchdog.state import Alert, Severity, make_alert_id


class _FakeBus:
    """记录告警发射，并按事件类型把事件分派回已注册的订阅者。"""

    def __init__(self) -> None:
        self.emitted: list[Event] = []
        self.observers: dict[str, list[Any]] = defaultdict(list)

    def observe(self, event_type: Any, handler: Any) -> None:
        self.observers[getattr(event_type, "value", event_type)].append(handler)

    async def emit(self, event: Event) -> None:
        self.emitted.append(event)

    async def dispatch(self, event: Event) -> None:
        for handler in self.observers[event.event_type.value]:
            await handler(event)


class _FakeSession:
    def __init__(self, cwd: str = ""):
        self.config = MagicMock()
        self.config.cwd = cwd


def _make_rule(rule_cls: Any, cfg: WatchdogConfig, *, cwd: str = "") -> _FakeBus:
    """构造规则订阅者并接入假总线，返回总线。"""
    bus = _FakeBus()
    pool = MagicMock()
    pool.get.return_value = _FakeSession(cwd)
    rule = rule_cls(cfg, bus, pool)
    rule.subscribe()
    return bus


def _subagent_event(
    session_id: str, phase: str, agent_id: str = "ag_1", agent_type: str = "tester"
) -> Event:
    return Event.create(
        session_id=session_id,
        iteration=0,
        event_type=EventType[f"SUBAGENT_{phase.upper()}"],
        data={"agent_id": agent_id, "agent_type": agent_type},
    )


# ── 组合根 ─────────────────────────────────────────────────────────────


class TestWatchdogCore:
    @staticmethod
    def _started_bus(cfg: WatchdogConfig) -> _FakeBus:
        bus = _FakeBus()
        WatchdogCore(cfg, bus, MagicMock()).start()
        return bus

    def test_start_subscribes_all_rules(self):
        bus = self._started_bus(WatchdogConfig())
        assert set(bus.observers) == {"subagent_start", "subagent_progress", "subagent_stop"}

    def test_start_noop_when_disabled(self):
        assert not self._started_bus(WatchdogConfig(enabled=False)).observers


# ── 规则 1：subagent 超时 ─────────────────────────────────────────────


class TestSubagentTimeout:
    async def test_alert_via_progress(self):
        bus = _make_rule(SubagentTimeoutRule, WatchdogConfig(subagent_timeout_seconds=60))
        now = datetime.now(tz=UTC)
        start = _subagent_event("ses", "start", agent_type="kernel-developer")
        start.timestamp = now - timedelta(seconds=70)
        await bus.dispatch(start)
        await bus.dispatch(_subagent_event("ses", "progress", agent_type="kernel-developer"))
        assert [e.event_type for e in bus.emitted] == [fault_event_type("subagent_timeout")]
        alert = Alert.from_dict(bus.emitted[0].data)
        assert alert.severity == Severity.WARNING
        assert alert.agent_type == "kernel-developer"
        assert alert.elapsed_seconds >= 70
        assert alert.threshold_seconds == 60

    async def test_no_alert_below_threshold(self):
        bus = _make_rule(SubagentTimeoutRule, WatchdogConfig(subagent_timeout_seconds=60))
        start = _subagent_event("ses", "start")
        start.timestamp = datetime.now(tz=UTC) - timedelta(seconds=30)
        await bus.dispatch(start)
        await bus.dispatch(_subagent_event("ses", "progress"))
        assert not bus.emitted

    async def test_alert_via_stop_and_dedup(self):
        """stop 后备触发；同 call 后续事件不再重复告警。"""
        bus = _make_rule(SubagentTimeoutRule, WatchdogConfig(subagent_timeout_seconds=60))
        start = _subagent_event("ses", "start")
        start.timestamp = datetime.now(tz=UTC) - timedelta(seconds=65)
        await bus.dispatch(start)
        await bus.dispatch(_subagent_event("ses", "stop"))
        await bus.dispatch(_subagent_event("ses", "progress"))  # stop 后迟到尾部
        assert len(bus.emitted) == 1

    async def test_threshold_zero_disabled(self):
        bus = _make_rule(SubagentTimeoutRule, WatchdogConfig(subagent_timeout_seconds=0))
        start = _subagent_event("ses", "start")
        start.timestamp = datetime.now(tz=UTC) - timedelta(seconds=999)
        await bus.dispatch(start)
        await bus.dispatch(_subagent_event("ses", "stop"))
        assert not bus.emitted


# ── 规则 2：文件不变 ──────────────────────────────────────────────────


class TestFileStall:
    async def test_alert_at_threshold_refires_after_change(self, tmp_path):
        track = tmp_path / "STATE.md"
        track.write_text("v1")
        bus = _make_rule(
            FileStallRule,
            WatchdogConfig(track_file="STATE.md", file_stall_threshold=2),
            cwd=str(tmp_path),
        )
        await bus.dispatch(_subagent_event("ses", "start"))  # 记基线 hash
        for _ in range(3):
            await bus.dispatch(_subagent_event("ses", "stop"))
        assert len(bus.emitted) == 1
        alert = Alert.from_dict(bus.emitted[0].data)
        assert alert.rule == "file_stall"
        assert alert.stall_count == 2
        await bus.dispatch(_subagent_event("ses", "stop"))  # 仍无变化 → 去重
        assert len(bus.emitted) == 1
        track.write_text("v2")
        await bus.dispatch(_subagent_event("ses", "stop"))  # 变化 → 计数清零
        for _ in range(2):
            await bus.dispatch(_subagent_event("ses", "stop"))
        assert len(bus.emitted) == 2  # 重新累计到阈值再告警

    async def test_no_alert_when_unconfigured_or_disabled(self, tmp_path):
        track = tmp_path / "STATE.md"
        track.write_text("v1")
        for cfg in (
            WatchdogConfig(track_file="", file_stall_threshold=3),
            WatchdogConfig(track_file="STATE.md", file_stall_threshold=0),
        ):
            bus = _make_rule(FileStallRule, cfg, cwd=str(tmp_path))
            for _ in range(5):
                await bus.dispatch(_subagent_event("ses", "stop"))
            assert not bus.emitted


# ── 规则 3：关键词检测 ────────────────────────────────────────────────


class TestKeywordDetection:
    async def test_alert_and_dedup_until_keyword_gone(self, tmp_path):
        f = tmp_path / "DESIGN.md"
        f.write_text("MEMBASE")  # 大小写不敏感
        bus = _make_rule(
            KeywordRule,
            WatchdogConfig(track_keyword_file="DESIGN.md", track_keyword="membase"),
            cwd=str(tmp_path),
        )
        await bus.dispatch(_subagent_event("ses", "stop"))
        assert len(bus.emitted) == 1
        alert = Alert.from_dict(bus.emitted[0].data)
        assert alert.rule == "keyword_detected"
        assert alert.keyword == "membase"
        await bus.dispatch(_subagent_event("ses", "stop"))  # 仍命中 → 去重
        assert len(bus.emitted) == 1
        f.write_text("regbase")  # 关键词消失 → 重置
        await bus.dispatch(_subagent_event("ses", "stop"))
        f.write_text("membase again")  # 再次命中 → 再告警
        await bus.dispatch(_subagent_event("ses", "stop"))
        assert len(bus.emitted) == 2

    async def test_no_alert_when_not_found_file_missing_or_unconfigured(self, tmp_path):
        (tmp_path / "DESIGN.md").write_text("regbase")
        for cfg in (
            WatchdogConfig(track_keyword_file="DESIGN.md", track_keyword="membase"),
            WatchdogConfig(track_keyword_file="no_such.md", track_keyword="membase"),
            WatchdogConfig(track_keyword_file="DESIGN.md", track_keyword=""),
        ):
            bus = _make_rule(KeywordRule, cfg, cwd=str(tmp_path))
            await bus.dispatch(_subagent_event("ses", "stop"))
            assert not bus.emitted


# ── 查询面：fault.* 解码 ──────────────────────────────────────────────


class TestWatchdogAPI:
    @staticmethod
    def test_list_alerts_decodes_fault_events_only():
        alert = Alert(
            alert_id=make_alert_id(),
            rule="file_stall",
            severity=Severity.WARNING,
            session_id="ses_x",
            message="test",
            stall_count=3,
        )
        fault_ev = Event.create(
            session_id="ses_x",
            iteration=0,
            event_type=fault_event_type("file_stall"),
            data=alert.to_dict(),
        )
        other_ev = Event.create(
            session_id="ses_x", iteration=0, event_type=EventType.LOG_MANUAL, data={"prompt": "hi"}
        )
        router = MagicMock()
        router.get_events_sync.return_value = [fault_ev, other_ev]
        assert WatchdogAPI(router).list_alerts("ses_x") == [alert.to_dict()]

    @staticmethod
    def test_list_alerts_skips_malformed_fault_payload():
        bad = Event.create(
            session_id="ses_x",
            iteration=0,
            event_type=fault_event_type("subagent_timeout"),
            data={"severity": 5},
        )
        router = MagicMock()
        router.get_events_sync.return_value = [bad]
        assert WatchdogAPI(router).list_alerts("ses_x") == []
