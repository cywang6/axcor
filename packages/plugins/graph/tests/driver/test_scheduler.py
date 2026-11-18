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

"""调度决策单测：就绪/传播/清扫、回滚数学与子图恢复策略（无 asyncio）。"""

from __future__ import annotations

from workflow_controller.plugins.graph.driver import scheduler
from workflow_controller.plugins.graph.driver.dag import DAG
from workflow_controller.plugins.graph.driver.state import GraphState


def _dag(nodes: list[dict]) -> DAG:
    return DAG(nodes)


def _state(node_ids: set[str]) -> GraphState:
    return GraphState("g1", "wf", node_ids)


def _gen_subgraph_failed() -> tuple[DAG, GraphState]:
    """generator 已 passed、子图已并入、bad 失败的回滚现场。"""
    dag = _dag(
        [
            {"id": "gen", "task_type": "generator"},
            {"id": "gen:s1", "depends_on": ["gen"]},
            {"id": "bad", "depends_on": ["gen"]},
        ]
    )
    st = _state({"gen", "gen:s1", "bad"})
    st.set_node_status("gen", "passed")
    st.set_node_status("gen:s1", "passed")
    st.set_subgraph_node_ids("gen", ["gen:s1"])
    st.set_node_status("bad", "failed")
    return dag, st


class TestDeriveGraphStatus:
    @staticmethod
    def test_running_wins_over_failed():
        snap = {
            "status": "running",
            "nodes": {"A": {"status": "running"}, "B": {"status": "failed"}},
        }
        assert scheduler.derive_graph_status(snap) == "running"

    @staticmethod
    def test_running_next():
        snap = {
            "status": "pending",
            "nodes": {"A": {"status": "running"}, "B": {"status": "stopped"}},
        }
        assert scheduler.derive_graph_status(snap) == "running"

    @staticmethod
    def test_stopped_maps_to_paused():
        snap = {"status": "running", "nodes": {"A": {"status": "stopped"}}}
        assert scheduler.derive_graph_status(snap) == "paused"

    @staticmethod
    def test_all_passed_completed():
        snap = {
            "status": "running",
            "nodes": {"A": {"status": "passed"}, "B": {"status": "skipped"}},
        }
        assert scheduler.derive_graph_status(snap) == "completed"

    @staticmethod
    def test_only_pending_keeps_fresh_running():
        # graph 刚置 running 而节点尚未启动（agents 预备轮）不可降级回 pending
        snap = {"status": "running", "nodes": {"A": {"status": "pending"}}}
        assert scheduler.derive_graph_status(snap) == "running"

    @staticmethod
    def test_only_pending_keeps_paused():
        snap = {"status": "paused", "nodes": {"A": {"status": "pending"}}}
        assert scheduler.derive_graph_status(snap) == "paused"

    @staticmethod
    def test_after_rollback_defaults_running():
        snap = {"status": "failed", "nodes": {"A": {"status": "pending"}}}
        assert scheduler.derive_status_after_rollback(snap) == "running"

    @staticmethod
    def test_after_rollback_keeps_real_failed():
        snap = {
            "status": "running",
            "nodes": {"A": {"status": "pending"}, "B": {"status": "failed"}},
        }
        assert scheduler.derive_status_after_rollback(snap) == "failed"

    @staticmethod
    def test_after_rollback_running_wins_over_failed():
        snap = {
            "status": "failed",
            "nodes": {"A": {"status": "running"}, "B": {"status": "failed"}},
        }
        assert scheduler.derive_status_after_rollback(snap) == "running"


class TestSelectReadyNodes:
    @staticmethod
    def test_pending_with_terminal_deps():
        dag = _dag([{"id": "A"}, {"id": "B", "depends_on": ["A"]}])
        st = _state({"A", "B"})
        st.set_node_status("A", "passed")
        assert [n["id"] for n in scheduler.select_ready_nodes(dag, st)] == ["B"]

    @staticmethod
    def test_dep_not_passed_not_ready():
        dag = _dag([{"id": "A"}, {"id": "B", "depends_on": ["A"]}])
        st = _state({"A", "B"})
        st.set_node_status("A", "failed")
        assert scheduler.select_ready_nodes(dag, st) == []

    @staticmethod
    def test_propagated_skip_does_not_unlock_normal_downstream():
        dag = _dag([{"id": "A"}, {"id": "B", "depends_on": ["A"]}])
        st = _state({"A", "B"})
        st.set_node_status("A", "skipped")

        assert scheduler.select_ready_nodes(dag, st) == []
        assert scheduler.propagate_failure_skips(dag, st)
        assert st.status("B") == "skipped"

    @staticmethod
    def test_executed_continue_skip_unlocks_normal_downstream():
        dag = _dag([{"id": "A"}, {"id": "B", "depends_on": ["A"]}])
        st = _state({"A", "B"})
        st.increment_attempts("A")
        st.set_node_status("A", "skipped")

        assert [n["id"] for n in scheduler.select_ready_nodes(dag, st)] == ["B"]
        assert not scheduler.propagate_failure_skips(dag, st)

    @staticmethod
    def test_run_on_failure_bypasses_dep_gate():
        dag = _dag([{"id": "A"}, {"id": "F", "depends_on": ["A"], "run_on_failure": True}])
        st = _state({"A", "F"})
        st.set_node_status("A", "failed")
        assert [n["id"] for n in scheduler.select_ready_nodes(dag, st)] == ["F"]

    @staticmethod
    def test_not_pending_excluded():
        dag = _dag([{"id": "A"}])
        st = _state({"A"})
        st.set_node_status("A", "running")
        assert scheduler.select_ready_nodes(dag, st) == []


class TestPropagateFailureSkips:
    @staticmethod
    def test_blocked_normal_node_skipped():
        dag = _dag(
            [
                {"id": "A"},
                {"id": "M", "depends_on": ["A"]},
                {"id": "F", "depends_on": ["M"], "run_on_failure": True},
            ]
        )
        st = _state({"A", "M", "F"})
        st.set_node_status("A", "failed")
        assert scheduler.propagate_failure_skips(dag, st) is True
        assert st.status("M") == "skipped"
        # run_on_failure 节点不被动 skip——等依赖终态后自行就绪
        assert st.status("F") == "pending"

    @staticmethod
    def test_all_passed_no_progress():
        dag = _dag([{"id": "A"}, {"id": "B", "depends_on": ["A"]}])
        st = _state({"A", "B"})
        st.set_node_status("A", "passed")
        assert scheduler.propagate_failure_skips(dag, st) is False


class TestSkipPendingOnFailure:
    @staticmethod
    def test_pending_marked_skipped():
        dag = _dag([{"id": "A"}, {"id": "B"}, {"id": "C"}])
        st = _state({"A", "B", "C"})
        st.set_node_status("A", "failed")
        st.set_node_status("B", "passed")
        scheduler.skip_pending_on_failure(dag, st)
        assert st.status("B") == "passed"
        assert st.status("C") == "skipped"


class TestPlanAndApplyRollback:
    @staticmethod
    def test_subgraph_rollback_resets_parent_gen():
        dag, st = _gen_subgraph_failed()
        plan = scheduler.plan_rollback(st, dag, "gen:s1", "bad", 0)
        assert plan is not None
        assert plan.parent_gen_node == "gen"
        assert plan.reset_nodes == {"gen", "gen:s1", "bad"}
        # 子图内回滚不重新生成：父 generator 名下子图不进废弃清单
        assert plan.discard_subgraphs == {}
        scheduler.apply_rollback(st, dag, plan)
        # gen-sub 保持 passed 不重跑，子图节点原地重置待重跑
        assert st.status("gen") == "passed"
        assert st.status("gen:s1") == "pending"
        assert "gen:s1" in dag.all_ids()
        assert st.subgraph_node_ids("gen") == ["gen:s1"]
        snap = st.snapshot()
        assert snap["rollback_edges"] == [{"from": "gen", "from_gen": 0, "to": "gen", "to_gen": 1}]

    @staticmethod
    def test_main_graph_rollback_to_generator_discards_subgraph():
        dag, st = _gen_subgraph_failed()
        plan = scheduler.plan_rollback(st, dag, "gen", "bad", 0)
        assert plan is not None
        assert plan.parent_gen_node is None
        # 回滚到 generator 重跑：旧子图进废弃清单
        assert plan.discard_subgraphs == {"gen": ["gen:s1"]}
        scheduler.apply_rollback(st, dag, plan)
        assert "gen:s1" not in dag.all_ids()
        assert st.subgraph_node_ids("gen") == []
        assert st.status("gen") == "pending"

    @staticmethod
    def _failed_setup():
        dag = _dag(
            [
                {"id": "A"},
                {"id": "B", "depends_on": ["A"]},
                {"id": "C", "depends_on": ["B"]},
            ]
        )
        st = _state({"A", "B", "C"})
        st.set_node_status("A", "passed")
        st.set_node_status("B", "failed")
        st.set_node_status("C", "pending")
        return dag, st

    def test_stale_result_rejected(self):
        dag, st = self._failed_setup()
        # 失败节点已被重置（他者先应用回滚）→ 陈旧结果丢弃
        st.set_node_status("B", "pending")
        assert scheduler.plan_rollback(st, dag, "A", "B", 0) is None

    def test_main_graph_plan_shape(self):
        dag, st = self._failed_setup()
        plan = scheduler.plan_rollback(st, dag, "A", "B", 0)
        assert plan is not None
        assert plan.reset_nodes == {"A", "B", "C"}
        assert plan.new_gen == 1
        assert plan.parent_gen_node is None
        assert plan.stop_nodes == set()

    def test_running_sibling_in_stop_set(self):
        dag, st = self._failed_setup()
        st.set_node_status("C", "running", session_id="sess-C")
        plan = scheduler.plan_rollback(st, dag, "A", "B", 0)
        assert plan is not None
        assert plan.stop_nodes == {"C"}

    def test_apply_resets_and_archives(self):
        dag, st = self._failed_setup()
        plan = scheduler.plan_rollback(st, dag, "A", "B", 0)
        scheduler.apply_rollback(st, dag, plan)
        assert all(st.status(n) == "pending" for n in ("A", "B", "C"))
        assert st.gen("A") == 1 and st.gen("B") == 1
        snap = st.snapshot()
        # 旧实例归档（failed 的 B、passed 的 A）
        assert "B#0" in snap["retired_instances"] and "A#0" in snap["retired_instances"]
        # 回滚边 + 预算计数
        assert snap["rollback_edges"] == [{"from": "B", "from_gen": 0, "to": "A", "to_gen": 1}]
        assert st.rollback_left("B", "A", 2) == 1


class TestSubgraphRestore:
    @staticmethod
    def test_restored_plan_id_mismatch_rejected():
        sub = {"nodes": [{"id": "other"}]}
        assert scheduler.restored_subgraph_plan("gen", ["gen:s1"], sub) is None

    @staticmethod
    def test_restored_plan_invalid_config_rejected():
        assert scheduler.restored_subgraph_plan("gen", ["gen:s1"], None) is None
        assert scheduler.restored_subgraph_plan("gen", ["gen:s1"], {"nodes": []}) is None

    @staticmethod
    def _merged():
        dag = _dag(
            [
                {"id": "gen", "task_type": "generator", "subgraph": {"output": "sub.yaml"}},
                {"id": "bad", "depends_on": ["gen"]},
            ]
        )
        st = _state({"gen", "bad"})
        st.set_node_status("gen", "passed")
        st.set_subgraph_node_ids("gen", ["gen:s1"])
        return dag, st

    def test_spec_finds_unmerged_generator(self):
        dag, st = self._merged()
        specs = scheduler.subgraph_restore_specs(dag, st, {})
        assert len(specs) == 1
        assert specs[0].node["id"] == "gen"
        assert specs[0].sub_ids == ["gen:s1"]

    def test_spec_renders_output_fallback(self):
        dag, st = self._merged()
        st.set_subgraph_node_ids("gen", [])
        st.set_subgraph_output("gen", "persisted.yaml")
        st.set_subgraph_node_ids("gen", ["gen:s1"])
        specs = scheduler.subgraph_restore_specs(dag, st, {})
        assert specs[0].output_rel == "persisted.yaml"

    def test_already_merged_skipped(self):
        dag, st = self._merged()
        dag.add_nodes(
            [{"id": "gen:s1", "depends_on": []}],
            parent_node_id="gen",
        )
        assert scheduler.subgraph_restore_specs(dag, st, {}) == []

    def test_fail_unrestorable_all_passed_noop(self):
        dag, st = self._merged()
        st.add_node("gen:s1")
        st.set_node_status("gen:s1", "passed")
        scheduler.fail_unrestorable_subgraph(st, dag, "gen", ["gen:s1"])
        assert st.snapshot()["status"] == "pending"

    def test_fail_unrestorable_marks_graph_failed(self):
        dag, st = self._merged()
        st.add_node("gen:s1")
        st.set_node_status("gen:s1", "pending")
        scheduler.fail_unrestorable_subgraph(st, dag, "gen", ["gen:s1"])
        assert st.snapshot()["status"] == "failed"
        assert st.status("gen") == "failed"
        assert st.subgraph_node_ids("gen") == []
        assert "gen:s1" not in dag.all_ids()

    def test_merge_restored_rebuilds_topology(self):
        dag, st = self._merged()
        prefixed = [{"id": "gen:s1", "depends_on": []}]
        scheduler.merge_restored_subgraph(st, dag, "gen", prefixed)
        assert "gen:s1" in dag.all_ids()
        topology = st.subgraph_nodes("gen")
        assert topology == [
            {"id": "gen:s1", "depends_on": [], "task_type": "bash", "task_description": ""}
        ]


class TestValidateSubgraphConfig:
    @staticmethod
    def test_missing_nodes_rejected():
        ok, why = scheduler.validate_subgraph_config({}, "wf")
        assert not ok
        assert "nodes" in why

    @staticmethod
    def test_on_exhaust_out_of_vocab_rejected():
        config = {"nodes": [{"id": "a", "acceptance": {"on_exhaust": "halt"}}]}
        ok, why = scheduler.validate_subgraph_config(config, "wf")
        assert not ok
        assert "on_exhaust" in why

    @staticmethod
    def test_on_exhaust_in_vocab_accepted():
        config = {"nodes": [{"id": "a", "acceptance": {"on_exhaust": "rollback"}}]}
        ok, _ = scheduler.validate_subgraph_config(config, "wf")
        assert ok

    @staticmethod
    def test_on_exhaust_thin_node_rejected():
        config = {"nodes": [{"id": "a", "on_exhaust": "halt"}]}
        ok, why = scheduler.validate_subgraph_config(config, "wf")
        assert not ok
        assert "on_exhaust" in why
