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

"""Watchdog 数据结构。

Alert 是 fault.* 告警事件的 payload；CallRecord 是超时规则的内存态调用记录
（stop 即清），其余规则字段随规则类扩展。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Alert:
    """一条 watchdog 告警。

    序列化进 fault.<rule> 告警事件的 data 字段；前端按 fault. 前缀过滤后
    直接消费 to_dict() 结果展示。
    """

    alert_id: str

    rule: str  # "subagent_timeout" | (未来: task_not_converging 等)

    severity: Severity

    session_id: str

    message: str

    # subagent 超时规则专用字段
    agent_id: str = ""

    agent_type: str = ""

    started_at: str = ""  # ISO 格式

    elapsed_seconds: int = 0

    threshold_seconds: int = 0

    # 文件不变规则专用字段
    file_path: str = ""  # 哪个文件卡住了

    stall_count: int = 0  # 连续未变化次数

    # 关键词检测规则专用字段
    keyword: str = ""  # 检测到的关键词

    timestamp: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Alert:
        return Alert(
            alert_id=d.get("alert_id", ""),
            rule=d.get("rule", ""),
            severity=Severity(d.get("severity", "warning")),
            session_id=d.get("session_id", ""),
            message=d.get("message", ""),
            agent_id=d.get("agent_id", ""),
            agent_type=d.get("agent_type", ""),
            started_at=d.get("started_at", ""),
            elapsed_seconds=int(d.get("elapsed_seconds", 0)),
            threshold_seconds=int(d.get("threshold_seconds", 0)),
            file_path=d.get("file_path", ""),
            stall_count=int(d.get("stall_count", 0)),
            keyword=d.get("keyword", ""),
            timestamp=d.get("timestamp", datetime.now(tz=UTC).isoformat()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "rule": self.rule,
            "severity": self.severity.value,
            "session_id": self.session_id,
            "message": self.message,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "started_at": self.started_at,
            "elapsed_seconds": self.elapsed_seconds,
            "threshold_seconds": self.threshold_seconds,
            "file_path": self.file_path,
            "stall_count": self.stall_count,
            "keyword": self.keyword,
            "timestamp": self.timestamp,
        }


@dataclass
class CallRecord:
    """一个在飞 subagent 的调用记录（内存态，stop 即清）。

    start 事件的 timestamp 直接缓存，不在 SubagentTracker 里
    加字段——后者只存 {"agent_type"}，watchdog 独立维护。
    """

    session_id: str
    agent_id: str
    agent_type: str
    started_at: datetime
    # 已对该 call 发过的告警 alert_id 集合，避免 progress 事件反复触发同一规则
    fired_alerts: set[str] = field(default_factory=set)


def make_alert_id() -> str:
    return f"alert_{uuid4().hex[:12]}"
