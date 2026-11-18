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

"""提示词与报告文本组装（纯函数）：节点/验收提示、重试上下文、工作报告。"""

from __future__ import annotations

from workflow_controller.plugins.graph.template import render

# 超时续跑的默认提示（执行成员 / 验收成员各一）
CONTINUE_PROMPT = (
    "上一轮执行因超时被中断，工作进度可能未收尾。请回顾已完成的"
    "工作（包括你此前写入的文件与结论），从断点继续推进任务并在"
    "完成后给出收尾汇报。不要重做已完成的部分。"
)
VERIFY_CONTINUE_PROMPT = (
    "验收执行因超时被中断。请基于此前已完成的检查继续验收，"
    "不要重做已核实的部分，最后给出最终判定（最后一行 "
    "PASS 或 FAILED）。"
)

# 重试上下文注入的验收反馈最大保留字符数（保持源提交的前缀截取行为）
MAX_RETRY_CONTEXT_CHARS = 2000


def build_node_prompt(node: dict, ctx: dict, extra_context: str = "") -> str:
    """节点执行提示词：任务描述/目标（缺省回退 prompt）+ 附加上下文。"""
    parts: list[str] = []
    header = [f"${name}={ctx[name]}" for name in ("WORK_DIR", "USER_PROMPT") if ctx.get(name)]
    if header:
        parts.append("\n".join(header))
    desc = node.get("task_description")
    if desc:
        parts.append(f"## 任务描述\n{render(desc, ctx)}")
    goal = node.get("task_goal")
    if goal:
        parts.append(f"## 任务目标\n{render(goal, ctx)}")
    if not desc and not goal:
        parts.append(render(node.get("prompt", "完成任务。"), ctx))
    if extra_context:
        parts.append(extra_context)
    parts.append("\n完成后返回 DONE。")
    return "\n\n".join(parts)


def build_verifier_prompt(acc: dict, ctx: dict) -> str:
    """验收提示词（acceptance.verifier_prompt 渲染）。"""
    return render(acc.get("verifier_prompt", "验收上述产物。通过即 PASS。"), ctx)


def build_retry_context(node: dict, ctx: dict, last_reason: str) -> str:
    """重试注入的上下文：上轮验收反馈 + 上轮报告路径（agent 自行阅读全文）。"""
    parts = [
        "## 重试上下文",
        f"这是第 {int(ctx.get('attempt', '1'))} 次尝试，上一轮验收未通过。",
    ]
    if last_reason:
        parts.append(f"上一轮验收反馈：{last_reason[:MAX_RETRY_CONTEXT_CHARS]}")
    report_cfg = node.get("report") or {}
    save_to = report_cfg.get("save_to")
    if report_cfg.get("enabled") and save_to:
        prev_ctx = dict(ctx)
        prev_ctx["attempt"] = str(int(ctx.get("attempt", "1")) - 1)
        prev_report = render(save_to, prev_ctx)
        parts.append(
            f"上一轮工作报告在 `{prev_report}`，请先阅读该文件全文，"
            "再结合上述验收反馈定位问题，针对性修复后重新完成任务。"
        )
    return "\n\n".join(parts)


def render_report(
    node_id: str,
    attempt: str,
    executor_error: str | None = None,
    executor_output: str = "",
    session_id: str | None = None,
    num_turns: int | None = None,
) -> str:
    """工作报告文本：标题 + 执行错误 + 执行输出 + 会话元数据。"""
    lines = [f"# Report: {node_id} attempt {attempt}\n"]
    if executor_error:
        lines.append(f"## Executor Error\n\n{executor_error}\n")
    if executor_output:
        lines.append(f"## Executor Output\n\n{executor_output}\n")
    if session_id:
        lines.append(f"## Session\n\n- session_id: {session_id}\n- num_turns: {num_turns}\n")
    return "\n".join(lines)


def report_save_path(node: dict, ctx: dict) -> str:
    """报告落盘的相对路径（save_to 模板渲染；未配置为空串）。"""
    save_to = (node.get("report") or {}).get("save_to")
    return render(save_to, ctx) if save_to else ""
