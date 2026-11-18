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

"""status_maps 单点：状态×触发迁移矩阵与暂停档位写者矩阵的穷举测试。"""

from __future__ import annotations

from typing import ClassVar

import pytest

from workflow_controller.core.runs import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    RunKind,
    RunStatus,
)
from workflow_controller.core.status_maps import (
    ALL_STATUSES,
    PauseGear,
    PauseReasonConflictError,
    RunTransitionError,
    RunTrigger,
    apply_run_trigger,
    implicit_projection_status,
    next_pause_gear,
    validate_kind_format,
)

KINDS = ("workflow", "container", "implicit")


class TestTransitionMatrix:
    """穷举：全部 (kind × status × trigger) 组合的结果与规则表逐格一致。"""

    # 触发 → {合法起始状态集: 目标}；DRIVER_FINISH 目标取 outcome、终态 STOP 幂等
    # pause 族不进迁移表：合法性取决于 pause_reason 档位，唯一权威 next_pause_gear
    RULES: ClassVar[dict] = {
        RunTrigger.START: {("pending",): "running"},
        RunTrigger.STOP: {("pending", "running", "paused"): "stopped"},
        RunTrigger.DRIVER_FINISH: {("running", "paused"): "failed"},
        RunTrigger.ALL_SESSIONS_TERMINAL: {("running", "paused"): "completed"},
        RunTrigger.RESTART: {("completed", "failed", "stopped"): "running"},
        RunTrigger.BOOT_PENDING: {("pending",): "failed"},
    }

    @staticmethod
    def test_driver_finish_outcome_passthrough():
        assert (
            apply_run_trigger("workflow", "running", RunTrigger.DRIVER_FINISH, outcome="completed")
            == "completed"
        )

    @staticmethod
    def test_driver_finish_requires_terminal_outcome():
        with pytest.raises(RunTransitionError):
            apply_run_trigger("workflow", "running", RunTrigger.DRIVER_FINISH, outcome="running")

    @staticmethod
    def test_implicit_rejects_all_triggers():
        with pytest.raises(RunTransitionError):
            apply_run_trigger("implicit", "running", RunTrigger.DRIVER_FINISH, outcome="completed")
        with pytest.raises(RunTransitionError):
            apply_run_trigger("implicit", "running", RunTrigger.STOP)
        with pytest.raises(RunTransitionError):
            apply_run_trigger("implicit", "completed", RunTrigger.RESTART)

    @staticmethod
    def test_restart_rejects_non_terminal():
        with pytest.raises(RunTransitionError):
            apply_run_trigger("workflow", "running", RunTrigger.RESTART)

    @staticmethod
    def test_pause_family_excluded():
        """迁移表不感知档位，pause 族进表会放行档位矩阵拒绝的写法（如 auth 档 resume）。"""
        pause_family_words = {"pause_user", "resume_user", "auth_suspend", "auth_resume"}
        assert not pause_family_words & {e.value for e in RunTrigger}

    def test_exhaustive_matrix(self):
        n_ok = 0
        for kind in KINDS:
            for status in ALL_STATUSES:
                n_ok += self._trigger_matches(kind, status)
        assert n_ok > 0

    def _trigger_matches(self, kind, status) -> int:
        """单行 (kind, status) 的全部 trigger 断言；返回成功格数。"""
        passed = 0
        for trigger in RunTrigger:
            expected = self._expected(kind, status, trigger)
            try:
                got = apply_run_trigger(kind, status, trigger, outcome="failed")
            except RunTransitionError:
                assert expected is None, (kind, status, trigger)
                continue
            assert got == expected, (kind, status, trigger)
            passed += 1
        return passed

    def _expected(self, kind, status, trigger):
        if kind == "implicit":
            return None  # 状态=成员投影，全触发拒绝
        base = None
        for states, target in self.RULES[trigger].items():
            if status in states:
                base = target
                break
        if base is None and trigger is RunTrigger.STOP and status in TERMINAL_STATUSES:
            return status  # 终态重复控制 = 查账 no-op
        if base is not None and trigger is RunTrigger.ALL_SESSIONS_TERMINAL and kind != "container":
            return None  # 收尾规则只对 container 成立
        return base


class TestPauseGearMatrix:
    """穷举：写者 × (状态, 当前档) 全覆盖；running 态档位恒视为空。"""

    EXPECTED: ClassVar[dict] = {
        ("pause_run", "running", None): ("paused", "user"),
        ("pause_run", "paused", "auth"): ("paused", "user"),  # 升格接管
        ("pause_run", "paused", "user"): ("paused", "user"),  # 幂等
        ("pause_run", "paused", None): ("paused", "user"),
        ("driver_pause", "running", None): ("paused", "user"),
        ("driver_pause", "paused", "auth"): ("paused", "auth"),  # 资源挂起优先：不改档
        ("driver_pause", "paused", "user"): ("paused", "user"),  # 幂等
        ("driver_pause", "paused", None): ("paused", "user"),
        ("resume_run", "running", None): ("running", None),  # 幂等
        ("resume_run", "paused", "user"): ("running", None),
        ("resume_run", "paused", None): ("running", None),
        ("auth_suspend", "running", None): ("paused", "auth"),
        ("auth_suspend", "paused", "auth"): ("paused", "auth"),  # 幂等
        ("auth_suspend", "paused", "user"): ("paused", "user"),  # 不覆写用户档
        ("auth_suspend", "paused", None): ("paused", None),  # 空档直通
        ("auth_resume", "running", None): ("running", None),  # 不触
        ("auth_resume", "paused", "auth"): ("running", None),
        ("auth_resume", "paused", "user"): ("paused", "user"),  # 只认 auth 档
        ("auth_resume", "paused", None): ("paused", None),
    }

    REFUSED: ClassVar[set] = {("resume_run", "paused", "auth")} | {
        (w, "paused", "projection")
        for w in ("pause_run", "driver_pause", "resume_run", "auth_suspend", "auth_resume")
    }

    GEARS: ClassVar[tuple] = (None, "user", "auth", "projection")

    WRITERS: ClassVar[tuple] = (
        "pause_run",
        "driver_pause",
        "resume_run",
        "auth_suspend",
        "auth_resume",
    )

    @staticmethod
    def test_resume_on_auth_gear_refused():
        with pytest.raises(PauseReasonConflictError):
            next_pause_gear("resume_run", "paused", "auth")

    @staticmethod
    def test_gear_enum_values():
        assert {g.value for g in PauseGear} == {"user", "auth", "projection"}

    def test_exhaustive_matrix(self):
        """每格要么命中规则、要么显式拒绝；running 态各档位输入归一为空档。"""
        for writer in self.WRITERS:
            for status in ("running", "paused"):
                self._assert_gear_row(writer, status)

    def _assert_gear_row(self, writer, status) -> None:
        """单 writer×status 行的全部档位断言。"""
        for gear in self.GEARS:
            combo = (writer, status, gear)
            if combo in self.REFUSED:
                with pytest.raises(PauseReasonConflictError):
                    next_pause_gear(writer, status, gear)
                continue
            key = (writer, status, None if status == "running" else gear)
            assert next_pause_gear(writer, status, gear) == self.EXPECTED[key], combo


class TestVocabularyAlignment:
    """status_maps 词表与账面枚举对齐（防两处漂移）。"""

    @staticmethod
    def test_status_sets_match_enum():
        values = {s.value for s in RunStatus}
        assert set(TERMINAL_STATUSES) | set(NON_TERMINAL_STATUSES) == values
        assert not set(TERMINAL_STATUSES) & set(NON_TERMINAL_STATUSES)

    @staticmethod
    def test_implicit_projection():
        assert implicit_projection_status(True) == ("running", None)
        assert implicit_projection_status(False) == ("paused", "projection")


class TestKindFormatValidation:
    @staticmethod
    def test_workflow_needs_format():
        validate_kind_format("workflow", "graph-yaml@1")
        with pytest.raises(ValueError, match="非空 format"):
            validate_kind_format("workflow", "")

    @staticmethod
    def test_container_implicit_empty_format():
        validate_kind_format("container", "")
        validate_kind_format("implicit", "")
        with pytest.raises(ValueError):
            validate_kind_format("container", "graph-yaml@1")
        with pytest.raises(ValueError):
            validate_kind_format("implicit", "dag-yaml@1")

    @staticmethod
    def test_kinds_covered():
        assert {k.value for k in RunKind} == {"workflow", "container", "implicit"}
