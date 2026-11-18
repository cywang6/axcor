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

"""hook 路由 native 权威化：内存映射先行、列兜底、最新绑定唯一持有人。"""

from __future__ import annotations

import asyncio

from workflow_controller.config.settings import AppConfig, ReviewerConfig
from workflow_controller.core.orchestrator import Orchestrator
from workflow_controller.core.runs import RunKind
from workflow_controller.session.events import Event, EventType


class TestNativeHookRouting:
    async def test_native_rebinding_latest_holder_wins(self, tmp_path):
        orch = Orchestrator(
            AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
            default_project_dir=tmp_path,
        )
        run = await orch.run_store.create("", "", kind=RunKind.CONTAINER)
        await orch.affiliation.attach("s1", run.run_id)
        await orch.affiliation.attach("s2", run.run_id)
        await orch.affiliation.set_native_session_id("s1", "native-x")
        # 同一 native id 换绑后最新持有者唯一（映射未记，走列兜底）
        await orch.affiliation.set_native_session_id("s2", "native-x")

        assert await getattr(orch, "_resolve_hook_session")("native-x") == "s2"
        rows = await orch.run_store.list_run_sessions(run.run_id)
        by_sid = {r.session_id: r for r in rows}
        assert by_sid["s1"].native_session_id is None  # 旧持有行被清
        assert by_sid["s2"].native_session_id == "native-x"

    async def test_in_memory_hit_skips_store_lookup(self, tmp_path):
        orch = Orchestrator(
            AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
            default_project_dir=tmp_path,
        )
        getattr(orch, "_cli_to_workflow_controller_session")["native-x"] = "s1"

        async def _fail(native_session_id):
            raise AssertionError("映射命中不应触发列查询")

        orch.run_store.find_session_by_native_id = _fail
        assert await getattr(orch, "_resolve_hook_session")("native-x") == "s1"

    async def test_attach_rebind_syncs_route_map(self, tmp_path):
        """attach 换绑写点同步映射：进程内路由不旧于权威列。"""
        orch = Orchestrator(
            AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
            default_project_dir=tmp_path,
        )
        r1 = await orch.run_store.create("", "", kind=RunKind.CONTAINER)
        r2 = await orch.run_store.create("", "", kind=RunKind.CONTAINER)
        await orch.affiliation.attach("s1", r1.run_id)
        await orch.affiliation.set_native_session_id("s1", "native-x")
        getattr(orch, "_cli_to_workflow_controller_session")["native-x"] = (
            "s1"  # ITERATION_START 注册后的映射状态
        )

        await orch.affiliation.attach("s2", r2.run_id, native_session_id="native-x")

        async def _fail(native_session_id):
            raise AssertionError("映射命中不应触发列查询")

        orch.run_store.find_session_by_native_id = _fail
        assert await getattr(orch, "_resolve_hook_session")("native-x") == "s2"

    async def test_attach_idempotent_reattach_heals_stale_map(self, tmp_path):
        """幂等重挂（列零变化）同样同步映射：旧值残留被纠正。"""
        orch = Orchestrator(
            AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
            default_project_dir=tmp_path,
        )
        run = await orch.run_store.create("", "", kind=RunKind.CONTAINER)
        await orch.affiliation.attach("s1", run.run_id)
        await orch.affiliation.attach("s1", run.run_id, native_session_id="native-x")

        getattr(orch, "_cli_to_workflow_controller_session")["native-x"] = (
            "stale"  # 模拟映射残留旧值
        )
        await orch.affiliation.attach("s1", run.run_id, native_session_id="native-x")

        assert getattr(orch, "_cli_to_workflow_controller_session")["native-x"] == "s1"

    async def test_register_session_persists_column_before_map(self, tmp_path):
        """列先落、映射随后：换绑窗口内映射不得新于列（重启权威源不回退旧持有）。"""
        orch = Orchestrator(
            AppConfig(project_dir=str(tmp_path), reviewer=ReviewerConfig(enabled=False)),
            default_project_dir=tmp_path,
        )
        run = await orch.run_store.create("", "", kind=RunKind.CONTAINER)
        await orch.affiliation.attach("s1", run.run_id)
        await orch.affiliation.attach("s2", run.run_id)
        await orch.affiliation.set_native_session_id("s1", "native-x")  # 旧持有

        column_written, hold = asyncio.Event(), asyncio.Event()
        inner = orch.run_store.set_session_native_id

        async def _hold_after_column_write(session_id, native_session_id):
            await inner(session_id, native_session_id)
            column_written.set()
            await hold.wait()

        orch.run_store.set_session_native_id = _hold_after_column_write

        register = asyncio.create_task(
            getattr(orch, "_register_session")(
                Event.create(
                    session_id="s2",
                    iteration=0,
                    event_type=EventType.ITERATION_START,
                    data={"session_id": "native-x"},
                )
            )
        )
        await column_written.wait()
        # 列已换绑到 s2，映射仍未先行更新
        assert getattr(orch, "_cli_to_workflow_controller_session").get("native-x") != "s2"
        hold.set()
        await register
        assert getattr(orch, "_cli_to_workflow_controller_session")["native-x"] == "s2"
