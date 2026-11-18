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

"""run 归属——缓存、显式挂靠与隐式铸造。

每个 session 恰好属于一个 run（恒等式）。归属写点收敛为三段：
- 显式（config.run_id）：经激活后挂靠钩子在 _runtime_lock 临界区内落账；
- 谱系派生（handoff 接续等）：派生写点把父归属透传进 config.run_id，
  走显式段入同一 run；
- 其余：session 创建入口铸隐式 run（kind=implicit，状态=成员投影）。

归属缓存（session→(run_id, context)）是事件打戳的唯一入口；重启经
run_sessions（权威）与 sessions.config_json（旁证）双源重建，冲突以
run_sessions 为准。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow_controller.core.runs import RunKind, RunNotFoundError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from workflow_controller.config.settings import SessionConfig
    from workflow_controller.core.runs import RunStore
    from workflow_controller.session.manager import SessionPool
    from workflow_controller.session.session import Session

    AuditFn = Callable[[str, str], Awaitable[None]]
    NativeBoundFn = Callable[[str, str], None]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionRunAffiliation:
    """session 的 run 归属（run_sessions 行的内存投影）。"""

    run_id: str
    role: str = "executor"
    context: str | None = None


class SessionRunAffiliationService:
    """归属唯一写点：显式 attach / 隐式 mint + 归属缓存维护。"""

    def __init__(
        self,
        run_store: RunStore,
        *,
        audit: AuditFn | None = None,
        on_native_bound: NativeBoundFn | None = None,
    ) -> None:
        """audit 仅在新落行时触发（幂等 no-op 不发）；缓存以 session_id 为键。

        on_native_bound 在 attach 带 native id 落账后同步（路由映射随列
        换绑更新，列先落、映射随后）。
        """
        self._run_store = run_store
        self._audit = audit
        self._on_native_bound = on_native_bound
        self._cache: dict[str, SessionRunAffiliation] = {}

    def resolve(self, session_id: str) -> SessionRunAffiliation | None:
        """打戳热路径：内存 dict 查找，零 join。"""
        return self._cache.get(session_id)

    async def attach(
        self,
        session_id: str,
        run_id: str,
        *,
        role: str = "executor",
        context: str | None = None,
        native_session_id: str | None = None,
    ) -> bool:
        """唯一显式归属写点。

        无归属 → 落行 + run_attached 审计；已属本 run → 幂等 no-op（不重复
        发审计）；已属其他 run → 冲突报错（转移走 adopt/detach，不静默串台）。
        """
        inserted = await self._run_store.attach_session(
            run_id,
            session_id,
            role=role,
            context=context,
            native_session_id=native_session_id,
        )
        if native_session_id is not None and self._on_native_bound is not None:
            # 幂等重挂同样同步：列写短路不代表映射已与列一致（可能仍持旧值）
            self._on_native_bound(native_session_id, session_id)
        if inserted:
            self._cache[session_id] = SessionRunAffiliation(
                run_id=run_id, role=role, context=context
            )
            if self._audit is not None:
                await self._audit(session_id, run_id)
        elif context is not None and session_id in self._cache:
            cached = self._cache[session_id]
            if cached.context != context:
                # 幂等重挂补记 context：激活钩子先挂（不带 context），分派方
                # 随后带 context 重挂——事件打戳与结局回执都从缓存取 context，
                # 丢了它消费端对不上账；账面行的 context 由 set_session_context
                # 单独写，这里只修缓存投影
                self._cache[session_id] = SessionRunAffiliation(
                    run_id=cached.run_id, role=cached.role, context=context
                )
        return inserted

    async def set_context(self, session_id: str, context: str | None) -> None:
        """成员标签（分派 context）更新：账面行与缓存投影同写。

        事件打戳与结局回执都从归属缓存取 context——只写行不写缓存，消费端
        （驱动按 context 对账）会对旧标签，换代场景（RestartSession 带新
        context 重启成员）实测踩出。无归属（缓存缺）静默跳过：没有投影可写。
        """
        aff = self._cache.get(session_id)
        if aff is None:
            return
        await self._run_store.set_session_context(aff.run_id, session_id, context)
        self._cache[session_id] = SessionRunAffiliation(
            run_id=aff.run_id, role=aff.role, context=context
        )

    async def set_native_session_id(self, session_id: str, native_session_id: str) -> None:
        """native 会话 id 记账（hook 路由权威源）；无归属静默跳过。"""
        if session_id not in self._cache:
            return
        await self._run_store.set_session_native_id(session_id, native_session_id)

    async def on_session_created(
        self,
        session: Session,
        config: SessionConfig,
        *,
        roster: dict | None = None,
    ) -> bool:
        """session 创建入口的隐式铸造三段规则。

        显式段（config.run_id 在）留给激活后挂靠钩子——激活失败半截不进
        账面，sessions 表行由 pool 独立写（返回 False）；谱系段（handoff 接续等派生）
        由派生写点把父归属透传进 config.run_id，同样走显式段；其余在此铸
        隐式 run 并回写归属，sessions 表行随 mint 同事务落账（返回 True）。
        名册 config_json 的 run_id 旁证由 mint 在事务内注入（首次落库即带归属）；
        回写让后续谱系派生看到归属。
        """
        if config.run_id is not None:
            return False
        run_id = await self.mint_for(
            session.id,
            role=config.role,
            label=config.name,
            created_at=session.created_at,
            roster=roster,
        )
        config.run_id = run_id
        return True

    async def mint_for(
        self,
        session_id: str,
        *,
        role: str,
        label: str,
        created_at: datetime | None = None,
        roster: dict | None = None,
    ) -> str:
        """直接铸隐式归属（session 创建入口与 boot 补铸共用）。"""
        run_id = await self._run_store.mint_implicit(
            session_id=session_id,
            role=role,
            label=label,
            created_at=created_at,
            roster=roster,
        )
        self._cache[session_id] = SessionRunAffiliation(run_id=run_id, role=role)
        return run_id

    async def discard(
        self,
        session_id: str,
        *,
        delete_roster: bool = False,
    ) -> str | None:
        """回滚清理：归属行与无成员隐式 run 一起删，缓存驱逐。

        delete_roster=True 时 sessions 表行同事务删除（删除/回滚路径）。
        """
        run_id = await self._run_store.discard_affiliation(
            session_id,
            delete_roster=delete_roster,
        )
        self._cache.pop(session_id, None)
        return run_id

    def evict(self, session_id: str) -> None:
        """仅驱逐缓存（归属行保留的删除路径，如显式 run 成员）。"""
        self._cache.pop(session_id, None)

    async def rebuild(self) -> None:
        """缓存重建：run_sessions 为权威；config_json 旁证由补铸消费。"""
        rows = await self._run_store.list_run_affiliations()
        self._cache = {
            row.session_id: SessionRunAffiliation(
                run_id=row.run_id,
                role=row.role,
                context=row.context,
            )
            for row in rows
        }

    async def recover_at_boot(self, pool: SessionPool) -> None:
        """boot 归属恢复：缓存双源重建 + 崩溃窗口补铸（sessions 表行在、归属缺）
        + 正向孤儿回收（归属行在、sessions 表行缺且属隐式 run）。

        名册为空不触账面（懒建不落盘）；组合根在 orch.start() 之后、run
        恢复之前排序调用——归属缓存须先于一切 session 活动就绪。
        """
        sessions = pool.list_sessions()
        if not sessions:
            return
        await self.rebuild()
        for session in sessions:
            current = self.resolve(session.id)
            if current is not None:
                # sessions 表 config_json 与账面冲突时以 run_sessions 为准
                if session.config.run_id != current.run_id:
                    session.config.run_id = current.run_id
                    await pool.persist(session)
                continue
            await self._backfill(pool, session)
        # 正向孤儿（mint 已提交、sessions 表写入未及的崩溃窗口遗留）按删除同款
        # 语义回收：隐式归属才 discard，显式 run 的成员行是调用方的记录
        roster = {s.id for s in sessions}
        for row in await self._run_store.list_run_affiliations():
            if row.session_id not in roster and (
                (await self._run_store.get(row.run_id)).kind == RunKind.IMPLICIT
            ):
                await self.discard(row.session_id)

    async def _backfill(self, pool: SessionPool, session: Session) -> None:
        """按三段规则幂等补铸：config.run_id 可用 → attach；否则铸隐式。"""
        run_id = session.config.run_id
        if run_id is not None:
            try:
                await self.attach(session.id, run_id, role=session.config.role)
                return
            except ValueError:
                # 已属其他 run：账面权威，跳过待人工裁定，强铸会撞 UNIQUE(session_id)
                logger.warning(
                    "补铸：session=%s 已属其他 run，跳过待人工裁定",
                    session.id,
                )
                return
            except RunNotFoundError:
                logger.warning(
                    "补铸：run_id=%s 不在账面（session=%s），改铸隐式 run",
                    run_id,
                    session.id,
                )
        session.config.run_id = await self.mint_for(
            session.id,
            role=session.config.role,
            label=session.name,
            created_at=session.created_at,
        )
        await pool.persist(session)
