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

"""Run 导出贡献协议——format 所有者翻译工作流关系的 SDK 契约。

贡献者是纯数据翻译组件：只读请求载荷（冻结程序目录允许读取），产出
中立的成员/关系贡献；会话单元生产、校验与写包由核心独占。请求与
贡献的形状保持格式中立（annotation 自由），不把任何交换格式的词表
固化进 SDK 类型。增补字段属兼容演进，不升 run_driver 的
PROTOCOL_VERSION。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from workflow_controller.core.runs import RunRecord, SessionRunAffiliationRecord


@dataclass(frozen=True)
class RunExportRequest:
    """贡献者的只读输入：核心装配的账面、事件、快照与冻结程序面。"""

    run: RunRecord
    members: tuple[SessionRunAffiliationRecord, ...]
    roster: Mapping[str, Mapping[str, Any]]
    events: Sequence[Mapping[str, Any]]
    driver_snapshot: Mapping[str, Any] | None
    driver_projection: Mapping[str, Any] | None
    program_dir: Path


@dataclass(frozen=True)
class WorkflowMemberContribution:
    """中立成员：session 引用源会话，process/workflow 无会话。"""

    id: str
    kind: str
    name: str
    started_at: str | None = None
    finished_at: str | None = None
    session_id: str | None = None
    output_text: str | None = None
    annotation: Mapping[str, Any] | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowRelationContribution:
    """中立关系边：端点是整棵成员树唯一的成员 id。"""

    from_id: str
    to_id: str
    annotation: Mapping[str, Any] | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunExportContribution:
    """一次导出的工作流贡献（nested 键为成员 id → 嵌套子图贡献）。"""

    name: str | None = None
    members: tuple[WorkflowMemberContribution, ...] = ()
    relations: tuple[WorkflowRelationContribution, ...] = ()
    nested: Mapping[str, RunExportContribution] = field(default_factory=dict)


class RunExportContributor(Protocol):
    """驱动工厂的可选导出面：把 run 事实翻译为工作流关系贡献。"""

    def export_contribution(self, request: RunExportRequest) -> RunExportContribution:
        raise NotImplementedError


def synthetic_member_id(
    run_id: str,
    node_path: str,
    generation: int,
    attempt: int,
    role: str,
) -> str:
    """确定性合成成员 id（m+24hex）：同 run 同位置跨次导出稳定。"""
    raw = f"{run_id}|{node_path}|{generation}|{attempt}|{role}"
    return "m" + hashlib.sha256(raw.encode()).hexdigest()[:24]
