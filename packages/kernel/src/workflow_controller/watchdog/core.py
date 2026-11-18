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

"""Watchdog 检测规则 —— 事件库订阅者，命中条件发 fault.* 告警。

三条规则：
1. subagent 超时：start 记录开始时间，progress/stop 检查 elapsed，超阈值告警。
2. 文件不变：每个 subagent stop 后读 track_file 内容 hash，跟上一次 stop 时比，
   连续 N 次无变化 → 告警。不依赖 git，纯文件 hash 比较。
3. 关键词检测：subagent stop 后查 track_keyword_file 是否含 track_keyword。

设计要点：
- 不做后台扫描；规则只在 SUBAGENT_{START,PROGRESS,STOP} 事件回调里触发。
- 告警事件经 fault.py 编码进事件库（events 表持久化 + SSE 透传），
  前端按 fault. 前缀过滤。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from workflow_controller.session.events import Event, EventType
from workflow_controller.watchdog.fault import emit_fault_event
from workflow_controller.watchdog.state import Alert, CallRecord, Severity, make_alert_id

if TYPE_CHECKING:
    from datetime import datetime

    from workflow_controller.config.settings import WatchdogConfig
    from workflow_controller.event.event_bus import EventBus
    from workflow_controller.session.manager import SessionPool

log = logging.getLogger(__name__)


class DetectionRule:
    """检测规则订阅者基类：订阅事件总线，命中条件时发 fault.<rule> 告警。"""

    rule = ""

    def __init__(self, config: WatchdogConfig, bus: EventBus, pool: SessionPool) -> None:
        self._config = config
        self._bus = bus
        self._pool = pool

    @staticmethod
    def _hash_file(file_path: Path) -> str | None:
        """读文件内容算 md5 hash。文件不可读返回 None（视为"无变化"——文件丢了也算卡住）。"""
        try:
            return hashlib.md5(file_path.read_bytes(), usedforsecurity=False).hexdigest()
        except OSError:
            return None

    def subscribe(self) -> None:
        """向事件总线注册本规则的事件订阅（子类覆写，需保持实例方法签名）。"""
        raise NotImplementedError(f"{type(self).__name__}.subscribe() is not implemented")

    async def _emit_alert(self, alert: Alert) -> None:
        """发 fault 告警事件；失败只记日志，不影响后续回调。"""
        try:
            await emit_fault_event(self._bus, alert)
            log.warning("watchdog alert fired: %s", alert.message)
        except Exception:
            log.exception("watchdog failed to submit alert")

    def _resolve_file_path(self, session_id: str, file_name: str) -> Path | None:
        """相对 session cwd 解析追踪文件路径。"""
        session = self._pool.get(session_id)
        if session is None:
            return None
        cwd = session.config.cwd or "."
        return (Path(cwd) / file_name).resolve()


class SubagentTimeoutRule(DetectionRule):
    """规则 1：subagent 运行超过阈值 → fault.subagent_timeout。"""

    rule = "subagent_timeout"

    def __init__(self, config: WatchdogConfig, bus: EventBus, pool: SessionPool) -> None:
        super().__init__(config, bus, pool)
        # (session_id, agent_id) -> CallRecord
        self._active_calls: dict[tuple[str, str], CallRecord] = {}

    def subscribe(self) -> None:
        self._bus.observe(EventType.SUBAGENT_START, self.on_start)
        self._bus.observe(EventType.SUBAGENT_PROGRESS, self.on_progress)
        self._bus.observe(EventType.SUBAGENT_STOP, self.on_stop)

    async def on_start(self, event: Event) -> None:
        """建 CallRecord，记录开始时间。"""
        if not self._config.enabled:
            return
        data = event.data or {}
        agent_id = data.get("agent_id", "")
        if not agent_id:
            return
        self._active_calls[(event.session_id, agent_id)] = CallRecord(
            session_id=event.session_id,
            agent_id=agent_id,
            agent_type=data.get("agent_type", ""),
            started_at=event.timestamp,
        )
        log.debug(
            "watchdog track start: %s/%s for %s",
            data.get("agent_type", ""),
            agent_id[:8],
            event.session_id[:8],
        )

    async def on_progress(self, event: Event) -> None:
        """运行中超时检查（progress 不规律，仅机会性）。"""
        if not self._config.enabled:
            return
        data = event.data or {}
        agent_id = data.get("agent_id", "")
        if not agent_id:
            return
        rec = self._active_calls.get((event.session_id, agent_id))
        if rec is None:
            # 该 call 已 stop（rec 已清）→ poller 在 stop 之后才推的迟到尾部，
            # 不重建记录（否则 elapsed 永远≈0，测不到超时），直接忽略。
            return
        await self._check_timeout(rec, event.timestamp)

    async def on_stop(self, event: Event) -> None:
        """超时检查的后备路径：progress 错过的在 stop 时一定能查到。"""
        if not self._config.enabled:
            return
        data = event.data or {}
        agent_id = data.get("agent_id", "")
        if not agent_id:
            return
        rec = self._active_calls.pop((event.session_id, agent_id), None)
        if rec is not None:
            await self._check_timeout(rec, event.timestamp)

    async def _check_timeout(self, rec: CallRecord, now: datetime) -> None:
        threshold = self._config.subagent_timeout_seconds
        if threshold <= 0:
            return  # 0 = 关闭该规则
        if self.rule in rec.fired_alerts:
            return  # 同一 call 只发一次

        elapsed = int((now - rec.started_at).total_seconds())
        if elapsed < threshold:
            return

        alert = Alert(
            alert_id=make_alert_id(),
            rule=self.rule,
            severity=Severity.WARNING,
            session_id=rec.session_id,
            message=f"subagent '{rec.agent_type}' 运行超时，agent_id={rec.agent_id[:12]}",
            agent_id=rec.agent_id,
            agent_type=rec.agent_type,
            started_at=rec.started_at.isoformat(),
            elapsed_seconds=elapsed,
            threshold_seconds=threshold,
        )
        rec.fired_alerts.add(self.rule)
        await self._emit_alert(alert)


class FileStallRule(DetectionRule):
    """规则 2：连续 N 个 subagent 结束后追踪文件无变化 → fault.file_stall。"""

    rule = "file_stall"

    def __init__(self, config: WatchdogConfig, bus: EventBus, pool: SessionPool) -> None:
        super().__init__(config, bus, pool)
        # session_id -> 基线文件 hash（第一个 subagent start 前的文件内容）
        self._baseline_hashes: dict[str, str | None] = {}
        # session_id -> 上一次 subagent stop 时的文件 hash（用于连续比较）
        self._last_hashes: dict[str, str | None] = {}
        # session_id -> 连续未变化计数
        self._stall_counts: dict[str, int] = {}
        # session_id -> 本轮是否已告警（文件变化后重置）
        self._fired: dict[str, bool] = {}

    def subscribe(self) -> None:
        self._bus.observe(EventType.SUBAGENT_START, self.on_start)
        self._bus.observe(EventType.SUBAGENT_STOP, self.on_stop)

    async def on_start(self, event: Event) -> None:
        """第一个 subagent start 前记录追踪文件基线 hash（后续 start 不覆盖）。"""
        if not self._config.enabled:
            return
        data = event.data or {}
        if not data.get("agent_id"):
            return
        track_file = self._config.track_file
        if not track_file:
            return
        file_path = self._resolve_file_path(event.session_id, track_file)
        if file_path is not None:
            self._baseline_hashes.setdefault(event.session_id, self._hash_file(file_path))

    async def on_stop(self, event: Event) -> None:
        if not self._config.enabled:
            return
        data = event.data or {}
        if not data.get("agent_id"):
            return
        await self._check_stall(event.session_id, data.get("agent_type", ""))

    async def _check_stall(self, session_id: str, agent_type: str) -> None:
        """每次 stop 与上次 stop 的 hash 比较，相同则计数 +1，达到阈值告警。"""
        track_file = self._config.track_file
        threshold = self._config.file_stall_threshold
        if not track_file or threshold <= 0:
            return  # 未配置或关闭

        file_path = self._resolve_file_path(session_id, track_file)
        if file_path is None:
            return  # 找不到 session cwd，跳过
        current_hash = self._hash_file(file_path)

        # 第一次 stop 无上次 hash，用基线做比较
        prev_hash = self._last_hashes.get(session_id)
        if prev_hash is None:
            prev_hash = self._baseline_hashes.get(session_id)
        if prev_hash is not None and current_hash == prev_hash:
            self._stall_counts[session_id] = self._stall_counts.get(session_id, 0) + 1
        else:
            self._stall_counts[session_id] = 0
            self._fired[session_id] = False
        self._last_hashes[session_id] = current_hash

        count = self._stall_counts[session_id]
        if count >= threshold and not self._fired.get(session_id, False):
            alert = Alert(
                alert_id=make_alert_id(),
                rule=self.rule,
                severity=Severity.WARNING,
                session_id=session_id,
                message=f"连续 {count} 个 subagent 结束后 {track_file} 无变化",
                agent_type=agent_type,
                file_path=str(file_path),
                stall_count=count,
            )
            self._fired[session_id] = True
            await self._emit_alert(alert)


class KeywordRule(DetectionRule):
    """规则 3：subagent 结束后追踪文件中出现关键词 → fault.keyword_detected。"""

    rule = "keyword_detected"

    def __init__(self, config: WatchdogConfig, bus: EventBus, pool: SessionPool) -> None:
        super().__init__(config, bus, pool)
        # session_id -> 是否已告警（关键词消失后重置）
        self._fired: dict[str, bool] = {}

    def subscribe(self) -> None:
        self._bus.observe(EventType.SUBAGENT_STOP, self.on_stop)

    async def on_stop(self, event: Event) -> None:
        if not self._config.enabled:
            return
        data = event.data or {}
        if not data.get("agent_id"):
            return
        await self._check_keyword(event.session_id, data.get("agent_type", ""))

    async def _check_keyword(self, session_id: str, agent_type: str) -> None:
        keyword_file = self._config.track_keyword_file
        keyword = self._config.track_keyword
        if not keyword_file or not keyword:
            return  # 未配置或关闭

        file_path = self._resolve_file_path(session_id, keyword_file)
        if file_path is None:
            return
        try:
            content = file_path.read_text(errors="replace")
        except OSError:
            return  # 文件不存在，不告警

        found = keyword.lower() in content.lower()
        if found and not self._fired.get(session_id, False):
            alert = Alert(
                alert_id=make_alert_id(),
                rule=self.rule,
                severity=Severity.WARNING,
                session_id=session_id,
                message=f"在 {keyword_file} 中发现关键词 '{keyword}'",
                agent_type=agent_type,
                file_path=str(file_path),
                keyword=keyword,
            )
            self._fired[session_id] = True
            await self._emit_alert(alert)
        elif not found:
            self._fired[session_id] = False


class WatchdogCore:
    """watchdog 组合根：构造检测规则订阅者并在启动时接入事件总线。

    生命周期：orchestrator.start() 里构造 + start()；进程生命周期内常驻。
    配置热更新：每次回调现读 config（host.config.watchdog 直接引用，
    update_config 改的是同一对象，下一回调即生效）。
    """

    def __init__(
        self,
        config: WatchdogConfig,
        bus: EventBus,
        pool: SessionPool,
    ) -> None:
        self._config = config
        self._rules: list[DetectionRule] = [
            SubagentTimeoutRule(config, bus, pool),
            FileStallRule(config, bus, pool),
            KeywordRule(config, bus, pool),
        ]

    def start(self) -> None:
        """各规则接入事件总线。由 orchestrator.start() 调用。"""
        if not self._config.enabled:
            return
        for rule in self._rules:
            rule.subscribe()
        log.info("watchdog installed (subagent_timeout=%ds)", self._config.subagent_timeout_seconds)
