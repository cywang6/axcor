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

"""验收判定协议：verdict 解析、进程结局分类与重试/回滚去向决策（纯函数）。

三级判定的前两级在此；无任何可识别 verdict 时返回 None，由宿主起兜底
agent 分析全文（异常/超时/仍无结论一律按失败）。
"""

from __future__ import annotations

import re

_PASS_LINE_RE = re.compile(r"(?:pass|passed|通过)", re.IGNORECASE)
_FAIL_LINE_RE = re.compile(r"(?:fail|failed)", re.IGNORECASE)
_NEGATED_PASS_LINE_RE = re.compile(
    r"(?:cannot|can't|cant|\bnot\b|\bno\b|尚未|未|无法|不能|无需|不需|don't|doesn't)"
    r"\s*(?:pass|passed|通过)",
    re.IGNORECASE,
)

# 三级判定关键词：子串匹配，负面词优先——「不通过」含「通过」、「failed」
# 含「pass」等重叠必须先查负面。
_VERDICT_NEG = ("fail", "failed", "失败", "不通过", "未通过", "拒绝")
_VERDICT_POS = ("pass", "通过", "已验证", "验收通过")


def parse_verifier_verdict(text: str) -> bool:
    """二级判定：解析独立整行的 PASS/FAILED verdict，含糊/否定一律失败。

    只把独立整行的 ``PASS`` / ``PASSED`` / ``通过`` 或 ``FAIL`` / ``FAILED``
    （忽略空白）当作 verdict，取最后一条为准；对 PASS 的否定（cannot / not /
    尚未 / 未 / 无法 … PASS）按失败处理。其余文本（如 ``10 passed, 0 failed``）
    不参与判定，避免最终 ``PASS`` 被历史失败词误伤。
    """
    verdict: bool | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _NEGATED_PASS_LINE_RE.fullmatch(stripped):
            verdict = False
        elif _PASS_LINE_RE.fullmatch(stripped):
            verdict = True
        elif _FAIL_LINE_RE.fullmatch(stripped):
            verdict = False
    return verdict is True


def judge_verdict_last_line(text: str) -> bool | None:
    """一级判定：最后一个非空行关键词子串匹配，命中返回 bool，未命中 None。"""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].lower()
    if any(w in last for w in _VERDICT_NEG):
        return False
    if any(w in last for w in _VERDICT_POS):
        return True
    return None


def has_explicit_verdict(text: str) -> bool:
    """全文是否存在任何独立整行 verdict（区分「明确 FAIL」与「无结论」）。"""
    return any(
        (
            _NEGATED_PASS_LINE_RE.fullmatch(line.strip())
            or _PASS_LINE_RE.fullmatch(line.strip())
            or _FAIL_LINE_RE.fullmatch(line.strip())
        )
        for line in text.splitlines()
        if line.strip()
    )


def judge_verify_output(text: str) -> bool | None:
    """三级判定前两级：True/False 为结论，None 需兜底 agent。"""
    tier1 = judge_verdict_last_line(text)
    if tier1 is not None:
        return tier1
    if parse_verifier_verdict(text):
        return True
    if has_explicit_verdict(text):
        return False
    return None


def classify_process_exit(rc: int, timed_out: bool, exit_mark: str | None) -> str:
    """进程结局分类：ok/timeout/stopped/spawn_failed/signaled/failed。

    stopped 是本面组杀（暂停/停止语义，保留断点不算失败）；signaled 是
    外部信号死亡（真实失败）——不凭 rc 符号推断，以终局标记为准。
    """
    if timed_out:
        return "timeout"
    if exit_mark == "spawn_failed":
        return "spawn_failed"
    if rc == 0:
        return "ok"
    if rc < 0 and exit_mark == "plane_killed":
        return "stopped"
    if rc < 0:
        return "signaled"
    return "failed"


def script_verify_reason(script: str, rc: int, out_tail: str = "") -> str:
    """验收脚本结果反馈：带脚本原文，失败时附输出尾部（重试才有信息量）。"""
    script_oneshot = " ".join(script.split())
    if rc == 0:
        return f"验收通过: `{script_oneshot}`"
    reason = f"验收脚本失败 (exit {rc}): `{script_oneshot}`"
    if out_tail:
        reason += f"（输出: {out_tail}）"
    return reason


def retry_or_exhaust(
    attempt_num: int,
    max_retries: int,
    on_exhaust: str,
    rollback_to: str,
    rollback_left: int,
) -> str:
    """验收失败后的去向：retry/continue/rollback/fail。"""
    if attempt_num <= max_retries:
        return "retry"
    if on_exhaust == "continue":
        return "continue"
    if on_exhaust == "rollback" and rollback_to and rollback_left > 0:
        return "rollback"
    return "fail"
