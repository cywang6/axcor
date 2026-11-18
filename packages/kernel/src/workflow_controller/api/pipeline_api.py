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

"""Pipeline API — 流水线进度。

进度和门禁同源:都读 session.stages + 同一套阶段求值（workflow_controller.plugin.stages 共享模块）。
一个通用引擎,所有领域零定制——换领域只改项目 workflow-controller.yaml 的 stages,不碰这里。

snapshot() 取指定 session(或自动选 RUNNING),从该 session 的 stages 声明 +
磁盘门禁逐阶段求完成情况。session 没声明 stages → 返回空 snapshot。

all_snapshots() 返回所有有 stages 声明的 session 的进度快照,供 SSE 推送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.stages import completed_prefix, declared_stages

if TYPE_CHECKING:
    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.session.session import Session


@dataclass
class PipelineSnapshot:
    """流水线进度纯数据结构。"""

    stages: list[dict[str, Any]] = field(default_factory=list)
    active_stage: str | None = None
    total_progress: float = 0.0
    detail: str = ""


class PipelineAPI:
    """流水线进度 API。"""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    @staticmethod
    def _stages_from(session: Session) -> list[dict[str, Any]]:
        """从 session.config.stages 读有效阶段(name + artifacts 必填)。"""
        return declared_stages(list(getattr(session.config, "stages", None) or []))

    def snapshot(self, session_id: str | None = None) -> PipelineSnapshot:
        """目标 session 的逐阶段进度快照。

        session_id 缺省 → 自动选(RUNNING 优先,否则最近活跃)。没 session /
        session 没声明 stages → 空 snapshot。
        """
        session = self._resolve_session(session_id)
        if session is None:
            return PipelineSnapshot()
        return self._snapshot_for(session)

    def all_snapshots(self) -> dict[str, PipelineSnapshot]:
        """所有有 stages 声明的 session 的进度快照,keyed by session_id。"""
        result: dict[str, PipelineSnapshot] = {}
        pool = self._orch.pool
        for session in pool.list_sessions():
            stages = self._stages_from(session)
            if not stages:
                continue
            result[session.id] = self._snapshot_for(session)
        return result

    def _snapshot_for(self, session: Session) -> PipelineSnapshot:
        stages = self._stages_from(session)
        if not stages:
            return PipelineSnapshot()

        cwd = Path(session.config.cwd)
        n_done = completed_prefix(stages, cwd)
        total = len(stages)
        active_idx = n_done if n_done < total else None

        stages_out: list[dict[str, Any]] = []
        for i, stage in enumerate(stages):
            done = i < n_done
            stages_out.append(
                {
                    "name": stage["name"],
                    "completed": done,
                    "active": i == active_idx,
                    "progress": 1.0 if done else 0.0,
                }
            )

        if active_idx is not None:
            active_cfg = stages[active_idx]
            detail = active_cfg.get("description") or f"{active_cfg['name']} · 进行中"
            active_name: str | None = active_cfg["name"]
        else:
            detail = "done"
            active_name = None

        return PipelineSnapshot(
            stages=stages_out,
            active_stage=active_name,
            total_progress=n_done / total,
            detail=detail,
        )

    def _resolve_session(self, session_id: str | None) -> Session | None:
        """session_id 显式则取它;否则 RUNNING 优先,无则回退最近活跃。"""
        pool = self._orch.pool
        if session_id:
            return pool.get(session_id)
        from workflow_controller.session.session import SessionState

        running = [s for s in pool.list_sessions() if s.state == SessionState.RUNNING]
        if running:
            return running[0]
        sessions = pool.list_sessions()
        if not sessions:
            return None
        return max(sessions, key=lambda s: getattr(s, "last_activity", s.created_at))
