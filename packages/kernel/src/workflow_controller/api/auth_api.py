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

"""Auth API — 认证管理面门面。

所有方法返回纯数据结构；rotator 缺席时沿用各端点的空载形状。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_controller.core.orchestrator import Orchestrator

_EMPTY_KEY_STATUS: dict[str, Any] = {
    "mode": "API",
    "reset_at": None,
    "providers": [],
    "active_provider_index": 0,
}


class ProviderInUseError(ValueError):
    """A retained session still refers to a provider index that would move."""


class AuthAPI:
    """收口 key 状态、激活、验证与全局认证设置。"""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    @staticmethod
    def _raise_if_index_would_shift(
        provider_index: int,
        session_id: str,
        indices: list[int | None],
        provider_count: int,
    ) -> None:
        # 只拦删除会使「当前仍有效」的索引前移的引用；越界索引（provider_count
        # 内不存在）本就失效，删除不会让它错绑到别的 key/model，忽略之。
        blocking = next(
            (
                index
                for index in indices
                if index is not None and provider_index <= index < provider_count
            ),
            None,
        )
        if blocking is not None:
            raise ProviderInUseError(
                f"session {session_id} references provider index {blocking} "
                f"(deleting index {provider_index} would shift it)"
            )

    @staticmethod
    def _persisted_provider_indices(row: dict[str, Any]) -> list[int | None]:
        raw = row.get("config_json")
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                cfg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        else:
            cfg = raw
        if not isinstance(cfg, dict):
            return []
        indices: list[int | None] = [cfg.get("provider_index")]
        reviewer = cfg.get("reviewer")
        if isinstance(reviewer, dict):
            indices.append(reviewer.get("provider_index"))
        return indices

    def key_status(self) -> dict[str, Any]:
        """key 轮换状态载荷。"""
        rotator = self._rotator()
        if rotator is None:
            return dict(_EMPTY_KEY_STATUS)
        return rotator.get_key_status_dict()

    def keys_for_ui(self) -> dict[str, Any]:
        """UI 展示用的 key 清单。"""
        rotator = self._rotator()
        if rotator is None:
            return {"keys": []}
        return {"keys": rotator.get_api_keys_for_ui()}

    async def set_keys_validated(self, keys: list[dict[str, str]]) -> dict[str, Any]:
        """批量写入已验证 key；非法输入抛 ValueError 由路由映射 400。"""
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        await rotator.set_keys_validated(keys)
        return {"ok": True}

    async def delete_provider(self, provider_index: int) -> bool:
        rotator = self._rotator()
        if rotator is None:
            raise RuntimeError("auth rotator not available")
        if not 0 <= provider_index < rotator.get_provider_count():
            return False
        provider_count = rotator.get_provider_count()
        for session in self._orch.pool.list_sessions():
            config = session.config
            reviewer_index = config.reviewer.provider_index if config.reviewer is not None else None
            self._raise_if_index_would_shift(
                provider_index,
                session.id,
                [config.provider_index, reviewer_index],
                provider_count,
            )
        # 池外持久化会话（graph 成员完成/被丢弃后仍保留快照，恢复时重新绑定索引）
        event_router = getattr(self._orch, "event_router", None)
        if event_router is not None:
            for row in await asyncio.to_thread(event_router.list_sessions_sync):
                self._raise_if_index_would_shift(
                    provider_index,
                    row.get("id", ""),
                    self._persisted_provider_indices(row),
                    provider_count,
                )
        return await asyncio.to_thread(rotator.delete_provider, provider_index)

    def activate_provider(self, index: Any) -> dict[str, Any]:
        """按序号激活 provider。"""
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        if not isinstance(index, int) or index < 0:
            return {"ok": False, "error": "invalid index"}
        ok = rotator.activate_provider(index)
        return {"ok": ok, "active_index": rotator.get_active_provider_index()}

    async def test_active(self) -> dict[str, Any]:
        """重跑当前 provider 的 worker/reviewer 校验。"""
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        if rotator.get_provider_count() == 0:
            return {"success": False, "error": "No provider configured"}
        return await rotator.validate_provider(
            rotator.get_active_provider_index(),
            force=True,
        )

    async def test_provider(self, provider_index: int) -> dict[str, Any]:
        """强制重测指定 provider 的两种能力。"""
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        if not 0 <= provider_index < rotator.get_provider_count():
            raise ValueError(f"invalid provider index: {provider_index}")
        return await rotator.validate_provider(provider_index, force=True)

    async def validate_candidate(
        self,
        key: str,
        model: str,
        base_url: str,
        runtime: str | None,
        model_catalog_path: str = "",
    ) -> dict[str, Any]:
        """试测 key/model/base_url/runtime 组合，不写入 provider 列表。

        opencode 探测不依赖 project cwd：provider 定义经 OPENCODE_CONFIG_CONTENT
        注入子进程 env，临时目录即可测；探测到的 kind 由后端存 auth_state.json。
        """
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        if not key:
            return {"success": False, "error": "Key is required"}
        if not runtime:
            raise ValueError("runtime is required")
        return await rotator.validate_candidate(
            key,
            model,
            base_url,
            runtime,
            model_catalog_path=model_catalog_path,
            force=True,
        )

    def global_settings(self) -> dict[str, Any]:
        """全局认证设置载荷。"""
        rotator = self._rotator()
        if rotator is None:
            return {}
        return rotator.get_global_settings_dict()

    def set_global_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """更新全局认证设置。"""
        rotator = self._rotator()
        if rotator is None:
            return {"error": "auth rotator not available"}
        rotator.set_global_settings(**settings)
        return {"ok": True}

    def _rotator(self) -> Any:
        return getattr(self._orch, "auth_rotator", None)
