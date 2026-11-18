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

"""fake_driver 插件：注册 fake@1 run 驱动工厂，供无外部运行时的验收使用。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.run_driver import (
    ControlNotification,
    EmitEvent,
    FinishOutcome,
    FinishRun,
    RunAction,
)
from workflow_controller.plugin.sdk import Plugin

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from workflow_controller.core.runs import RunRecord
    from workflow_controller.plugin.run_driver import RunNotification, RunSessionInfo, RunWorld

# 驱动格式串：工厂注册键与 run 账面 format 列共用
FAKE_FORMAT = "fake@1"

# 驱动留痕事件类型：验收据此确认决策循环真的跑过
FAKE_PROGRESS_EVENT = "fake_progress"

# 恢复实例留痕事件类型：验收据此确认 run 确实经恢复链续跑
FAKE_RESUMED_EVENT = "fake_resumed"

# run 参数键与行为取值：fail/resumable 供失败与崩溃恢复验收选择，缺省完成
BEHAVIOR_PARAM = "behavior"
FAIL_BEHAVIOR = "fail"
RESUMABLE_BEHAVIOR = "resumable"

_BEHAVIORS = frozenset({"complete", FAIL_BEHAVIOR, RESUMABLE_BEHAVIOR})


def resolve_behavior(params: Mapping[str, str]) -> str:
    """解析 run 参数中的行为选择，非法值显式报错。"""
    behavior = params.get(BEHAVIOR_PARAM, "complete")
    if behavior not in _BEHAVIORS:
        raise ValueError(f"未知 fake 驱动行为：{behavior}（可选 {sorted(_BEHAVIORS)}）")
    return behavior


class FakeRunDriver:
    """确定性 run 驱动：按验收行为留痕并判定终态，不启动任何成员。"""

    def __init__(self, behavior: str, restored: bool) -> None:
        self._behavior = behavior
        self._restored = restored

    @staticmethod
    def on_control(control: ControlNotification) -> list[RunAction] | None:
        return None

    @staticmethod
    def snapshot() -> dict[str, Any]:
        # 行为是 run 参数的纯函数，恢复侧从 run.params 重算，快照无需携带
        return {"schema_version": 1, "state": {}, "pending_timers": []}

    @staticmethod
    def handle_op(op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        return {"op": op}

    def decide(self, world: RunWorld, notifications: Sequence[RunNotification]) -> list[RunAction]:
        if notifications:
            # 防挂起注入等后续通知不重复留痕，保持事件数确定
            return []
        run_id = world.run.run_id
        if self._restored:
            # 恢复实例补一条续跑留痕后立即判定终态；失败行为的 run 复现失败终态
            finish = FinishRun(
                FinishOutcome.FAILED
                if self._behavior == FAIL_BEHAVIOR
                else FinishOutcome.COMPLETED,
                summary="fake run resumed",
            )
            return [EmitEvent(FAKE_RESUMED_EVENT, {"run_id": run_id}), finish]
        if self._behavior == FAIL_BEHAVIOR:
            return [
                EmitEvent(FAKE_PROGRESS_EVENT, {"run_id": run_id}),
                FinishRun(FinishOutcome.FAILED, summary="fake run failed"),
            ]
        if self._behavior == RESUMABLE_BEHAVIOR:
            # 只留痕不判定终态：run 停在 running，等进程崩溃后由恢复链续跑
            return [EmitEvent(FAKE_PROGRESS_EVENT, {"run_id": run_id})]
        return [
            EmitEvent(FAKE_PROGRESS_EVENT, {"run_id": run_id}),
            FinishRun(FinishOutcome.COMPLETED, summary="fake run completed"),
        ]

    def projection(self) -> dict[str, Any]:
        return {"schema_version": 1, "format": FAKE_FORMAT, "behavior": self._behavior}


class FakeRunDriverFactory:
    """fake@1 工厂：创建按 run 参数选行为，恢复实例续跑留痕后立即判定终态。"""

    @property
    def format(self) -> str:
        return FAKE_FORMAT

    @staticmethod
    def create(run: RunRecord, program_path: Path, params: Mapping[str, str]) -> FakeRunDriver:
        return FakeRunDriver(resolve_behavior(params), restored=False)

    @staticmethod
    def restore(
        run: RunRecord, snapshot: Mapping[str, Any], sessions: Sequence[RunSessionInfo]
    ) -> FakeRunDriver:
        # 行为取自 run 记录的参数：恢复实例不再走可中断分支
        return FakeRunDriver(resolve_behavior(run.params), restored=True)


class FakeDriverPlugin(Plugin):
    """内置验收插件：只登记 fake@1 驱动工厂，无其他装配。"""

    def uninstall(self) -> None:
        """无资源需要释放。"""

    def get_run_driver_factories(self) -> dict[str, Any]:
        """登记 fake@1 驱动工厂。"""
        return {FAKE_FORMAT: FakeRunDriverFactory()}
