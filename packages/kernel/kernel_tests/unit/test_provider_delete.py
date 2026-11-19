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

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from workflow_controller.api.auth_api import AuthAPI, ProviderInUseError
from workflow_controller.config.settings import SessionConfig, SessionReviewerConfig
from workflow_controller.core.auth_rotator import AuthRotator
from workflow_controller.core.auth_rotator.settings import GlobalSettings
from workflow_controller.session.session import Session, SessionState

if TYPE_CHECKING:
    from pathlib import Path


def _rotator(tmp_path: Path) -> AuthRotator:
    GlobalSettings(
        api_keys=[
            {"key": "sk-old", "model": "old", "base_url": "https://old.test"},
            {
                "key": "sk-keep",
                "model": "keep",
                "base_url": "https://keep.test",
                "runtime": "claude_cli",
            },
        ]
    ).save(tmp_path / "settings.conf")
    return AuthRotator(state_path=tmp_path / "auth_state.json", env_path=tmp_path / ".env")


def _pool(sessions: list[Session], persisted: list[Session] | None = None) -> SimpleNamespace:
    """pool 替身：list_sessions + 记录终态清空后的回写调用。"""

    async def persist(session: Session) -> None:
        if persisted is not None:
            persisted.append(session)

    return SimpleNamespace(list_sessions=lambda: sessions, persist=persist)


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.to_thread 目标函数内联执行（环境 workaround）。

    本环境的默认线程池在 loop 关闭阶段退出极慢/挂住：Runner.close 的
    shutdown_default_executor 带 300s 超时（asyncio.constants.THREAD_
    JOIN_TIMEOUT），而 asyncio.run 路径无超时——main 上 tests/web/
    test_auth_routes.py 的删除用例即可复现，与本仓库代码无关。单测
    聚焦 AuthAPI 业务语义；「阻塞 I/O 必须委托线程池」契约由下方
    spy 回归用例显式验证。
    """

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline)


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_delete_provider_does_not_validate_remaining_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deleted_index: int,
) -> None:
    rotator = _rotator(tmp_path)

    async def fail_probe(*args, **kwargs):
        raise AssertionError("deletion must not probe another provider")

    monkeypatch.setattr(rotator, "validate_provider", fail_probe)
    assert rotator.delete_provider(deleted_index)
    remaining = "sk-keep" if deleted_index == 0 else "sk-old"
    assert [entry["key"] for entry in GlobalSettings.load(tmp_path / "settings.conf").api_keys] == [
        remaining
    ]
    assert [provider.key for provider in rotator.get_providers()] == [remaining]
    assert rotator.get_active_provider_index() == 0


def test_delete_refuses_stale_config_index(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    settings = GlobalSettings.load(tmp_path / "settings.conf")
    settings.api_keys[0]["model"] = "changed-elsewhere"
    settings.save(tmp_path / "settings.conf")

    assert not rotator.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_ignores_idle_session_references(tmp_path: Path) -> None:
    """历史会话不阻塞删除，删除后清空绑定（防 Run 页重启静默错绑）。

    审查场景：providers [A,B]，终态会话引用索引 1（B）。删 A 后列表变
    [B]，索引 1 越界——但若列表是 [A,B,C]，索引 1 已指向 C 且同模型
    时 resolve_available_provider 原样返回、无任何告警。清空为 None 后，
    重启时 _ensure_session_snapshots 补绑当前默认，语义正确。
    """
    rotator = _rotator(tmp_path)
    session = Session(
        config=SessionConfig(
            name="idle",
            provider_index=1,
            reviewer=SessionReviewerConfig(provider_index=0),
        ),
    )
    persisted: list[Session] = []
    pool = _pool([session], persisted)
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [p.key for p in rotator.get_providers()] == ["sk-keep"]
    assert session.config.provider_index is None
    assert session.config.reviewer is not None
    assert session.config.reviewer.provider_index is None
    assert persisted == [session]


@pytest.mark.parametrize(
    "state",
    [
        SessionState.CREATING,
        SessionState.RUNNING,
        SessionState.PAUSED,
        SessionState.WAITING_RETRY,
    ],
)
async def test_delete_refuses_running_session_reference(
    tmp_path: Path, state: SessionState
) -> None:
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="busy", provider_index=0))
    session.state = state
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


@pytest.mark.parametrize(
    "deleted_index,worker_index,reviewer_index",
    [
        (0, 1, None),  # worker 引用更高索引：删 0 → 1 下移错绑
        (0, None, 1),  # reviewer 引用更高索引：同样下移错绑
        (1, 1, None),  # 删尾部：相等引用（既有语义）
    ],
)
async def test_delete_refuses_running_session_higher_index_shift(
    tmp_path: Path,
    deleted_index: int,
    worker_index: int | None,
    reviewer_index: int | None,
) -> None:
    """回归：活跃会话引用「高于被删索引」时也必须 409。

    删除低位 provider 会让更高索引整体下移；Session.query() 每轮经
    resolve_available_provider(config.provider_index) 解析，同索引已
    指向另一个 provider → 运行中静默热错绑（模型相同时无告警回调）。
    旧区间护栏 provider_index <= index < provider_count 的下界不可丢。
    """
    rotator = _rotator(tmp_path)
    session = Session(
        config=SessionConfig(
            name="busy",
            provider_index=worker_index,
            reviewer=SessionReviewerConfig(provider_index=reviewer_index)
            if reviewer_index is not None
            else None,
        )
    )
    session.state = SessionState.RUNNING
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(deleted_index)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_allows_running_session_lower_index(
    tmp_path: Path,
) -> None:
    """活跃会话引用低于被删索引：不受移位影响，放行删除。"""
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="busy", provider_index=0))
    session.state = SessionState.RUNNING
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(1)
    assert [p.key for p in rotator.get_providers()] == ["sk-old"]


async def test_delete_refuses_real_paused_session(tmp_path: Path) -> None:
    """回归：真实暂停路径（state=IDLE + pause_requested=True）必须拦截。

    Session.pause() 把 state 置 IDLE、pause_requested 置 True；PAUSED 只在
    wait_if_paused 竞态窗口短暂出现。若仅按 state 判活跃，暂停会话被当
    历史会话跳过——恢复后 play() 置 RUNNING，下一次 query 用旧索引解析
    即静默热错绑。此处直接构造真实暂停终态字段（不调 async pause()，
    避免触 driver 停止路径）。
    """
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="paused", provider_index=1))
    # 镜像 Session.pause() 的真实落点（session.py pause()）：
    session.state = SessionState.IDLE
    session.pause_requested = True
    session.stop_reason = "user_pause"
    session.loop_status = "no"
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_refuses_real_paused_session_equal_reference(tmp_path: Path) -> None:
    """真实暂停会话引用被删索引本身（provider 用尽场景）同样拦截。"""
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="paused", provider_index=0))
    session.state = SessionState.IDLE
    session.pause_requested = True
    session.stop_reason = "user_pause"
    session.loop_status = "no"
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    with pytest.raises(ProviderInUseError, match=session.id):
        await api.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2


async def test_delete_allows_terminal_idle_without_pause_flag(tmp_path: Path) -> None:
    """真终态 idle（pause_requested=False）仍走历史会话语义：放行。"""
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="done", provider_index=1))
    session.state = SessionState.IDLE
    session.stop_reason = "completed"
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [p.key for p in rotator.get_providers()] == ["sk-keep"]


async def test_delete_allows_stopped_after_pause(tmp_path: Path) -> None:
    """回归：暂停后再停止（state=STOPPED + pause_requested 残留）不拦截。

    Session.stop() 不清 pause_requested，「先暂停再停止」（含侧栏全部
    停止 / stop-all 路由，均无状态守卫）会留下该组合。它是终态，不能
    因 pause_requested 残留被误判为活跃——否则用户按「请先停止」提示
    操作也解除不了 409（会话早已停止）。
    """
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="paused-then-stopped", provider_index=1))
    # 镜像 pause() → stop() 的真实落点：stop() 不复位 pause_requested。
    session.state = SessionState.STOPPED
    session.pause_requested = True
    session.stop_reason = "user_pause"
    session.loop_status = "no"
    persisted: list[Session] = []
    pool = _pool([session], persisted)
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [p.key for p in rotator.get_providers()] == ["sk-keep"]
    assert session.config.provider_index is None
    assert persisted == [session]


async def test_delete_allows_completed_after_pause(tmp_path: Path) -> None:
    """其他硬终态同理：pause_requested 残留不构成删除阻塞。"""
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="done", provider_index=1))
    session.state = SessionState.COMPLETED
    session.pause_requested = True
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [p.key for p in rotator.get_providers()] == ["sk-keep"]
    assert session.config.provider_index is None


async def test_delete_keeps_active_session_binding_below_deleted_index(
    tmp_path: Path,
) -> None:
    """活跃会话引用低于被删索引：绑定不受移位影响，不得被清空。"""
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="busy", provider_index=0))
    session.state = SessionState.RUNNING
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(1)
    assert [p.key for p in rotator.get_providers()] == ["sk-old"]
    # 索引 0 不在移位区间，运行中绑定原样保留
    assert session.config.provider_index == 0


async def test_delete_failure_leaves_terminal_binding_untouched(tmp_path: Path) -> None:
    """删除失败（settings.conf 与内存陈旧不一致）时不清空任何绑定。"""
    rotator = _rotator(tmp_path)
    settings = GlobalSettings.load(tmp_path / "settings.conf")
    settings.api_keys[0]["model"] = "changed-elsewhere"
    settings.save(tmp_path / "settings.conf")
    session = Session(config=SessionConfig(name="done", provider_index=0))
    session.state = SessionState.COMPLETED
    persisted: list[Session] = []
    pool = _pool([session], persisted)
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert not await api.delete_provider(0)
    assert len(GlobalSettings.load(tmp_path / "settings.conf").api_keys) == 2
    assert session.config.provider_index == 0
    assert persisted == []


async def test_delete_allows_unreferenced_later_provider(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="retained", provider_index=0))
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(1)
    assert [provider.key for provider in rotator.get_providers()] == ["sk-old"]


async def test_delete_ignores_out_of_range_session_index(tmp_path: Path) -> None:
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="stale", provider_index=5))
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [provider.key for provider in rotator.get_providers()] == ["sk-keep"]


async def test_delete_ignores_persisted_session_outside_pool(tmp_path: Path) -> None:
    """池外持久化会话（graph 成员等终态快照）不阻塞删除。"""
    rotator = _rotator(tmp_path)
    pool = _pool([])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))

    assert await api.delete_provider(0)
    assert [p.key for p in rotator.get_providers()] == ["sk-keep"]


async def test_delete_delegates_blocking_rotator_io_to_thread_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：rotator.delete_provider 的阻塞磁盘 I/O 必须走 asyncio.to_thread。

    该方法内部读写 settings.conf；若直接在事件循环内同步调用，
    auth_delete_key 路由会卡住整个服务（SSE 流 / 其他会话 / 并发请求）。
    本用例用 spy 断言委托关系而非真正起线程（后者在本环境会触发上述
    挂死）；spy 后写覆盖 autouse 的内联替身，monkeypatch 同栈按序还原。
    """
    rotator = _rotator(tmp_path)
    session = Session(config=SessionConfig(name="idle", provider_index=0))
    pool = _pool([session])
    api = AuthAPI(SimpleNamespace(auth_rotator=rotator, pool=pool))
    seen: list[tuple[object, tuple[object, ...]]] = []

    async def spy_to_thread(func, /, *args, **kwargs):
        seen.append((func, args))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    assert await api.delete_provider(0)
    assert seen == [(rotator.delete_provider, (0,))]
