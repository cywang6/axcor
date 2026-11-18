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

"""staged-pipeline 门禁执行器：读 session 带来的领域阶段，确定性强制顺序 + 完成门禁。

**stages 归项目，不归 plugin**：阶段声明在项目 ``workflow-controller.yaml`` 的 ``stages:``，选目录
起 session 时注入到 ``session.config.stages``。本 plugin 在门禁钩子里从 session 读它——
**没声明 stages 的 session 门禁惰性返回 None**（不把任何阶段强行套上去）。

**一道门，长在 iteration boundary**（driver-agnostic，框架唯一控制点）：
- ``on_iteration_boundary``：算 ``completed_prefix`` → 跳步 block / k==0 block /
  0<k<总 set pending+defer（交 orchestrator boundary reviewer）/ 全过放行。
  block 经 loop 反馈注入推进下一轮（worker 每轮做一个阶段、停下），CC/opencode/CLI 一致。
- **不再用 on_stop_hook 做 per-stage 门禁**：CC 的 Stop-hook block 返回值能重定向活 worker，
  但 opencode 的 stop 返回值被丢弃（worker 已退出）→ stop-hook 门禁对 opencode 无效。统一到
  iteration boundary 后两个 runtime 行为一致。stop-hook 对 staged session 退化为「让 turn 结束」
  （orchestrator 顺手捕获 ``last_worker_text`` 给 boundary reviewer 当 context）。

和 reviewer 的关系：门禁是确定性**硬地板**（查产物在不在/非占位/按序），0<k<总 时 defer；
orchestrator 的 boundary reviewer 查语义（该阶段产物真不真），approve→推进下一轮 / reject→重做。
门禁查结构、reviewer 查语义，互补不冗余。详见 docs/designs/stage-gating.md §3。

加新领域 = 新项目 workflow-controller.yaml 写一组 stages + 一段**短**的领域 starting_prompt（任务 +
每阶段产物该装什么领域内容），**零动核心、零 plugin 复制**。通用的「逐阶段工作纪律」（一轮
一阶段、写完该阶段全部文件、停下汇报、跨阶段=跳步会白做、不要占位空壳）描述的是 stage 机制
本身、不是领域知识——由本 plugin 经 ``prompt_prefix`` 自动注入（见 ``_WORK_DISCIPLINE``），
session 启动时 prepend 到领域 prompt 前。所以**领域 starting_prompt 不必重复这套**，只写领域
内容。plugin 代码：门禁（确定性硬地板）+ 这段通用纪律注入。

领域 starting_prompt（放进项目 workflow-controller.yaml，不是 plugin 的 config.yaml；纪律自动加在前）::

    任务：<一句话领域任务>。完整需求见 <README>，先读它。
    按 <stage1> → <stage2> → ... 推进（产物对应 workflow-controller.yaml 的 stages）：
    - <stage1>：<该阶段产物的领域内容说明>
    - <stage2>：<...>
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.sdk import IterationGateResult, Plugin
from workflow_controller.plugin.stages import (
    artifacts_display,
    completed_prefix,
    declared_stages,
    detect_jump,
)

if TYPE_CHECKING:
    from workflow_controller.plugin.context import PluginContext

logger = logging.getLogger(__name__)


# 通用逐阶段工作纪律——描述的是 stage 机制本身怎么运转（一轮一阶段/写完停/跨阶段=跳步
# 白做/不要占位），**不是领域知识**。由 plugin 经 prompt_prefix 注入，session 启动时
# 自动 prepend 到项目 starting_prompt 前。这样 domain yaml 的 starting_prompt 只写领域
# 内容（任务 + 每阶段产物该装什么），新领域零成本、不必复制这套纪律。
_WORK_DISCIPLINE = """\
【staged-pipeline 工作纪律·硬规则，先于一切】本轮任务按声明的 stages 分阶段推进，**每一轮
只做一个阶段**：把该阶段该交付的文件全部写完（一个阶段可以有多个文件，都属同一阶段、同一
轮内一起写完），然后**立刻停下、在最后一条消息里汇报本阶段做了什么**，等审查说「做下一阶段」
才动下一个。**禁止跨阶段**：一轮里写到了后续阶段的文件 = 跳步，门禁会拦回、后面写的作废要
重做——你会白做。不要用占位/空壳文件糊弄门禁（产物要实质）。这是逐阶段质量门控，不是一口气
做完再总查。\
"""


def _jump_gate_result(
    session_id: str,
    iteration: int,
    stages: list[dict[str, Any]],
    k: int,
    jump: tuple[str, str],
) -> IterationGateResult:
    """跳步 block：后置阶段产物已出现而前置门禁未过。"""
    current_name, later_name = jump
    cur_display = artifacts_display(stages[k])
    logger.info(
        "staged-pipeline: jump %s to %s for session %s (iter %d)",
        current_name,
        later_name,
        session_id,
        iteration,
    )
    return IterationGateResult(
        action="block",
        feedback=(
            f"⚠ 阶段顺序违规：检测到「{later_name}」阶段产物已出现，"
            f"但「{current_name}」阶段尚未完成（门禁未过）。\n"
            f"请先完成 {current_name} 阶段（产出 {cur_display} 且通过门禁），"
            f"再做 {later_name}。不要跳步。"
        ),
        reason=f"jump:{current_name}->{later_name}",
    )


def _first_stage_gate_result(
    session_id: str, iteration: int, first: dict[str, Any]
) -> IterationGateResult:
    """k==0 block：一个阶段都没过，要求先做 stage 0。"""
    logger.info(
        "staged-pipeline: boundary block, no stage complete for %s (iter %d, do %s)",
        session_id[:12],
        iteration,
        first["name"],
    )
    return IterationGateResult(
        action="block",
        feedback=(
            f"先完成「{first['name']}」阶段（产出 {artifacts_display(first)} 且通过门禁），"
            f"做完停下汇报。不要用占位文件糊弄。"
        ),
        reason=f"no_stage_complete:{first['name']}",
    )


def _defer_stage_review(
    session: Any, stages: list[dict[str, Any]], k: int, session_id: str
) -> IterationGateResult:
    """0<k<总：刚完成的阶段交 orchestrator boundary reviewer 审语义。"""
    done = stages[k - 1]
    nxt = stages[k]
    with contextlib.suppress(AttributeError, TypeError):
        # 测试用 SimpleNamespace/特殊对象可能不支持赋值——静默降级（不阻塞流程）
        session.pending_stage_review = {
            "done": done,
            "next": nxt,
            "done_index": k - 1,
        }
    logger.info(
        "门禁 ✓: %s 结构通过(产物已落盘/非占位/按序) → 交 reviewer 审语义 for %s (next=%s)",
        done["name"],
        session_id[:12],
        nxt["name"],
    )
    return IterationGateResult(action="pass")


class StagedPipelinePlugin(Plugin):
    def install(self, ctx: PluginContext) -> None:
        """Single lifecycle entry: declare the iteration_boundary gate.

        Pure 门禁 plugin：不挂 hook 路由、不挂 web router。``super().install(ctx)``
        dispatches :meth:`_register_interceptors`.
        """
        super().install(ctx)
        logger.info("staged-pipeline plugin installed")

    def uninstall(self) -> None:
        logger.info("staged-pipeline plugin uninstalled")

    def prompt_prefix(self, config: Any) -> str | None:
        """注入通用逐阶段工作纪律（stages 声明了才注入；通用 session 返回 None）。

        这套纪律描述的是 stage 机制本身（不是领域知识），归 plugin 拥有——domain 的
        starting_prompt 不必自带。镜像 _stages_for 的校验（name + artifact 必填）：
        有效 stages 非空才注入，全无效/没声明 → None（compose 跳过，零影响）。
        """
        raw = list(getattr(config, "stages", None) or [])
        return _WORK_DISCIPLINE if declared_stages(raw) else None

    async def on_iteration_boundary(
        self,
        session_id: str,
        session: Any,
        iteration: int,
    ) -> IterationGateResult:
        """逐阶段门禁（driver-agnostic；iteration boundary = 框架唯一控制点）。

        算 ``k = completed_prefix(stages, cwd)``，按阶段状态决定：

        - stages 空 → ``pass``（惰性，通用 session 不套阶段）。
        - 全过（k==总）→ ``pass``（放行到完成路径）。
        - 跳步（后置阶段产物已落盘而前置门禁未过）→ ``block``「别跳步，先做 {current}」。
        - 一个阶段都没过（k==0）→ ``block``「先做 stage 0，做完停」。
        - 某阶段刚完成、还没全过（0<k<总）→ 把 ``{done: stages[k-1], next: stages[k],
          done_index: k-1}`` 写到 ``session.pending_stage_review``，**返回 pass** →
          orchestrator 的 iteration-boundary reviewer 做 per-stage 语义评判（approve→推进 /
          reject→重做），block 经反馈注入推进下一轮——CC/opencode/CLI 一致（不再依赖 CC
          Stop-hook 的 block 返回值，opencode 也跑得起来）。这是「逐阶段检查点」的核心：
          worker 每轮做一个阶段、停下，boundary 审一次。

        只有结构判定会 ``block``（确定性硬地板）；语义判定（该阶段产物真不真）交给 reviewer。
        """
        stages = self._stages_for(session)
        if not stages:
            return IterationGateResult(action="pass")
        cwd = self._cwd(session)
        # 前缀单次求值，后续分支全部复用：command 门求值要起 shell，逐分支重算代价高。
        k = completed_prefix(stages, cwd)
        if k == len(stages):
            # 全过 → 置完成信号，让 iteration-boundary 闭包 mark_reviewer_let_exit（auto）/
            # 落 manual-prompt（manual）。确定性完成，不依赖 evaluator 配置（避免 PassThrough
            # evaluator 下 k==总 死循环）。不设 pending（最后阶段不做 per-stage review，结构门禁
            # 全过即查过 non_placeholder；语义整体收尾此处跳过，与原 stop-hook 一致）。
            with contextlib.suppress(AttributeError, TypeError):
                session.stages_complete = True
            return IterationGateResult(action="pass")

        jump = detect_jump(stages, cwd, prefix=k)
        if jump is not None:
            return _jump_gate_result(session_id, iteration, stages, k, jump)
        if k == 0:
            return _first_stage_gate_result(session_id, iteration, stages[0])
        return _defer_stage_review(session, stages, k, session_id)

    def _register_interceptors(self, ctx: Any) -> None:
        """Declare the iteration_boundary gate (staged sessions).

        The base _on_iteration_boundary_intercept helper adapts
        on_iteration_boundary's IterationGateResult into InterceptResult.
        Priority 10 — runs before the reviewer (priority 0).
        """
        ctx.bus.intercept(
            "iteration_boundary",
            self._on_iteration_boundary_intercept,
            priority=10,
        )

    def _cwd(self, session: Any) -> Path:
        try:
            return Path(session.config.cwd)
        except (AttributeError, TypeError):
            return Path.cwd()

    def _stages_for(self, session: Any) -> list[dict[str, Any]]:
        """从 session.config.stages 读阶段声明并校验（name + artifact 必填）。

        来源是项目 workflow-controller.yaml（选目录起 session 时注入）。没声明 / session 没这属性 /
        全无效 → 返回 []，两道门据此惰性返回 None（不强行套阶段）。
        """
        try:
            raw = list(getattr(session.config, "stages", None) or [])
        except AttributeError:
            return []
        stages = declared_stages(raw)
        if raw and not stages:
            logger.warning(
                "staged-pipeline: %d stage entries on session but 0 valid "
                "(missing name/artifacts?) — gate inert for this session",
                len(raw),
            )
        return stages
