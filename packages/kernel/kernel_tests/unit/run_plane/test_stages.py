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

"""阶段约定共享求值（workflow_controller.plugin.stages）——规范化/展示、门禁、阶段派生纯函数。"""

from __future__ import annotations

from workflow_controller.plugin.stages import (
    artifacts_display,
    completed_prefix,
    declared_stages,
    detect_jump,
    evaluate_gate,
    gate_command,
    gate_exists,
    gate_non_placeholder,
    normalize_artifacts,
)

# ── declared_stages ────────────────────────────────────────────────────


def test_declared_stages_keeps_only_valid_entries():
    """混合有效/无效声明：缺 name、缺/空 artifacts、非 dict 条目均被滤除。"""
    valid = {"name": "spec", "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]}
    assert declared_stages(
        [
            valid,
            {"name": "", "artifacts": [{"artifact": "x.md", "gate": "exists"}]},
            {"name": "plan"},
            {"name": "dev", "artifacts": []},
            "not-a-dict",
        ]
    ) == [valid]


def test_declared_stages_all_invalid_returns_empty():
    """声明非空但全无效 → []：消费方据此惰性（plugin 告警「N entries but 0 valid」）。"""
    assert declared_stages([{"name": "spec"}, {"artifacts": [{"artifact": "x.md"}]}, 42]) == []


def test_declared_stages_empty_input():
    assert declared_stages([]) == []


# ── normalize_artifacts / artifacts_display ────────────────────────────


def test_normalize_single_entry():
    stage = {
        "name": "spec",
        "artifacts": [
            {"artifact": "spec.md", "gate": "non_placeholder"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0] == {"artifact": "spec.md", "gate": "non_placeholder", "command": ""}


def test_normalize_multiple_entries():
    stage = {
        "name": "dev",
        "artifacts": [
            {"artifact": "docs/LOG.md", "gate": "non_placeholder"},
            {"artifact": "issues/", "gate": "exists"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 2
    assert result[0] == {"artifact": "docs/LOG.md", "gate": "non_placeholder", "command": ""}
    assert result[1] == {"artifact": "issues", "gate": "exists", "command": ""}


def test_normalize_shorthand_artifact_list_expands():
    """entry.artifact 是列表 → 展开为多条共享同一 gate。"""
    stage = {
        "name": "design",
        "artifacts": [
            {"artifact": ["DESIGN.md", "PLAN.md", "TEST.md"], "gate": "non_placeholder"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 3
    assert result[0]["artifact"] == "DESIGN.md"
    assert result[0]["gate"] == "non_placeholder"
    assert result[1]["artifact"] == "PLAN.md"
    assert result[1]["gate"] == "non_placeholder"
    assert result[2]["artifact"] == "TEST.md"
    assert result[2]["gate"] == "non_placeholder"


def test_normalize_mixed_shorthand_and_single():
    stage = {
        "name": "verify",
        "artifacts": [
            {"artifact": "verify.md", "gate": "non_placeholder"},
            {"artifact": ["test_a.py", "test_b.py"], "gate": "command", "command": "pytest -q"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 3
    assert result[0] == {"artifact": "verify.md", "gate": "non_placeholder", "command": ""}
    assert result[1] == {"artifact": "test_a.py", "gate": "command", "command": "pytest -q"}
    assert result[2] == {"artifact": "test_b.py", "gate": "command", "command": "pytest -q"}


def test_normalize_command_gate_entry():
    stage = {
        "name": "verify",
        "artifacts": [
            {"artifact": "impl/", "gate": "command", "command": "pytest -q"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0] == {"artifact": "impl", "gate": "command", "command": "pytest -q"}


def test_normalize_command_without_command_is_skipped():
    """gate=command 但没有 command 字段 → 跳过。"""
    stage = {
        "name": "bad",
        "artifacts": [
            {"artifact": "x.md", "gate": "command"},  # 缺 command
            {"artifact": "y.md", "gate": "non_placeholder"},  # 有效
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0]["artifact"] == "y.md"


def test_normalize_missing_gate_is_skipped():
    """entry 缺 gate → 跳过。"""
    stage = {
        "name": "bad",
        "artifacts": [
            {"artifact": "x.md"},  # 缺 gate
            {"artifact": "y.md", "gate": "non_placeholder"},  # 有效
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0]["artifact"] == "y.md"


def test_normalize_invalid_gate_value_is_skipped():
    """gate 值不合法 → 跳过。"""
    stage = {
        "name": "bad",
        "artifacts": [
            {"artifact": "x.md", "gate": "bad_mode"},
            {"artifact": "y.md", "gate": "non_placeholder"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0]["artifact"] == "y.md"


def test_normalize_empty_artifacts_list():
    assert normalize_artifacts({"name": "x", "artifacts": []}) == []


def test_normalize_missing_artifacts_key():
    assert normalize_artifacts({"name": "x"}) == []


def test_normalize_artifacts_not_a_list():
    assert normalize_artifacts({"name": "x", "artifacts": "not_a_list"}) == []


def test_normalize_strips_trailing_slash():
    stage = {
        "name": "execute",
        "artifacts": [
            {"artifact": "impl/", "gate": "non_placeholder"},
        ],
    }
    result = normalize_artifacts(stage)
    assert result[0]["artifact"] == "impl"


def test_normalize_empty_artifact_string_skipped():
    stage = {
        "name": "bad",
        "artifacts": [
            {"artifact": "   ", "gate": "non_placeholder"},
            {"artifact": "ok.md", "gate": "exists"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0]["artifact"] == "ok.md"


def test_normalize_non_dict_entry_skipped():
    stage = {
        "name": "x",
        "artifacts": [
            "just_a_string",
            {"artifact": "ok.md", "gate": "non_placeholder"},
        ],
    }
    result = normalize_artifacts(stage)
    assert len(result) == 1
    assert result[0]["artifact"] == "ok.md"


def test_display_single():
    stage = {
        "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}],
    }
    assert artifacts_display(stage) == "spec.md"


def test_display_two():
    stage = {
        "artifacts": [
            {"artifact": "spec.md", "gate": "non_placeholder"},
            {"artifact": "plan.md", "gate": "non_placeholder"},
        ],
    }
    assert artifacts_display(stage) == "spec.md, plan.md"


def test_display_many_truncates():
    stage = {
        "artifacts": [
            {"artifact": "a.md", "gate": "non_placeholder"},
            {"artifact": "b.md", "gate": "non_placeholder"},
            {"artifact": "c.md", "gate": "non_placeholder"},
            {"artifact": "d.md", "gate": "non_placeholder"},
        ],
    }
    result = artifacts_display(stage)
    assert result.startswith("a.md, b.md")
    assert "等 4 个产物" in result


def test_display_empty():
    assert artifacts_display({"artifacts": []}) == "?"


# ── 门禁求值 ───────────────────────────────────────────────────────────

S1 = {"artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]}
S2 = {"artifacts": [{"artifact": "spec.md", "gate": "exists"}]}
S3 = {"artifacts": [{"artifact": "nope.md", "gate": "exists"}]}
S4 = {"artifacts": [{"artifact": "x", "gate": "command", "command": "true"}]}
S5 = {"artifacts": [{"artifact": "x", "gate": "command", "command": "false"}]}


def test_exists_file(tmp_path):
    (tmp_path / "spec.md").write_text("x")
    assert gate_exists(tmp_path, "spec.md") is True
    assert gate_exists(tmp_path, "nope.md") is False


def test_exists_dir(tmp_path):
    (tmp_path / "impl").mkdir()
    assert gate_exists(tmp_path, "impl/") is True


def test_non_placeholder_missing_is_false(tmp_path):
    assert gate_non_placeholder(tmp_path, "spec.md") is False


def test_non_placeholder_stub_is_false(tmp_path):
    (tmp_path / "spec.md").write_text("TODO")
    assert gate_non_placeholder(tmp_path, "spec.md") is False


def test_non_placeholder_real_is_true(tmp_path):
    (tmp_path / "spec.md").write_text("# Spec\n" + "x" * 200)
    assert gate_non_placeholder(tmp_path, "spec.md") is True


def test_non_placeholder_dir_with_real_file_is_true(tmp_path):
    d = tmp_path / "impl"
    d.mkdir()
    (d / "romanconv.py").write_text("def to_roman(n): ...\n" + "x" * 200)
    assert gate_non_placeholder(tmp_path, "impl/") is True


def test_non_placeholder_empty_dir_is_false(tmp_path):
    d = tmp_path / "impl"
    d.mkdir()
    assert gate_non_placeholder(tmp_path, "impl/") is False


def test_command_exit_zero_passes(tmp_path):
    assert gate_command(tmp_path, "true") is True
    assert gate_command(tmp_path, "false") is False


def test_evaluate_gate_single_artifact(tmp_path):
    (tmp_path / "spec.md").write_text("# Spec\n" + "y" * 200)
    assert evaluate_gate(tmp_path, S1) is True  # non_placeholder pass
    assert evaluate_gate(tmp_path, S2) is True  # exists pass
    assert evaluate_gate(tmp_path, S3) is False  # exists fail
    assert evaluate_gate(tmp_path, S4) is True  # command pass
    assert evaluate_gate(tmp_path, S5) is False  # command fail


def test_evaluate_gate_all_artifacts_pass(tmp_path):
    (tmp_path / "a.md").write_text("# A\n" + "x" * 200)
    (tmp_path / "b.md").write_text("# B\n" + "y" * 200)
    stage = {
        "artifacts": [
            {"artifact": "a.md", "gate": "non_placeholder"},
            {"artifact": "b.md", "gate": "non_placeholder"},
        ],
    }
    assert evaluate_gate(tmp_path, stage) is True


def test_evaluate_gate_partial_fail(tmp_path):
    (tmp_path / "a.md").write_text("# A\n" + "x" * 200)
    # b.md missing
    stage = {
        "artifacts": [
            {"artifact": "a.md", "gate": "non_placeholder"},
            {"artifact": "b.md", "gate": "non_placeholder"},
        ],
    }
    assert evaluate_gate(tmp_path, stage) is False


def test_evaluate_gate_mixed_gate_types(tmp_path):
    (tmp_path / "spec.md").write_text("# Spec\n" + "x" * 200)
    # command 门槛命令执行成功即恒过
    stage = {
        "artifacts": [
            {"artifact": "spec.md", "gate": "non_placeholder"},
            {"artifact": "x", "gate": "command", "command": "true"},
        ],
    }
    assert evaluate_gate(tmp_path, stage) is True


def test_evaluate_gate_empty_artifacts(tmp_path):
    assert evaluate_gate(tmp_path, {"artifacts": []}) is False


def test_evaluate_gate_no_artifacts_key(tmp_path):
    assert evaluate_gate(tmp_path, {}) is False


# ── 阶段派生与跳步检测 ──────────────────────────────────────────────────

STAGES = [
    {"name": "spec", "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]},
    {"name": "plan", "artifacts": [{"artifact": "plan.md", "gate": "non_placeholder"}]},
    {"name": "execute", "artifacts": [{"artifact": "impl/", "gate": "non_placeholder"}]},
]

STAGES_MULTI = [
    {"name": "spec", "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]},
    {
        "name": "plan",
        "artifacts": [
            {"artifact": "plan.md", "gate": "non_placeholder"},
            {"artifact": "design.md", "gate": "exists"},
        ],
    },
    {"name": "execute", "artifacts": [{"artifact": "impl/", "gate": "non_placeholder"}]},
]


def _real(p):
    """Write a non-placeholder file (passes non_placeholder gate)."""
    p.write_text("# x\n" + "y" * 200)
    return p


def _exists(p):
    """Write a minimal file that passes exists gate."""
    p.write_text("x")
    return p


def test_completed_prefix_stops_at_first_unsatisfied(tmp_path):
    _real(tmp_path / "spec.md")
    assert completed_prefix(STAGES, tmp_path) == 1


def test_completed_prefix_all(tmp_path):
    for a in ("spec.md", "plan.md"):
        _real(tmp_path / a)
    (tmp_path / "impl").mkdir()
    (tmp_path / "impl" / "f.py").write_text("x" * 50)
    assert completed_prefix(STAGES, tmp_path) == 3


def test_completed_prefix_multi_artifact_all_pass(tmp_path):
    _real(tmp_path / "spec.md")
    _real(tmp_path / "plan.md")
    _exists(tmp_path / "design.md")
    assert completed_prefix(STAGES_MULTI, tmp_path) == 2


def test_completed_prefix_multi_artifact_partial(tmp_path):
    _real(tmp_path / "spec.md")
    _real(tmp_path / "plan.md")
    # design.md missing → plan stage gate fails
    assert completed_prefix(STAGES_MULTI, tmp_path) == 1


def test_detect_jump_later_artifact_while_current_unsatisfied(tmp_path):
    (tmp_path / "impl").mkdir()
    (tmp_path / "impl" / "f.py").write_text("x" * 50)
    jump = detect_jump(STAGES, tmp_path)
    assert jump is not None
    assert jump[0] == "spec" and jump[1] == "execute"


def test_detect_jump_multi_artifact_later(tmp_path):
    """later stage 有多个 artifact，任一存在即判跳步。"""
    (tmp_path / "design.md").write_text("x")  # plan stage 的第二个 artifact
    jump = detect_jump(STAGES_MULTI, tmp_path)
    assert jump is not None
    assert jump[0] == "spec" and jump[1] == "plan"


def test_detect_jump_accepts_precomputed_prefix(tmp_path):
    """prefix 复用调用方已算好的前缀，不重跑门禁（command 门求值要起 shell）。"""
    (tmp_path / "impl").mkdir()
    (tmp_path / "impl" / "f.py").write_text("x" * 50)
    assert detect_jump(STAGES, tmp_path, prefix=0) == ("spec", "execute")


def test_detect_jump_none_when_in_order(tmp_path):
    _real(tmp_path / "spec.md")
    assert detect_jump(STAGES, tmp_path) is None


def test_detect_jump_none_when_all_done(tmp_path):
    for a in ("spec.md", "plan.md"):
        _real(tmp_path / a)
    (tmp_path / "impl").mkdir()
    (tmp_path / "impl" / "f.py").write_text("x" * 50)
    assert detect_jump(STAGES, tmp_path) is None
