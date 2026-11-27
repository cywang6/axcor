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

"""深度恢复：工作区树运行时崩溃后的重建与对账。

重开或崩溃后工作区树运行时（内存账本）缺席——本模块从作用域账本表读
出该树的全部行装入账本：状态、两层代次、工作区引用、双基线与属主一律
以行为准，闸门状态归运行（崩溃的进程没有持有中的闸门）。随后对操作日
志做三态核对：意图未生效可放弃、已写只补账、对不上账显式失败（分叉即
停，绝不自动回退）；核对通过的操作把账面后果补到行上。最后对盘上事
实：活动与待集成作用域的工作区目录与分支必须在，否则作用域损坏；已
释放与已废弃的行若工作区仍在盘，按账移除。检查点行仍是 claims 的来
源；活动派发组是进程内状态，恢复后在报告里显式降级，不静默。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from workflow_controller.core.checkpoints import CheckpointTrigger
from workflow_controller.core.workspace.oplog import (
    OperationPhase,
    RecoveryAction,
    SagaOperation,
)
from workflow_controller.core.workspace.repository import (
    ROOT_INTEGRATION_REF,
    ROOT_SEED_REF,
)
from workflow_controller.core.workspace.scope_tree import (
    INITIAL_EPOCH,
    ScopeGateState,
    ScopeKind,
    ScopeRecord,
    ScopeState,
    ScopeTreeError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from workflow_controller.core.checkpoints import CheckpointStore, ScopeCheckpoint
    from workflow_controller.core.workspace.integration import IntegrationCoordinator
    from workflow_controller.core.workspace.manager import WorkspaceManager
    from workflow_controller.core.workspace.oplog import SagaOplog
    from workflow_controller.core.workspace.repository import WorkspaceRepository
    from workflow_controller.core.workspace.scope_ledger import ScopeLedgerStore
    from workflow_controller.core.workspace.scope_tree import ScopeTree

__all__ = [
    "WorkspaceRecoveryError",
    "WorkspaceRecoveryReport",
    "rebuild_workspace_tree",
]

# 操作日志中的操作种类词面（与 manager 记账一致）
_RETRACT_OP_KIND = "retract_integration_line"
_PREPARE_OP_KIND = "prepare_scope_workspace"
_INTEGRATE_OP_KIND = "integrate_scope"
_RESTORE_OP_KIND = "restore_scope"

# 工作区须在盘的状态；其余状态的工作区在不在盘都不改账
_LIVE_STATES = (ScopeState.ACTIVE, ScopeState.AWAITING_INTEGRATION)
# 工作区按账应已移除的状态；仍在盘即账面已落、移除未完成的崩溃残留
_RECLAIMED_STATES = (ScopeState.RELEASED, ScopeState.DISCARDED)


class WorkspaceRecoveryError(RuntimeError):
    """深度恢复对不上账：显式失败，不静默降级。"""


@dataclass
class WorkspaceRecoveryReport:
    """一次深度恢复的结论面：重建范围、核对结果与显式降级项。"""

    rebuilt_scope_ids: list[str] = field(default_factory=list)
    root_epoch: int = INITIAL_EPOCH
    abandoned_op_ids: list[str] = field(default_factory=list)
    completed_op_ids: list[str] = field(default_factory=list)
    claims_restored: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


async def rebuild_workspace_tree(
    *,
    repository: WorkspaceRepository,
    scope_tree: ScopeTree,
    integration: IntegrationCoordinator,
    manager: WorkspaceManager,
    checkpoints: CheckpointStore,
    oplog: SagaOplog,
    ledger: ScopeLedgerStore,
) -> WorkspaceRecoveryReport:
    """从账本表重建作用域账本，对操作日志与盘上事实对账；对不上账抛错（分叉即停）。"""
    report = WorkspaceRecoveryReport()
    head = await repository.resolve_ref(ROOT_INTEGRATION_REF)
    seed = await repository.resolve_ref(ROOT_SEED_REF)
    if head is None or seed is None:
        raise WorkspaceRecoveryError(
            f"私有仓根双引用缺失（head={head}，seed={seed}），无法恢复工作区树"
        )
    rows = await ledger.list_for_tree()
    if not any(row.scope_kind is ScopeKind.ROOT for row in rows):
        raise WorkspaceRecoveryError(
            f"作用域账本没有根作用域 {ledger.root_scope_id} 的行：建树未完成，无法恢复工作区树"
        )
    # 闸门状态是进程内瞬态：崩溃时可能停在静止中或已静止，重建一律归运行并写回
    records = [replace(row, gate_state=ScopeGateState.RUNNING) for row in rows]
    if any(row.gate_state is not ScopeGateState.RUNNING for row in rows):
        await ledger.upsert(records)
    try:
        await scope_tree.load_records(records)
    except ScopeTreeError as exc:
        raise WorkspaceRecoveryError(f"作用域账本对不上账：{exc}") from exc
    report.rebuilt_scope_ids = [record.scope_id for record in records]
    await _reconcile_oplog(
        oplog=oplog,
        repository=repository,
        integration=integration,
        manager=manager,
        root_scope_id=ledger.root_scope_id,
        scope_branches=_scope_branches(records),
        report=report,
    )
    report.root_epoch = scope_tree.current_root_epoch
    # 对账可能改动行（补账、后代废弃），盘上核验与降级判定取对账后的账
    settled = await scope_tree.list_all_scopes()
    for record in settled:
        if record.scope_kind is ScopeKind.ROOT:
            continue
        path, branch = _workspace_location(record)
        if record.state in _LIVE_STATES:
            await _verify_live_workspace(repository, record, path, branch)
        elif record.state in _RECLAIMED_STATES and path is not None and path.exists():
            await integration.release_worktree(path)
    await _restore_runtime_registries(manager, checkpoints, ledger.root_scope_id, settled, report)
    return report


async def _verify_live_workspace(
    repository: WorkspaceRepository,
    record: ScopeRecord,
    path: Path | None,
    branch: str,
) -> None:
    """活动与待集成作用域的盘上核验：工作区目录与分支必须在，不在即作用域损坏。"""
    if path is None or not path.exists():
        where = path if path is not None else "账本行没有工作区引用"
        raise WorkspaceRecoveryError(
            f"作用域损坏：{record.scope_id}（{record.state.value}）的工作区目录不在盘上：{where}"
        )
    # reader 快照是 detached 工作区，没有分支
    if branch and await repository.resolve_ref(f"refs/heads/{branch}") is None:
        raise WorkspaceRecoveryError(
            f"作用域损坏：{record.scope_id}（{record.state.value}）的分支不在私有仓：{branch}"
        )


async def _restore_runtime_registries(
    manager: WorkspaceManager,
    checkpoints: CheckpointStore,
    root_scope_id: str,
    records: list[ScopeRecord],
    report: WorkspaceRecoveryReport,
) -> None:
    """重建进程内登记：claims 取自检查点行的声明列，reader 快照取自没有分支段的行。"""
    live = [
        record
        for record in records
        if record.scope_kind is not ScopeKind.ROOT and record.state in _LIVE_STATES
    ]
    manager.restore_reader_scopes(
        record.scope_id for record in live if not _workspace_location(record)[1]
    )
    if not live:
        return
    # 检查点行只到根与直接子作用域；同键重建有多行 scope_start，取最近一行
    scope_starts = [
        row
        for row in await checkpoints.list_for_tree(root_scope_id)
        if row.trigger is CheckpointTrigger.SCOPE_START
    ]
    for record in live:
        row = _latest_row(scope_starts, record.scope_id)
        if row is not None and row.claims:
            manager.restore_claims(record.scope_id, row.claims)
            report.claims_restored.append(record.scope_id)
    report.degraded.append("活动组内存态未恢复：恢复后的首个副作用将开新组")


def _latest_row(rows: Sequence[ScopeCheckpoint], scope_id: str) -> ScopeCheckpoint | None:
    """作用域指定触发的最新一行；没有返回 None。"""
    matched = [row for row in rows if row.scope_id == scope_id]
    if not matched:
        return None
    return max(matched, key=lambda row: (row.created_at, row.checkpoint_id))


def _workspace_location(record: ScopeRecord) -> tuple[Path | None, str]:
    """解码行的工作区引用「路径|分支」；没有引用返回 (None, 空串)。"""
    if not record.workspace_ref:
        return None, ""
    path, _, branch = record.workspace_ref.rpartition("|")
    return Path(path), branch


def _scope_branches(records: list[ScopeRecord]) -> dict[str, str]:
    """账本内各作用域的分支名（根检出的引用不带分支段，reader 快照没有分支）。"""
    return {
        record.scope_id: branch
        for record in records
        if record.scope_kind is not ScopeKind.ROOT
        if (branch := _workspace_location(record)[1])
    }


async def _reconcile_oplog(
    *,
    oplog: SagaOplog,
    repository: WorkspaceRepository,
    integration: IntegrationCoordinator,
    manager: WorkspaceManager,
    root_scope_id: str,
    scope_branches: Mapping[str, str],
    report: WorkspaceRecoveryReport,
) -> None:
    """操作日志三态核对：在操作所在的引用上取实测头，未生效放弃、已写补账、分叉即停。

    核对通过（操作已生效）的先补账面后果再收口；补账幂等，补账后、收
    口前再崩溃由下一次恢复跳过已落账的行。
    """
    for operation in await oplog.pending_operations():
        if operation.op_kind == _RETRACT_OP_KIND:
            await _reconcile_retract(
                oplog=oplog,
                repository=repository,
                integration=integration,
                manager=manager,
                operation=operation,
                report=report,
            )
            continue
        ref, current_head = await _resolve_operation_head(
            operation,
            repository=repository,
            root_scope_id=root_scope_id,
            scope_branches=scope_branches,
        )
        decision = await oplog.resolve(
            operation.client_op_id,
            current_head=current_head,
            read_commit=integration.read_commit_facts,
        )
        if decision.action is RecoveryAction.RETRY_SAFE:
            await oplog.abandon(operation.client_op_id)
            report.abandoned_op_ids.append(operation.client_op_id)
        elif decision.action is RecoveryAction.COMPLETE_LEDGER:
            await _complete_ledger(manager, operation)
            await oplog.commit(operation.client_op_id)
            report.completed_op_ids.append(operation.client_op_id)
        else:
            raise WorkspaceRecoveryError(
                f"操作 {operation.client_op_id}（{operation.op_kind}）与引用 {ref} 对不上账："
                f"{decision.detail}"
            )


async def _complete_ledger(manager: WorkspaceManager, operation: SagaOperation) -> None:
    """已生效操作的账面后果：建区只收口不补行，集成与恢复各自补到行上。"""
    if operation.op_kind == _INTEGRATE_OP_KIND:
        await manager.complete_integration_ledger(operation)
    elif operation.op_kind == _RESTORE_OP_KIND:
        await manager.complete_restore_ledger(operation)


def _operation_ref(
    operation: SagaOperation,
    *,
    root_scope_id: str,
    scope_branches: Mapping[str, str],
) -> str | None:
    """操作对账所在的引用：已写阶段是新头所在的引用，意图阶段是旧头所在的引用。

    建区的新头落在新作用域的分支（取自建区意图），旧头在父作用域；集成
    的新旧头都在父作用域（父为根即集成线）；恢复的新旧头都在作用域自己
    的分支。定位不出返回 None。
    """
    metadata = operation.metadata
    if operation.op_kind == _RESTORE_OP_KIND:
        return _branch_ref(scope_branches.get(str(metadata.get("scope_id"))))
    if operation.op_kind == _PREPARE_OP_KIND and operation.phase is OperationPhase.GIT_WRITTEN:
        return _branch_ref(cast("str | None", metadata.get("branch")))
    if operation.op_kind in (_PREPARE_OP_KIND, _INTEGRATE_OP_KIND):
        parent_scope_id = metadata.get("parent_scope_id")
        if parent_scope_id == root_scope_id:
            return ROOT_INTEGRATION_REF
        return _branch_ref(scope_branches.get(str(parent_scope_id)) if parent_scope_id else None)
    return None


def _branch_ref(branch: str | None) -> str | None:
    """作用域分支名转为完整引用名；没有分支返回 None。"""
    return f"refs/heads/{branch}" if branch else None


async def _resolve_operation_head(
    operation: SagaOperation,
    *,
    repository: WorkspaceRepository,
    root_scope_id: str,
    scope_branches: Mapping[str, str],
) -> tuple[str, str]:
    """取操作所在引用的实测头，返回 (引用名, 头)。

    引用定位不出（操作种类未知、意图缺分支、作用域不在重建的账本内）
    或引用已被删除时无从核对，也不能证明操作未生效：显式失败，不按
    放弃或分叉处理。
    """
    ref = _operation_ref(operation, root_scope_id=root_scope_id, scope_branches=scope_branches)
    if ref is None:
        raise WorkspaceRecoveryError(
            f"操作 {operation.client_op_id}（{operation.op_kind}）所在引用无法定位"
            "（操作种类未知、意图缺分支或作用域不在重建的账本内）：停止恢复"
        )
    current_head = await repository.resolve_ref(ref)
    if current_head is None:
        raise WorkspaceRecoveryError(
            f"操作 {operation.client_op_id}（{operation.op_kind}）所在引用不存在：{ref}：停止恢复"
        )
    return ref, current_head


async def _reconcile_retract(
    *,
    oplog: SagaOplog,
    repository: WorkspaceRepository,
    integration: IntegrationCoordinator,
    manager: WorkspaceManager,
    operation: SagaOperation,
    report: WorkspaceRecoveryReport,
) -> None:
    """集成线回退的崩溃补账：已写补引用与落账、未生效放弃、分叉即停。"""
    head = await repository.resolve_ref(ROOT_INTEGRATION_REF)
    if operation.phase is not OperationPhase.GIT_WRITTEN:
        if head != operation.expected_old_head:
            raise WorkspaceRecoveryError(
                f"集成线回退 {operation.client_op_id} 未生效但线头已变化"
                f"（head={head}，expected={operation.expected_old_head}）：分叉即停"
            )
        await oplog.abandon(operation.client_op_id)
        report.abandoned_op_ids.append(operation.client_op_id)
        return
    actual = operation.actual_new_head or ""
    if head not in (actual, operation.expected_old_head):
        raise WorkspaceRecoveryError(
            f"集成线回退 {operation.client_op_id} 与线头对不上账"
            f"（head={head}，actual={actual}）：分叉即停"
        )
    # 引用推进与根检出同步幂等：落账前崩溃的两处窗口（记新头后未推进、
    # 推进后未落账）在此一并收敛
    await integration.advance_root_checkout(actual)
    await manager.complete_retraction_ledger(operation)
    report.completed_op_ids.append(operation.client_op_id)
