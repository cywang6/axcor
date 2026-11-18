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

"""run 状态词表与迁移表单点。

账面状态×触发迁移、暂停档位写者矩阵、kind/format 一致性校验的唯一权威
应答者；账面守卫与 run 平面受理语义同源于此，不各自维护映射。
"""

from __future__ import annotations

from enum import StrEnum

TERMINAL_STATUSES = ("completed", "failed", "stopped")
NON_TERMINAL_STATUSES = ("pending", "running", "paused")
ALL_STATUSES = NON_TERMINAL_STATUSES + TERMINAL_STATUSES


class PauseGear(StrEnum):
    """暂停档位：写者矩阵的当前档与写入档词表。"""

    USER = "user"
    AUTH = "auth"
    PROJECTION = "projection"


class RunTransitionError(ValueError):
    """当前状态下不接受的迁移。"""


class PauseReasonConflictError(ValueError):
    """档位写者矩阵拒绝的写法（如对 auth 挂起强行清档）。"""


class RunTrigger(StrEnum):
    """账面状态迁移的触发词表（pause 族不在其列——合法性取决于档位，归 next_pause_gear）。"""

    START = "start"  # 写序：pending → running
    STOP = "stop"  # 终态控制
    DRIVER_FINISH = "driver_finish"  # 驱动终态判定
    ALL_SESSIONS_TERMINAL = "all_sessions_terminal"  # 容器收尾规则
    RESTART = "restart"  # 终态重开
    BOOT_PENDING = "boot_pending"  # boot 发现 pending 残行


def apply_run_trigger(
    kind: str, status: str, trigger: RunTrigger, *, outcome: str | None = None
) -> str:
    """状态×触发迁移表：返回新 status；迁移后状态不变的返回原值，非法迁移 raise。

    implicit 全触发拒绝：状态列唯一写者是投影回调（implicit_projection_status）。
    pause 族操作（pause_run/resume_run/auth 挂起/恢复）不在此表：合法性取决于
    pause_reason 档位，唯一权威是 next_pause_gear（返回 (status, pause_reason)）。
    """
    if kind == "implicit":
        raise RunTransitionError(f"implicit run 状态=成员投影，无触发迁移（收到 {trigger.value}）")
    match (status, trigger):
        case ("pending", RunTrigger.START):
            return "running"
        case ("pending" | "running" | "paused", RunTrigger.STOP):
            return "stopped"
        case ("completed" | "failed" | "stopped", RunTrigger.STOP):
            return status  # 终态重复控制 = 查账 no-op
        case ("running" | "paused", RunTrigger.DRIVER_FINISH):
            if outcome not in ("completed", "failed"):
                raise RunTransitionError(f"驱动终态判定只能是 completed/failed，收到：{outcome!r}")
            return str(outcome)
        case ("running" | "paused", RunTrigger.ALL_SESSIONS_TERMINAL) if kind == "container":
            return "completed"
        case ("completed" | "failed" | "stopped", RunTrigger.RESTART):
            return "running"
        case ("pending", RunTrigger.BOOT_PENDING):
            return "failed"
    raise RunTransitionError(f"非法迁移：{kind} run 在 {status} 态不接受触发 {trigger.value}")


def next_pause_gear(writer: str, status: str, reason: str | None) -> tuple[str, str | None]:
    """暂停档位写者矩阵：返回新 (status, pause_reason)；拒绝的写法 raise。

    user 档永不被 auth 挂起覆写；auth 档恢复写点不接受 resume_run 强清；
    driver_pause（PauseRun 动作）与 pause_run 同落 user 档，唯 auth 档保持
    不改判——资源挂起优先。
    """
    gear = reason if status == "paused" else None
    match (writer, status, gear):
        case ("pause_run", "running", _):
            return ("paused", PauseGear.USER)
        case ("pause_run", "paused", PauseGear.AUTH):
            return ("paused", PauseGear.USER)  # 用户接管：升格 user 档
        case ("pause_run", "paused", PauseGear.USER | None):
            return ("paused", PauseGear.USER)
        case ("driver_pause", "running", _):
            return ("paused", PauseGear.USER)
        case ("driver_pause", "paused", PauseGear.AUTH):
            return ("paused", PauseGear.AUTH)  # 资源挂起优先：保持 auth 档，恢复后条件再命中会再发
        case ("driver_pause", "paused", PauseGear.USER | None):
            return ("paused", PauseGear.USER)
        case ("resume_run", "running", _):
            return ("running", None)
        case ("resume_run", "paused", PauseGear.USER | None):
            return ("running", None)
        case ("resume_run", "paused", PauseGear.AUTH):
            raise PauseReasonConflictError(
                "auth 档挂起未恢复（key 耗尽），不能经 resume_run 强行清档"
            )
        case ("auth_suspend", "running", _):
            return ("paused", PauseGear.AUTH)
        case ("auth_suspend", "paused", PauseGear.AUTH):
            return ("paused", PauseGear.AUTH)  # 幂等
        case ("auth_suspend", "paused", PauseGear.USER | None):
            return ("paused", gear)  # 不覆写用户档
        case ("auth_resume", "running", _):
            return ("running", None)  # 无档可清，不触
        case ("auth_resume", "paused", PauseGear.AUTH):
            return ("running", None)
        case ("auth_resume", "paused", PauseGear.USER | None):
            return ("paused", gear)  # 只认 auth 档，用户暂停不丢
    raise PauseReasonConflictError(f"档位写者 {writer} 不适用于 {status}({gear}) 态")


def implicit_projection_status(any_session_running: bool) -> tuple[str, str | None]:
    """隐式 run 的状态投影：成员 session 任一运行 → running，否则 paused(projection)。"""
    if any_session_running:
        return ("running", None)
    return ("paused", PauseGear.PROJECTION)


def validate_kind_format(kind: str, fmt: str) -> None:
    """kind 与 format 的一致性单点：workflow 配非空格式串，其余配空串。"""
    if kind == "workflow" and not fmt:
        raise ValueError("kind=workflow 必须携带非空 format（驱动工厂派发键）")
    if kind in ("container", "implicit") and fmt:
        raise ValueError(f"kind={kind} 不携带 format（收到 {fmt!r}）")
