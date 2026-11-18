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

"""验收判定协议单测：三级判定、进程结局分类与重试/回滚去向（无 asyncio）。"""

from __future__ import annotations

import pytest

from workflow_controller.plugins.graph.driver.gates import (
    classify_process_exit,
    judge_verdict_last_line,
    judge_verify_output,
    parse_verifier_verdict,
    retry_or_exhaust,
    script_verify_reason,
)


class TestTier1LastLineKeyword:
    @staticmethod
    def test_last_line_pass():
        assert judge_verdict_last_line("验收过程……\n各项检查完成\nPASS") is True

    @staticmethod
    def test_last_line_failed():
        assert judge_verdict_last_line("……\nFAILED") is False

    @staticmethod
    def test_last_line_chinese_neg_first():
        # 「不通过」含「通过」——负面词必须优先
        assert judge_verdict_last_line("检查完成\n验收不通过") is False

    @staticmethod
    def test_last_line_chinese_pos():
        assert judge_verdict_last_line("检查完成\n验收通过") is True

    @staticmethod
    def test_last_line_chinese_neg():
        assert judge_verdict_last_line("……\n未通过") is False

    @staticmethod
    def test_last_line_mixed_desc():
        # 描述行不算（13 failed 在中间），最后一行 PASS 命中
        assert judge_verdict_last_line("13 tests / 0 passed / 13 failed\n分析……\nPASS") is True

    @staticmethod
    def test_empty():
        assert judge_verdict_last_line("") is None
        assert judge_verdict_last_line("\n \n") is None

    @staticmethod
    def test_no_keyword():
        assert judge_verdict_last_line("详见报告 docs/x.md") is None


class TestTier2FullLineVerdict:
    @staticmethod
    def test_stats_line_not_verdict():
        # 「10 passed, 0 failed」非独立整行 verdict，不参与
        assert parse_verifier_verdict("10 passed, 0 failed") is False

    @pytest.mark.parametrize(
        "text",
        [
            "PASS",
            "PASSED",
            "  PASS  ",
            "通过",
            "PASS\n补充说明",
            "说明\nPASS",
            "10 passed, 0 failed\nPASS",
            "Previously failed check is now fixed.\nPASS",
        ],
    )
    def test_verdict_pass(self, text):
        assert parse_verifier_verdict(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "cannot PASS",
            "NOT PASS",
            "尚未 PASS",
            "无法通过",
            "未通过",
            "no pass",
            "FAILED",
            "FAIL",
            "PASSED\nFAILED",
            "",
            "验收结果：失败",
            "maybe pass, not sure",
        ],
    )
    def test_verdict_fail(self, text):
        assert parse_verifier_verdict(text) is False


class TestJudgeVerifyOutput:
    @staticmethod
    def test_tier1_hit():
        assert judge_verify_output("检查完成\nPASS") is True

    @staticmethod
    def test_explicit_fail_is_false():
        # tier1 不命中、tier2 有明确 FAIL 行 → 直接失败（不触发兜底）
        assert judge_verify_output("检查过程……\nFAIL\n结论见上") is False

    @staticmethod
    def test_no_verdict_needs_fallback():
        # 无任何 verdict 行 → None（宿主起兜底 agent）
        assert judge_verify_output("检查完成，判定见上") is None

    @staticmethod
    def test_empty_needs_fallback():
        assert judge_verify_output("") is None


class TestClassifyProcessExit:
    @staticmethod
    def test_ok():
        assert classify_process_exit(0, False, None) == "ok"

    @staticmethod
    def test_timeout():
        assert classify_process_exit(-15, True, "plane_killed") == "timeout"

    @staticmethod
    def test_spawn_failed():
        assert classify_process_exit(-1, False, "spawn_failed") == "spawn_failed"

    @staticmethod
    def test_plane_killed_is_stopped():
        assert classify_process_exit(-15, False, "plane_killed") == "stopped"

    @staticmethod
    def test_external_signal_is_signaled():
        # SIGHUP 死亡的 -1 与起动失败区分开
        assert classify_process_exit(-1, False, None) == "signaled"

    @staticmethod
    def test_nonzero_exit_is_failed():
        assert classify_process_exit(2, False, None) == "failed"


class TestRetryOrExhaust:
    @staticmethod
    def test_within_retries():
        assert retry_or_exhaust(1, 2, "stop", "", 0) == "retry"

    @staticmethod
    def test_exhaust_continue():
        assert retry_or_exhaust(3, 2, "continue", "", 0) == "continue"

    @staticmethod
    def test_exhaust_rollback_with_budget():
        assert retry_or_exhaust(3, 2, "rollback", "A", 1) == "rollback"

    @staticmethod
    def test_rollback_budget_exhausted_fails():
        assert retry_or_exhaust(3, 2, "rollback", "A", 0) == "fail"

    @staticmethod
    def test_rollback_without_target_fails():
        assert retry_or_exhaust(3, 2, "rollback", "", 5) == "fail"

    @staticmethod
    def test_stop_fails():
        assert retry_or_exhaust(3, 2, "stop", "", 5) == "fail"


class TestScriptVerifyReason:
    @staticmethod
    def test_pass_reason():
        assert script_verify_reason("ls foo", 0) == "验收通过: `ls foo`"

    @staticmethod
    def test_fail_reason_with_output_tail():
        reason = script_verify_reason("grep -q x f", 1, "no match")
        assert reason == "验收脚本失败 (exit 1): `grep -q x f`（输出: no match）"

    @staticmethod
    def test_fail_reason_without_tail():
        assert "（输出" not in script_verify_reason("false", 1, "")
