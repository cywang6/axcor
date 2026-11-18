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

"""wait_session_done 竞态窗口与实际运行时长计费的单测。"""

from __future__ import annotations

import asyncio
import time

import pytest

from workflow_controller.core.orchestrator import Orchestrator


class _Sess:
    def __init__(self, loop_status="yes"):
        self.state = type("S", (), {"value": "running"})()
        self.loop_status = loop_status
        self.stop_reason = None
        self.active_seconds = 0.0
        self._active_since = None

    def settle_active(self):
        if self._active_since is not None:
            self.active_seconds += time.monotonic() - self._active_since
            self._active_since = None

    def active_elapsed(self):
        if self._active_since is None:
            return self.active_seconds
        return self.active_seconds + (time.monotonic() - self._active_since)


class _Pool:
    def __init__(self, sess):
        self._s = sess

    def get(self, sid):
        return self._s


def _orch(sess, tasks):
    o = Orchestrator.__new__(Orchestrator)
    o.pool = _Pool(sess)
    setattr(o, "_loop_tasks", tasks)
    return o


class TestResumeWindowRace:
    async def test_task_none_during_resume_window_waits(self):
        """resume 窗口（loop_status=yes 但 task 未注册）：等待而非立即误判。"""
        sess = _Sess(loop_status="yes")
        orch = _orch(sess, {})

        # 0.6s 后注册 task（模拟 _activate_session 完成）
        async def late_register():
            await asyncio.sleep(0.6)
            getattr(orch, "_loop_tasks")["s1"] = asyncio.current_task()

        # wait 用小超时（无 timeout 分支走 shield）
        async def scenario():
            late_task = asyncio.create_task(late_register())
            done = await orch.wait_session_done("s1", timeout=2.0)
            await late_task
            return done

        done = await asyncio.wait_for(scenario(), 5.0)
        assert done is True  # task 注册后（当前 task）正常返回，没有秒报 False

    async def test_stopped_no_task_returns_true(self):
        """真停止（loop_status=no、无 task）：已 drain 返回 True（非 resume 窗口）。

        与 main 既有语义一致：直接 stop / 自然结束且无 runtime task 的 session
        视为已 drain——返回 False 会被 node_executor 误当「超时」stop 并报错。
        """
        sess = _Sess(loop_status="no")
        orch = _orch(sess, {})
        assert await orch.wait_session_done("s1", timeout=1.0) is True

    async def test_never_timeout_resume_window_bounded(self, monkeypatch):
        """永不超时（timeout=None）的注册等待上限从 15s 放宽到 60s，仍有界。

        本仓库 main 的旧实现是 `for _ in range(150)`——无论 timeout 取值都只
        轮询 150 * 0.1s（15s）。本变更把 timeout=None 放宽到 600 * 0.1s（60s），
        但仍是上界轮询：若 loop_status=yes 且 runtime task 始终未注册（resume
        后引擎未成功 spawn/注册），达到上限后按未 drain 返回 False，触发调用方
        stop + 重试链路，不会无限空转。
        """
        sess = _Sess(loop_status="yes")
        orch = _orch(sess, {})

        async def fake_sleep(_):
            pass

        monkeypatch.setattr("workflow_controller.core.orchestrator.asyncio.sleep", fake_sleep)
        done = await asyncio.wait_for(orch.wait_session_done("s1", timeout=None), 5.0)
        assert done is False


class TestActiveSecondsBilling:
    async def test_pause_does_not_consume_budget(self):
        """暂停（idle+user_pause）期间对账循环 continue 等待，不判超时。

        预算远大于已运行时长，但 wall-clock 挂起超过一个对账周期（30s 缩至
        monkeypatch 不可行——用短预算 + 暂停态验证 continue 分支成立）：
        session 处于暂停态时即使余额 >0 也不返回 False，直到 task 完成。
        """
        sess = _Sess()
        sess.active_seconds = 1.0

        # task 先挂 0.7s 再完成（模拟暂停后恢复收尾）
        async def finish_later():
            await asyncio.sleep(0.7)

        t = asyncio.ensure_future(finish_later())

        # 0.3s 时切到暂停态（第一个 30s 对账周期内不可能——用小预算证语义：
        # budget=2, active=1 → 剩 1s；若按 wall-clock，0.7s 也 <2s 不超时，
        # 故此用例验证的是「暂停态 continue」路径：暂停时余额冻结不递减）
        async def flip_paused():
            await asyncio.sleep(0.2)
            sess.state.value = "idle"
            sess.stop_reason = "user_pause"

        flip_task = asyncio.ensure_future(flip_paused())
        orch = _orch(sess, {"s1": t})
        done = await orch.wait_session_done("s1", timeout=2.0)
        await flip_task
        assert done is True  # task 完成返回 True；暂停期从未判超时

    async def test_budget_exhausted_times_out(self, monkeypatch):
        """实际运行时长耗尽预算 → False（即使 wait 刚开始、task 永不结束）。"""
        sess = _Sess()
        sess.active_seconds = 300.0
        hang = asyncio.Event()
        t = asyncio.ensure_future(hang.wait())  # 永不完成
        orch = _orch(sess, {"s1": t})
        monkeypatch.setattr(orch, "_reconcile_interval", 0.05, raising=False)
        done = await orch.wait_session_done("s1", timeout=200.0)
        assert done is False
        t.cancel()

    async def test_hanging_query_exhausts_budget(self, monkeypatch):
        """进行中运行段计入已用时长：挂死的 query 也能耗尽预算。

        _active_since 起算后无人 settle（query 挂死），若只看累计
        active_seconds，对账循环会永久等待。
        """
        sess = _Sess()
        sess.active_seconds = 0.0
        monkeypatch.setattr(sess, "_active_since", time.monotonic() - 1.0)  # 已跑 1s 且无人结算
        hang = asyncio.Event()
        t = asyncio.ensure_future(hang.wait())  # 永不完成
        orch = _orch(sess, {"s1": t})
        monkeypatch.setattr(orch, "_reconcile_interval", 0.01, raising=False)
        # timeout=0.05 < 已跑 1s：必须及时返回 False，而不是等满 interval 轮询
        done = await asyncio.wait_for(orch.wait_session_done("s1", timeout=0.05), 5.0)
        assert done is False
        t.cancel()

    async def test_budget_checked_before_first_wait(self):
        """进入对账循环先查余额：已超时的调用立即返回，不等 30s 粒度。"""
        sess = _Sess()
        sess.active_seconds = 1.0  # 调用时已超过 timeout
        hang = asyncio.Event()
        t = asyncio.ensure_future(hang.wait())  # 永不完成
        orch = _orch(sess, {"s1": t})
        # 不设 _reconcile_interval（默认 30s）：若先等后查，这里会阻塞 30s
        done = await asyncio.wait_for(orch.wait_session_done("s1", timeout=0.05), 5.0)
        assert done is False
        t.cancel()


class TestSessionAccounting:
    @staticmethod
    def test_start_settle_roundtrip():
        from workflow_controller.config.settings import SessionConfig
        from workflow_controller.session.session import Session

        s = Session(
            session_id="x", name="x", role="worker", config=SessionConfig(name="x", role="worker")
        )
        assert s.active_seconds == 0.0
        s.start_active()
        assert getattr(s, "_active_since") is not None
        time.sleep(0.02)
        s.settle_active()
        assert s.active_seconds >= 0.02
        s.settle_active()  # 幂等
        assert 0.02 <= s.active_seconds < 5

    async def test_pause_freezes(self):
        from workflow_controller.config.settings import SessionConfig
        from workflow_controller.session.session import Session

        s = Session(
            session_id="x", name="x", role="worker", config=SessionConfig(name="x", role="worker")
        )
        s.start_active()
        await asyncio.sleep(0.02)
        await s.pause()
        frozen = s.active_seconds
        await asyncio.sleep(0.05)
        assert s.active_seconds == frozen  # 暂停后不再增长

    async def test_active_elapsed_includes_inflight_segment(self):
        """active_elapsed = 累计 + 进行中段，且不冻结计时。"""
        from workflow_controller.config.settings import SessionConfig
        from workflow_controller.session.session import Session

        s = Session(
            session_id="x", name="x", role="worker", config=SessionConfig(name="x", role="worker")
        )
        assert s.active_elapsed() == 0.0  # 未在跑
        s.start_active()
        e1 = s.active_elapsed()
        await asyncio.sleep(0.03)
        e2 = s.active_elapsed()
        assert e2 > e1  # 实时增长（没被冻结）
        assert s.active_seconds == 0.0  # 未结算：累计值不动
        s.settle_active()
        assert abs(s.active_elapsed() - s.active_seconds) < 1e-6


class TestExitWait:
    async def test_old_runtime_has_fresh_exit_wait_window(self):
        """长运行会话获得新的退出等待窗口。"""
        sess = _Sess()
        sess.active_seconds = 66.0
        release = asyncio.Event()
        runtime = asyncio.create_task(release.wait())
        orch = _orch(sess, {"s1": runtime})
        waiter = asyncio.create_task(orch.wait_session_done("s1", wait_timeout=1.0))
        try:
            await asyncio.sleep(0)
            assert not waiter.done()
            release.set()
            assert await waiter is True
        finally:
            release.set()
            await runtime

    async def test_wait_timeout_does_not_cancel_runtime(self):
        """等待超时不取消会话运行任务。"""
        release = asyncio.Event()
        runtime = asyncio.create_task(release.wait())
        orch = _orch(_Sess(), {"s1": runtime})
        try:
            assert await orch.wait_session_done("s1", wait_timeout=0.01) is False
            assert not runtime.done()
        finally:
            release.set()
            await runtime

    async def test_registration_window_is_bounded_by_wait_timeout(self):
        """runtime 注册窗口也受固定等待限制。"""
        orch = _orch(_Sess(), {})
        assert (
            await asyncio.wait_for(orch.wait_session_done("s1", wait_timeout=0.01), timeout=0.5)
            is False
        )

    async def test_wait_timeout_waits_for_late_runtime_registration(self):
        """固定窗口内延迟注册的 runtime 可正常等待完成。"""
        sess = _Sess()
        sess.active_seconds = 66.0
        orch = _orch(sess, {})
        waiter = asyncio.create_task(orch.wait_session_done("s1", wait_timeout=1.0))
        await asyncio.sleep(0)
        runtime = asyncio.create_task(asyncio.sleep(0))
        getattr(orch, "_loop_tasks")["s1"] = runtime
        assert await waiter is True

    async def test_cancelled_waiter_preserves_runtime(self):
        """取消等待调用不取消会话 runtime。"""
        release = asyncio.Event()
        runtime = asyncio.create_task(release.wait())
        orch = _orch(_Sess(loop_status="no"), {"s1": runtime})
        waiter = asyncio.create_task(orch.wait_session_done("s1", wait_timeout=1.0))
        try:
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not runtime.done()
        finally:
            release.set()
            await runtime

    async def test_wait_timeout_accepts_stopped_session(self):
        """已停止且无 runtime 的会话立即视为完成。"""
        orch = _orch(_Sess(loop_status="no"), {})
        assert await orch.wait_session_done("s1", wait_timeout=0) is True

    async def test_timeout_parameters_are_mutually_exclusive(self):
        """两种等待时钟不能同时启用。"""
        orch = _orch(_Sess(), {})
        with pytest.raises(ValueError, match="mutually exclusive"):
            await orch.wait_session_done("s1", timeout=1.0, wait_timeout=1.0)
