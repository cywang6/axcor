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

"""驱动状态机单测：恢复归一、全新初始化与档案缺键补默认（无 asyncio）。"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

from workflow_controller.core.runs import RunStore
from workflow_controller.plugins.graph.driver.state import GraphState


def _payload(graph_status: str, node_status: str) -> dict:
    """单节点状态载荷。"""
    return {
        "graph_id": "g1",
        "status": graph_status,
        "nodes": {"A": {"status": node_status, "gen": 0}},
        "rollbacks": {},
        "rollback_edges": [],
        "retired_instances": {},
    }


def test_paused_graph_running_node_restored_as_stopped():
    """paused graph 里 running node → stopped（保住用户暂停语义）。"""
    st = GraphState("g1", "wf", {"A"}, initial_data=_payload("paused", "running"))
    assert st.status("A") == "stopped"


def test_running_graph_running_node_restored_as_stopped():
    """崩溃后先保留断点，由成员事实决定复用或重跑。"""
    st = GraphState("g1", "wf", {"A"}, initial_data=_payload("running", "running"))
    assert st.status("A") == "stopped"


def test_new_node_ids_get_default_archive():
    """yaml 变更新增的节点补默认档案，既有节点状态保留。"""
    st = GraphState("g1", "wf", {"A", "B"}, initial_data=_payload("running", "passed"))
    assert st.status("A") == "passed"
    assert st.status("B") == "pending"


def test_fresh_state_all_pending():
    st = GraphState("g1", "wf", {"A", "B"})
    assert st.snapshot()["status"] == "pending"
    assert all(st.status(n) == "pending" for n in ("A", "B"))


def test_checkpoint_runs_synchronously_after_each_mutation():
    st = GraphState("g1", "wf", {"A"})
    snapshots: list[dict] = []
    st.bind_checkpoint(lambda: snapshots.append(st.snapshot()))
    st.increment_attempts("A")
    st.set_node_status("A", "running", session_id="s1")
    st.set_verify_session_id("A", "v1")

    assert [s["nodes"]["A"]["attempts"] for s in snapshots] == [1, 1, 1]
    assert [s["nodes"]["A"]["status"] for s in snapshots] == [
        "pending",
        "running",
        "running",
    ]
    assert snapshots[-1]["nodes"]["A"]["verify_session_id"] == "v1"


def test_failure_reason_keeps_long_verifier_feedback():
    st = GraphState("g1", "wf", {"A"})
    reason = "x" * 6000 + "FAILED"
    st.set_failure_reason("A", reason)
    assert st.snapshot()["nodes"]["A"]["failure_reason"] == reason[:5000]


async def test_checkpoint_survives_sigkill_before_decide_returns(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    run = await store.create("workflow.yaml", "graph-yaml@1")
    script = """
import os, signal, sys
from pathlib import Path
from workflow_controller.core.runs import RunStore
from workflow_controller.plugins.graph.driver.state import GraphState
store = RunStore(Path(sys.argv[1]))
state = GraphState("g1", "wf", {"A"})
state.bind_checkpoint(lambda: store.save_driver_state_sync(sys.argv[2], state.snapshot()))
state.set_node_status("A", "passed")
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.db_path), run.run_id],
        check=False,
    )
    assert result.returncode == -signal.SIGKILL
    persisted = await store.load_driver_state(run.run_id)
    assert persisted["nodes"]["A"]["status"] == "passed"


def test_batch_coalesces_round_mutations_into_one_commit():
    st = GraphState("g1", "wf", {"A"})
    snapshots: list[dict] = []
    st.bind_checkpoint(lambda: snapshots.append(st.snapshot()))

    with st.batch():
        st.increment_attempts("A")
        st.set_node_status("A", "running", session_id="s1")
        st.set_verify_session_id("A", "v1")

    assert len(snapshots) == 1
    info = snapshots[0]["nodes"]["A"]
    assert info["attempts"] == 1
    assert info["status"] == "running"
    assert info["verify_session_id"] == "v1"


def test_batch_flushes_partial_changes_when_body_raises():
    """批内异常也提交已发生的变更（部分进度可恢复）。"""
    st = GraphState("g1", "wf", {"A"})
    snapshots: list[dict] = []
    st.bind_checkpoint(lambda: snapshots.append(st.snapshot()))

    with pytest.raises(RuntimeError, match="boom"), st.batch():
        st.set_node_status("A", "running", session_id="s1")
        raise RuntimeError("boom")

    assert len(snapshots) == 1
    assert snapshots[0]["nodes"]["A"]["status"] == "running"


def test_partial_archive_filled_with_defaults():
    """载荷缺键的节点档案补默认值（快照载荷随版本演进的兼容面）。"""
    st = GraphState(
        "g1",
        "wf",
        {"A"},
        initial_data={
            "graph_id": "g1",
            "status": "paused",
            "nodes": {"A": {"status": "running"}},
        },
    )
    info = st.snapshot()["nodes"]["A"]
    assert info["attempts"] == 0
    assert info["workflow_controller_session_id"] is None
    assert info["subgraph_generated"] is False


def test_rollback_reset_clears_both_session_ids():
    st = GraphState("g1", "wf", {"A"})
    st.set_node_status("A", "running", session_id="executor")
    st.set_verify_session_id("A", "verifier")
    st.reset_rollback("A", 1)
    info = st.snapshot()["nodes"]["A"]
    assert info["status"] == "pending"
    assert info["workflow_controller_session_id"] is None
    assert info["verify_session_id"] is None


def test_rollback_reset_clears_retries_and_attempts():
    """回滚重置清零重试预算与 attempt 计数，避免回滚后立即耗尽 max_retries。"""
    st = GraphState("g1", "wf", {"A"})
    st.increment_attempts("A")
    st.increment_retries("A")
    st.reset_rollback("A", 1)
    info = st.snapshot()["nodes"]["A"]
    assert info["attempts"] == 0
    assert info["retries"] == 0
