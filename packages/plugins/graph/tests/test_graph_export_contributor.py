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

"""graph 导出贡献者测试：纯函数直测（快照+事件+冻结程序 → 贡献）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from workflow_controller.core.runs import RunRecord
from workflow_controller.plugin.run_export import RunExportRequest
from workflow_controller.plugins.graph.exporter import build_contribution

RUN_ID = "r1"


def _run() -> RunRecord:
    return RunRecord(run_id=RUN_ID, program_ref="wf.yaml", format="graph-yaml@1")


def _freeze_program(tmp_path, nodes: list[dict]) -> Any:
    program_dir = tmp_path / "program"
    program_dir.mkdir(parents=True)
    (program_dir / "wf.yaml").write_text(
        yaml.safe_dump({"workflow": "demo", "nodes": nodes}),
        encoding="utf-8",
    )
    (program_dir / ".workflow-controller-entry").write_text("wf.yaml\n", encoding="utf-8")
    return program_dir


def _event(
    seq: int,
    nid: str,
    gen: int,
    attempt: int,
    status: str,
    *,
    sid: str | None = None,
    verify: dict | None = None,
    output: str | None = None,
) -> dict[str, Any]:
    return {
        "id": seq,
        "source": "run_events",
        "run_id": RUN_ID,
        "context": None,
        "timestamp": f"2026-10-19T10:00:{seq:02d}",
        "event_type": "node_history",
        "data": {
            "graph_id": "g",
            "node_id": nid,
            "attempt": attempt,
            "status": status,
            "workflow_controller_session_id": sid,
            "failure_reason": None,
            "output": output,
            "started_at": f"2026-10-19T10:00:{seq:02d}",
            "finished_at": f"2026-10-19T10:01:{seq:02d}",
            "verify_type": (verify or {}).get("verify_type"),
            "verify_session_id": (verify or {}).get("verify_session_id"),
            "verify_reason": (verify or {}).get("verify_reason"),
            "gen": gen,
        },
    }


def _outcome(seq: int, sid: str, kind: str) -> dict[str, Any]:
    return {
        "id": seq,
        "source": "events",
        "run_id": RUN_ID,
        "session_id": sid,
        "context": None,
        "timestamp": f"2026-10-19T10:02:{seq:02d}",
        "event_type": "session_outcome",
        "data": {"kind": kind},
    }


def _request(
    program_dir: Any,
    events: list[dict],
    snapshot: dict | None = None,
) -> RunExportRequest:
    return RunExportRequest(
        run=_run(),
        members=(),
        roster={},
        events=events,
        driver_snapshot=snapshot,
        driver_projection=None,
        program_dir=program_dir,
    )


def _graph_of(member: Any) -> dict[str, Any]:
    return member.fields


def _by_key(members: list[Any]) -> dict[tuple[str, int, int, str], Any]:
    return {
        (
            _graph_of(m)["nodeId"],
            _graph_of(m)["generation"],
            _graph_of(m)["attempt"],
            m.annotation["role"],
        ): m
        for m in members
        if m.annotation and "role" in m.annotation
    }


def _basic_nodes() -> list[dict]:
    """验收/重试用例的三节点清单：agent 验收（可重试）+ script 验收 + 裸 agent。"""
    return [
        {
            "id": "build",
            "task_type": "agent",
            "depends_on": [],
            "acceptance": {
                "enabled": True,
                "type": "agent",
                "verifier": "v",
                "verifier_prompt": "p",
                "max_retries": 2,
                "on_exhaust": "stop",
                "rollback_to": "",
            },
        },
        {
            "id": "setup",
            "task_type": "bash",
            "depends_on": ["build"],
            "command": "true",
            "acceptance": {
                "enabled": True,
                "type": "script",
                "script": "true",
                "max_retries": 0,
                "on_exhaust": "stop",
                "rollback_to": "",
            },
        },
        {"id": "live", "task_type": "agent", "depends_on": ["setup"]},
    ]


def _basic_events() -> list[dict]:
    """build 两次尝试（失败/通过）+ setup 完成 + live 运行中的事件与 outcome。"""
    return [
        _event(1, "build", 0, 1, "running", sid="ob1"),
        _event(
            2,
            "build",
            0,
            1,
            "failed",
            verify={
                "verify_type": "agent",
                "verify_session_id": "vb1",
                "verify_reason": "缺少边界用例",
            },
        ),
        _event(3, "build", 0, 2, "running", sid="ob2"),
        _event(
            4,
            "build",
            0,
            2,
            "passed",
            verify={"verify_type": "agent", "verify_session_id": "vb2", "verify_reason": ""},
        ),
        _event(
            5,
            "setup",
            0,
            1,
            "passed",
            output="ready",
            verify={"verify_type": "script", "verify_session_id": None, "verify_reason": "exit 0"},
        ),
        _event(
            6,
            "live",
            0,
            1,
            "running",
            sid="ob9",
            verify={"verify_type": "agent", "verify_session_id": "vb9", "verify_reason": "验收中…"},
        ),
        _outcome(7, "ob1", "failed"),
        _outcome(8, "vb1", "completed"),
        _outcome(9, "ob2", "completed"),
        _outcome(10, "vb2", "completed"),
        _outcome(11, "ob9", "completed"),
    ]


def test_basic_attempts_verification_and_output(tmp_path) -> None:
    """成员五键、attemptResult、agent/script 验收、retryPolicy、bash 输出。"""
    program = _freeze_program(tmp_path, _basic_nodes())
    events = _basic_events()
    snapshot = {
        "workflow": "demo",
        "state": {"nodes": {}, "retired_instances": {}, "rollback_edges": []},
    }
    contribution = build_contribution(_request(program, events, snapshot))
    assert contribution.name == "demo"
    keyed = _by_key(contribution.members)
    build_a1 = keyed[("build", 0, 1, "execute")]
    assert build_a1.id == "s_ob1" and build_a1.kind == "session"
    assert build_a1.fields["executionState"] == "finished"
    assert "role" not in build_a1.fields
    assert _graph_of(build_a1)["attemptResult"] == "failed"
    assert _graph_of(build_a1)["retryPolicy"] == {
        "maxRetries": 2,
        "onExhaust": "stop",
    }
    assert keyed[("build", 0, 1, "verify")].id == "s_vb1"
    verify_ann = _graph_of(keyed[("build", 0, 1, "verify")])
    assert verify_ann["verification"] == {
        "type": "agent",
        "status": "failed",
        "reason": "缺少边界用例",
    }
    build_a2 = keyed[("build", 0, 2, "execute")]
    assert _graph_of(build_a2)["attemptResult"] == "succeeded"
    assert "retryPolicy" not in _graph_of(build_a2)  # 只挂首条执行成员
    setup = keyed[("setup", 0, 1, "execute")]
    assert setup.kind == "process" and setup.output_text == "ready"
    assert _graph_of(keyed[("setup", 0, 1, "verify")])["verification"] == {
        "type": "script",
        "status": "passed",
        "reason": "exit 0",
    }
    live_verify = _graph_of(keyed[("live", 0, 1, "verify")])
    assert live_verify["executionState"] == "in_progress"
    assert live_verify["verification"] == {"type": "agent", "status": "running"}
    assert _graph_of(keyed[("live", 0, 1, "execute")])["executionState"] == "finished"
    assert "attemptResult" not in _graph_of(keyed[("live", 0, 1, "execute")])
    # 依赖边 A 端取末次验收成员
    edges = {(r.from_id, r.to_id): r for r in contribution.relations}
    assert (build_a2.id, "s_vb2") in edges
    assert ("s_vb2", setup.id) in edges


def test_execution_state_uses_snapshot_status_when_history_is_missing(tmp_path) -> None:
    program = _freeze_program(
        tmp_path,
        [
            {"id": "check", "task_type": "bash", "command": "true"},
        ],
    )
    for status, expected in (
        ("running", "in_progress"),
        ("passed", "finished"),
        ("stopped", "in_progress"),
    ):
        snapshot = {
            "workflow": "demo",
            "state": {
                "nodes": {"check": {"gen": 0, "status": status, "attempts": 1}},
                "retired_instances": {},
                "rollback_edges": [],
            },
        }
        member = _by_key(build_contribution(_request(program, [], snapshot)).members)[
            ("check", 0, 1, "execute")
        ]
        assert member.fields["executionState"] == expected
        assert ("attemptResult" in member.fields) == (status == "passed")


def test_session_lifecycle_is_independent_of_node_status(tmp_path) -> None:
    program = _freeze_program(
        tmp_path,
        [
            {"id": "step", "task_type": "agent"},
            {"id": "paused", "task_type": "agent"},
        ],
    )
    events = [
        _event(1, "step", 0, 1, "running", sid="exec"),
        _event(2, "paused", 0, 1, "stopped", sid="hold"),
        _outcome(3, "exec", "failed"),
        _outcome(4, "hold", "paused"),
    ]
    keyed = _by_key(build_contribution(_request(program, events)).members)
    failed = keyed[("step", 0, 1, "execute")]
    paused = keyed[("paused", 0, 1, "execute")]
    assert failed.fields["executionState"] == "finished"
    assert failed.finished_at == events[2]["timestamp"]
    assert paused.fields["executionState"] == "in_progress"
    assert paused.finished_at is None


def test_session_lifecycle_uses_roster_when_outcome_event_is_missing(tmp_path) -> None:
    program = _freeze_program(
        tmp_path,
        [
            {"id": "step", "task_type": "agent"},
        ],
    )
    request = _request(program, [_event(1, "step", 0, 1, "running", sid="exec")])
    request = _with_roster(request, {"exec": {"loop_status": "no", "stop_reason": "error"}})
    member = _by_key(build_contribution(request).members)[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"
    assert member.finished_at is None


def test_execution_state_defers_to_node_status_when_roster_not_terminal(tmp_path) -> None:
    """名册非终态（如恢复库 user_pause）但节点已终态时，executionState 收口为 finished。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _request(program, [_event(1, "step", 0, 1, "passed", sid="exec")])
    request = _with_roster(request, {"exec": {"loop_status": "no", "stop_reason": "user_pause"}})
    member = _by_key(build_contribution(request).members)[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"


def _rollback_bad_node() -> dict:
    """回滚验收配方的 bad 节点（agent 验收、耗尽即回滚到 step）。"""
    return {
        "id": "bad",
        "task_type": "agent",
        "depends_on": ["step"],
        "acceptance": {
            "enabled": True,
            "type": "agent",
            "verifier": "v",
            "verifier_prompt": "p",
            "max_retries": 0,
            "on_exhaust": "rollback",
            "rollback_to": "step",
        },
    }


def _bad_verify_fail_event() -> dict:
    """bad 节点 agent 验收不过的 node_history 事件（回滚现场公共形状）。"""
    return _event(
        4,
        "bad",
        0,
        1,
        "failed",
        verify={
            "verify_type": "agent",
            "verify_session_id": "vb1",
            "verify_reason": "验收不过",
        },
    )


def _with_roster(request: RunExportRequest, roster: dict) -> RunExportRequest:
    """在既有请求上覆写 roster（其余字段原样透传）。"""
    return RunExportRequest(
        run=request.run,
        members=request.members,
        roster=roster,
        events=request.events,
        driver_snapshot=request.driver_snapshot,
        driver_projection=request.driver_projection,
        program_dir=request.program_dir,
    )


def _agent_verify_request(program_dir: Any) -> RunExportRequest:
    """单 agent 节点 + 验收会话事实的基线请求（verify 时间导出用例公共形状）。"""
    return _request(
        program_dir,
        [
            _event(
                1,
                "step",
                0,
                1,
                "passed",
                sid="exec",
                verify={"verify_type": "agent", "verify_session_id": "vchk", "verify_reason": ""},
            ),
        ],
    )


def _rollback_snapshot() -> dict:
    """回滚用例驱动快照：gen1 在册、step/bad 的 gen0 已退役、一条回滚边。"""
    return {
        "workflow": "demo",
        "state": {
            "nodes": {
                "step": {"gen": 1, "status": "passed", "attempts": 1, "output": "v2"},
                "bad": {"gen": 1, "status": "failed", "attempts": 1},
                "solo": {"gen": 0, "status": "passed", "attempts": 1, "output": "s0"},
                "fresh": {"gen": 0, "status": "pending", "attempts": 0},
            },
            "retired_instances": {
                "step#0": {
                    "id": "step",
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "output": "v1",
                },
                "bad#0": {"id": "bad", "gen": 0, "status": "failed", "attempts": 1},
            },
            "rollback_edges": [{"from": "bad", "from_gen": 0, "to": "step", "to_gen": 1}],
        },
    }


def test_retry_rollback_carried_and_placeholders(tmp_path) -> None:
    """多 attempt、retired 代实例、回滚边、carried 与未运行占位。"""
    program = _freeze_program(
        tmp_path,
        [
            {"id": "step", "task_type": "bash", "depends_on": [], "command": "true"},
            _rollback_bad_node(),
            {"id": "solo", "task_type": "bash", "depends_on": [], "command": "true"},
            {"id": "fresh", "task_type": "agent", "depends_on": []},
        ],
    )
    events = [
        _event(1, "step", 0, 1, "passed", output="v1"),
        _event(2, "solo", 0, 1, "passed", output="s0"),
        _event(3, "bad", 0, 1, "running", sid="ob1"),
        _bad_verify_fail_event(),
        _event(5, "step", 1, 1, "passed", output="v2"),
        _event(6, "bad", 1, 1, "running", sid="ob2"),
        _event(
            7,
            "bad",
            1,
            1,
            "failed",
            verify={"verify_type": "agent", "verify_session_id": "vb2", "verify_reason": "仍不过"},
        ),
    ]
    contribution = build_contribution(_request(program, events, _rollback_snapshot()))
    keyed = _by_key(contribution.members)
    # retired 代实例保留、输出分代
    assert keyed[("step", 0, 1, "execute")].output_text == "v1"
    assert keyed[("step", 1, 1, "execute")].output_text == "v2"
    assert keyed[("step", 0, 1, "execute")].id != keyed[("step", 1, 1, "execute")].id
    # carried：solo 未被 reset，gen1 沿用 gen0 终态结果
    carried = keyed[("solo", 1, 0, "execute")]
    assert carried.kind == "process" and carried.session_id is None
    assert carried.fields["executionState"] == "carried"
    assert carried.fields["resultFromGeneration"] == 0
    # 未运行占位：fresh 在两代都是 attempt=0 空位
    assert keyed[("fresh", 0, 0, "execute")].kind == "session"
    assert keyed[("fresh", 0, 0, "execute")].session_id is None
    assert keyed[("fresh", 0, 0, "execute")].fields["executionState"] == "pending"
    assert ("fresh", 1, 0, "execute") in keyed
    # 回滚边：失败验收成员 → 新代目标执行成员
    rollback = [r for r in contribution.relations if r.fields.get("type") == "rollback"]
    assert [(r.from_id, r.to_id) for r in rollback] == [
        ("s_vb1", keyed[("step", 1, 1, "execute")].id)
    ]
    assert rollback[0].fields == {"type": "rollback"}


def _nested_subgraph_events() -> list[dict]:
    """gen 两代执行及其子节点事件（gen0: x/y，gen1: x/y/z）+ after。"""
    return [
        _event(1, "gen", 0, 1, "failed", output="retry"),
        _event(2, "gen", 0, 2, "passed", output="gen0"),
        _event(3, "gen:x", 0, 1, "passed", output="x0"),
        _event(4, "gen:y", 0, 1, "passed", output="y0"),
        _event(5, "gen", 1, 1, "passed", output="gen1"),
        _event(6, "gen:x", 1, 1, "passed", output="x1"),
        _event(7, "gen:y", 1, 1, "passed", output="y1"),
        _event(8, "gen:z", 1, 1, "passed", output="z1"),
        _event(9, "after", 1, 1, "passed", output="a1"),
    ]


def _gen_node_state(gen: int, attempts: int, output: str, deps: dict[str, list[str]]) -> dict:
    """子图 generator 节点状态：subgraph_node_ids/nodes 由「子节点→依赖」映射派生。"""
    return {
        "gen": gen,
        "status": "passed",
        "attempts": attempts,
        "output": output,
        "subgraph_generated": True,
        "subgraph_node_ids": list(deps),
        "subgraph_nodes": [
            {"id": nid, "depends_on": dep, "task_type": "bash"} for nid, dep in deps.items()
        ],
    }


def _nested_subgraph_snapshot() -> dict:
    """两代子图嵌套用例快照：gen0 拓扑 x→y→w（w 未跑），gen1 拓扑 x,y→z。"""
    return {
        "workflow": "demo",
        "state": {
            "nodes": {
                "gen": _gen_node_state(
                    1, 1, "gen1", {"gen:x": [], "gen:y": [], "gen:z": ["gen:y"]}
                ),
                "gen:x": {"gen": 1, "status": "passed", "attempts": 1, "output": "x1"},
                "gen:y": {"gen": 1, "status": "passed", "attempts": 1, "output": "y1"},
                "gen:z": {"gen": 1, "status": "passed", "attempts": 1, "output": "z1"},
                "after": {"gen": 1, "status": "passed", "attempts": 1, "output": "a1"},
            },
            "retired_instances": {
                "gen#0": {"id": "gen"}
                | _gen_node_state(
                    0, 2, "gen0", {"gen:x": [], "gen:y": ["gen:x"], "gen:w": ["gen:y"]}
                ),
                "gen:x#0": {
                    "id": "gen:x",
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "output": "x0",
                },
                "gen:y#0": {
                    "id": "gen:y",
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "output": "y0",
                },
            },
            "rollback_edges": [],
        },
    }


def test_subgraph_nested_workflow_and_topology_isolation(tmp_path) -> None:
    """每次执行各有一个子图 workflow；归档代拓扑不吃当前代。"""
    program = _freeze_program(
        tmp_path,
        [
            {"id": "gen", "task_type": "subgraph", "depends_on": [], "file": ".workflow/sub.yaml"},
            {"id": "after", "task_type": "bash", "depends_on": ["gen"], "command": "true"},
        ],
    )
    contribution = build_contribution(
        _request(program, _nested_subgraph_events(), _nested_subgraph_snapshot())
    )
    keyed = _by_key(contribution.members)
    failed = keyed[("gen", 0, 1, "execute")]
    first = keyed[("gen", 0, 2, "execute")]
    second = keyed[("gen", 1, 1, "execute")]
    assert failed.kind == first.kind == second.kind == "workflow"
    assert len({failed.id, first.id, second.id}) == 3
    assert first.fields["nodeId"] == second.fields["nodeId"] == "gen"
    assert all(m.session_id is None and m.output_text is None for m in (failed, first, second))
    assert contribution.nested[failed.id].members == ()
    first_nested = contribution.nested[first.id]
    second_nested = contribution.nested[second.id]
    sub_keyed = {
        **_by_key(first_nested.members),
        **_by_key(second_nested.members),
    }
    # 全部代的子节点实例都在嵌套层；w 已知而未跑 → 占位，z 首见于 gen1
    assert sub_keyed.get(("gen:x", 0, 1, "execute")).output_text == "x0"
    assert sub_keyed.get(("gen:w", 0, 0, "execute")).kind == "process"
    assert ("gen:z", 0, 0, "execute") not in sub_keyed
    assert sub_keyed.get(("gen:z", 1, 1, "execute")).output_text == "z1"
    first_edges = {(r.from_id, r.to_id) for r in first_nested.relations}
    second_edges = {(r.from_id, r.to_id) for r in second_nested.relations}
    x0, y0 = (
        sub_keyed.get(("gen:x", 0, 1, "execute")).id,
        sub_keyed.get(("gen:y", 0, 1, "execute")).id,
    )
    y1, z1 = (
        sub_keyed.get(("gen:y", 1, 1, "execute")).id,
        sub_keyed.get(("gen:z", 1, 1, "execute")).id,
    )
    assert (x0, y0) in first_edges
    assert (y1, z1) in second_edges
    assert (x0, y1) not in first_edges | second_edges
    assert not any(r.fields.get("type") == "gen-sub-graph" for r in contribution.relations)
    after_exec = keyed[("after", 1, 1, "execute")]
    assert (second.id, after_exec.id) in {(r.from_id, r.to_id) for r in contribution.relations}


def _running_generator_snapshot() -> dict:
    """generator 运行中 + 并行分支回滚出 gen1 的快照（缺成员代不索引）。"""
    return {
        "workflow": "demo",
        "state": {
            "nodes": {
                "gen": {
                    "gen": 0,
                    "status": "running",
                    "attempts": 1,
                    "subgraph_generated": True,
                    "subgraph_node_ids": ["gen:x"],
                    "subgraph_nodes": [
                        {"id": "gen:x", "depends_on": [], "task_type": "bash"},
                    ],
                },
                "gen:x": {"gen": 0, "status": "passed", "attempts": 1, "output": "x0"},
                "step": {"gen": 1, "status": "passed", "attempts": 1, "output": "v1"},
                "bad": {"gen": 0, "status": "failed", "attempts": 1},
            },
            "retired_instances": {
                "step#0": {
                    "id": "step",
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "output": "v1",
                },
            },
            "rollback_edges": [{"from": "bad", "from_gen": 0, "to": "step", "to_gen": 1}],
        },
    }


def test_subgraph_generator_running_while_sibling_rolls_back(tmp_path) -> None:
    """子图 generator 非终态运行中、并行分支回滚出更高代时不崩（缺成员代不索引）。"""
    program = _freeze_program(
        tmp_path,
        [
            {"id": "gen", "task_type": "subgraph", "depends_on": [], "file": ".workflow/sub.yaml"},
            {"id": "step", "task_type": "bash", "depends_on": [], "command": "true"},
            _rollback_bad_node(),
        ],
    )
    events = [
        _event(1, "gen", 0, 1, "running"),
        _event(2, "gen:x", 0, 1, "passed", output="x0"),
        _event(3, "step", 0, 1, "passed", output="v1"),
        _bad_verify_fail_event(),
    ]
    contribution = build_contribution(_request(program, events, _running_generator_snapshot()))
    keyed = _by_key(contribution.members)
    gen_exec = keyed[("gen", 0, 1, "execute")]
    assert gen_exec.kind == "workflow"
    nested = contribution.nested[gen_exec.id]
    assert _by_key(list(nested.members))[("gen:x", 0, 1, "execute")].output_text == "x0"
    # gen 在 gen1 缺成员（最近实例 running 非终态跳过生成），子节点 carried 成员
    # 仍须有 workflow 容器兜底，否则会被静默丢弃
    placeholder = keyed[("gen", 1, 0, "execute")]
    assert placeholder.kind == "workflow"
    assert placeholder.fields["executionState"] == "pending"
    carried = _by_key(list(contribution.nested[placeholder.id].members))[("gen:x", 1, 0, "execute")]
    assert carried.kind == "process"
    assert carried.fields["executionState"] == "carried"
    assert carried.fields["resultFromGeneration"] == 0


def test_subgraph_edge_referencing_main_node_is_skipped(tmp_path) -> None:
    """子图拓扑边指向作用域外成员时跳过该边，不 fail-closed。"""
    program = _freeze_program(
        tmp_path,
        [
            {"id": "gen", "task_type": "subgraph", "depends_on": [], "file": ".workflow/sub.yaml"},
            {"id": "main", "task_type": "bash", "depends_on": [], "command": "true"},
        ],
    )
    events = [
        _event(1, "gen", 0, 1, "passed"),
        _event(2, "gen:x", 0, 1, "passed", output="x0"),
        _event(3, "main", 0, 1, "passed", output="m0"),
    ]
    snapshot = {
        "workflow": "demo",
        "state": {
            "nodes": {
                "gen": {
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "subgraph_generated": True,
                    "subgraph_node_ids": ["gen:x"],
                    "subgraph_nodes": [
                        {"id": "gen:x", "depends_on": ["main"], "task_type": "bash"},
                    ],
                },
                "gen:x": {"gen": 0, "status": "passed", "attempts": 1, "output": "x0"},
                "main": {"gen": 0, "status": "passed", "attempts": 1, "output": "m0"},
            },
            "retired_instances": {},
            "rollback_edges": [],
        },
    }
    contribution = build_contribution(_request(program, events, snapshot))
    workflow = next(m for m in contribution.members if m.kind == "workflow")
    nested = contribution.nested[workflow.id]
    assert _by_key(list(nested.members))[("gen:x", 0, 1, "execute")].output_text == "x0"
    assert nested.relations == ()


def test_execute_uses_session_own_time_not_node_history(tmp_path) -> None:
    """execute 成员时间取执行会话自身 created_at/last_activity，不取 node_history。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _request(program, [_event(1, "step", 0, 1, "passed", sid="exec")])
    request = _with_roster(
        request,
        {
            "exec": {
                "loop_status": "no",
                "stop_reason": "error",
                "created_at": "2026-10-19T10:00:00",
                "last_activity": "2026-10-19T10:01:30",
            }
        },
    )
    member = _by_key(build_contribution(request).members)[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"
    assert member.started_at == "2026-10-19T10:00:00"
    assert member.finished_at == "2026-10-19T10:01:30"


def test_execute_without_roster_row_exports_none_timestamps(tmp_path) -> None:
    """roster 无 exec 行（会话已删除的归档 run）时 execute 时间导出 None，不回退 node_history。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _request(program, [_event(1, "step", 0, 1, "passed", sid="exec")])
    request = _with_roster(request, {})
    member = _by_key(build_contribution(request).members)[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"
    assert member.started_at is None
    assert member.finished_at is None


def test_execute_without_session_id_exports_none_timestamps(tmp_path) -> None:
    """node_history 事实缺 sid（快照重建/事件残缺）时 execute 时间导出 None，不回退 node_history。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _request(program, [_event(1, "step", 0, 1, "passed")])
    member = _by_key(build_contribution(request).members)[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"
    assert member.started_at is None
    assert member.finished_at is None


def test_verify_finished_at_uses_verify_session_not_execute(tmp_path) -> None:
    """verify 成员 finished_at 取验收会话自身 last_activity，不拷 execute 的 finished_at。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _agent_verify_request(program)
    request = _with_roster(
        request,
        {
            "exec": {"loop_status": "no", "stop_reason": "completed"},
            "vchk": {
                "created_at": "2026-10-19T10:02:00",
                "last_activity": "2026-10-19T10:03:00",
                "loop_status": "no",
                "stop_reason": "completed",
            },
        },
    )
    verify = _by_key(build_contribution(request).members)[("step", 0, 1, "verify")]
    assert verify.started_at == "2026-10-19T10:02:00"
    assert verify.finished_at == "2026-10-19T10:03:00"


def test_verify_without_roster_row_exports_none_timestamps(tmp_path) -> None:
    """roster 无 vchk 行（会话已删除的归档 run）时 verify 时间导出 None，不回退 execute 时间。"""
    program = _freeze_program(tmp_path, [{"id": "step", "task_type": "agent"}])
    request = _agent_verify_request(program)
    request = _with_roster(request, {"exec": {"loop_status": "no", "stop_reason": "completed"}})
    verify = _by_key(build_contribution(request).members)[("step", 0, 1, "verify")]
    assert verify.started_at is None
    assert verify.finished_at is None


def test_agent_without_session_derives_state_from_status(tmp_path) -> None:
    """agent 节点缺 session（降级路径）时 executionState 取自节点 status。"""
    program = _freeze_program(
        tmp_path,
        [
            {"id": "step", "task_type": "agent"},
        ],
    )
    member = _by_key(
        build_contribution(
            _request(program, [_event(1, "step", 0, 1, "passed")]),
        ).members
    )[("step", 0, 1, "execute")]
    assert member.fields["executionState"] == "finished"
    assert member.fields["attemptResult"] == "succeeded"


def _generator_session_snapshot() -> dict:
    """generator 会话与生成子图分离用例的快照（make-yaml:child 单节点）。"""
    return {
        "workflow": "demo",
        "state": {
            "nodes": {
                "make-yaml": {
                    "gen": 0,
                    "status": "passed",
                    "attempts": 1,
                    "workflow_controller_session_id": "ob-generator",
                    "subgraph_generated": True,
                    "subgraph_node_ids": ["make-yaml:child"],
                    "subgraph_nodes": [
                        {"id": "make-yaml:child", "depends_on": [], "task_type": "bash"},
                    ],
                },
                "make-yaml:child": {"gen": 0, "status": "passed", "attempts": 1},
                "after": {"gen": 0, "status": "passed", "attempts": 1},
            },
            "retired_instances": {},
            "rollback_edges": [],
        },
    }


def test_generator_session_stays_separate_from_generated_workflow(tmp_path) -> None:
    program = _freeze_program(
        tmp_path,
        [
            {"id": "make-yaml", "task_type": "generator", "depends_on": []},
            {"id": "after", "task_type": "bash", "depends_on": ["make-yaml"], "command": "true"},
        ],
    )
    events = [
        _event(1, "make-yaml", 0, 1, "passed", sid="ob-generator"),
        _event(2, "make-yaml:child", 0, 1, "passed", output="child"),
        _event(3, "after", 0, 1, "passed"),
    ]
    contribution = build_contribution(_request(program, events, _generator_session_snapshot()))
    keyed = _by_key(contribution.members)
    generator = keyed[("make-yaml", 0, 1, "execute")]
    assert generator.kind == "session" and generator.session_id == "ob-generator"
    workflow = next(m for m in contribution.members if m.kind == "workflow")
    assert workflow.session_id is None and workflow.output_text is None
    child = _by_key(contribution.nested[workflow.id].members)[("make-yaml:child", 0, 1, "execute")]
    assert child.kind == "process"
    assert (generator.id, workflow.id) in {
        (r.from_id, r.to_id)
        for r in contribution.relations
        if r.fields.get("type") == "gen-sub-graph"
    }
    assert (workflow.id, keyed[("after", 0, 1, "execute")].id) in {
        (r.from_id, r.to_id) for r in contribution.relations
    }


def _gdict(member: dict) -> dict:
    """workflow.json 成员的 graph 定位字段（端到端用例）。"""
    return member


async def test_end_to_end_export_through_run_plane(tmp_path) -> None:
    """真 run 全链路：bash 工作流跑到终态，经协调器导出并通过校验。"""
    from .run_plane_helpers import (
        GraphRunPlaneEnv,
        await_run_status,
    )

    env = GraphRunPlaneEnv(tmp_path)
    yaml_path = env.write_workflow(
        [
            {
                "id": "first",
                "task_type": "bash",
                "command": "echo first-ok",
                "acceptance": {"enabled": True, "type": "script", "script": "true"},
            },
            {
                "id": "second",
                "task_type": "bash",
                "command": "echo second-ok",
                "depends_on": ["first"],
            },
        ]
    )
    run_id = await env.start_graph_run(yaml_path=yaml_path, label="export-e2e")
    await await_run_status(env, run_id, {"completed"})
    result = await env.coord.export_run(run_id)
    assert result["ok"] and result["exportDataComplete"] is True
    assert result["members"] >= 3 and result["relations"] >= 2
    bundle = Path(result["path"])
    from workflow_controller.core.run_export import validate_bundle

    validate_bundle(bundle)
    doc = json.loads((bundle / "workflow.json").read_text(encoding="utf-8"))
    assert doc["name"] == "switch"  # yaml workflow 名（贡献者给）
    members = {
        (_gdict(m)["nodeId"], m["annotation"]["role"]): m
        for m in doc["data"]["members"]
        if m["kind"] != "workflow"
    }
    first_exec = members[("first", "execute")]
    assert "role" not in first_exec
    assert first_exec["executionState"] == "finished"
    assert first_exec["output"]["text"].startswith("first-ok")
    assert _gdict(members[("first", "verify")])["verification"]["type"] == "script"
    # 重导出幂等：workflow.json 字节一致
    again = await env.coord.export_run(run_id)
    assert (Path(again["path"]) / "workflow.json").read_bytes() == (
        bundle / "workflow.json"
    ).read_bytes()
