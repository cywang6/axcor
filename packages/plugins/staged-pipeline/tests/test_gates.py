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

"""staged_pipeline 门禁测试：阶段产物存在性/跳步判定与提示前缀。"""

from __future__ import annotations

import types

import pytest

from workflow_controller.plugin.sdk import IterationGateResult, PluginConfig
from workflow_controller.plugins.staged_pipeline.plugin import StagedPipelinePlugin

STAGES = [
    {"name": "spec", "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]},
    {"name": "plan", "artifacts": [{"artifact": "plan.md", "gate": "non_placeholder"}]},
]

STAGES_MULTI = [
    {
        "name": "spec",
        "artifacts": [
            {"artifact": "spec.md", "gate": "non_placeholder"},
            {"artifact": "notes.md", "gate": "exists"},
        ],
    },
    {
        "name": "plan",
        "artifacts": [
            {"artifact": "plan.md", "gate": "non_placeholder"},
            {"artifact": "design.md", "gate": "non_placeholder"},
        ],
    },
]


def _plugin() -> StagedPipelinePlugin:
    cfg = PluginConfig(name="staged_pipeline", version="0.1.0", enabled=True)
    return StagedPipelinePlugin(cfg, None)  # type: ignore[arg-type]


def _sess(cwd, stages=None):
    return types.SimpleNamespace(
        id="s1",
        config=types.SimpleNamespace(cwd=str(cwd), stages=STAGES if stages is None else stages),
        pending_stage_review=None,
    )


def _real(p):
    p.write_text("# x\n" + "y" * 200)
    return p


def _exists(p):
    p.write_text("x")
    return p


@pytest.mark.asyncio
async def test_iteration_boundary_block_on_jump(tmp_path):
    p = _plugin()
    (tmp_path / "plan.md").write_text("# p\n" + "x" * 200)
    res = await p.on_iteration_boundary("s1", _sess(tmp_path), 1)
    assert isinstance(res, IterationGateResult)
    assert res.action == "block"
    assert "spec" in res.feedback
    assert "plan" in res.feedback
    assert res.reason.startswith("jump:")


@pytest.mark.asyncio
async def test_iteration_boundary_sets_pending_when_partial_complete(tmp_path):
    p = _plugin()
    sess = _sess(tmp_path)
    _real(tmp_path / "spec.md")
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert res.action == "pass"
    assert sess.pending_stage_review is not None
    assert sess.pending_stage_review["done"]["name"] == "spec"
    assert sess.pending_stage_review["next"]["name"] == "plan"
    assert sess.pending_stage_review["done_index"] == 0


@pytest.mark.asyncio
async def test_iteration_boundary_blocks_when_no_stage_complete(tmp_path):
    p = _plugin()
    sess = _sess(tmp_path)
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert isinstance(res, IterationGateResult)
    assert res.action == "block"
    assert "spec" in res.feedback
    assert sess.pending_stage_review is None


@pytest.mark.asyncio
async def test_iteration_boundary_none_when_all_complete(tmp_path):
    p = _plugin()
    sess = _sess(tmp_path)
    for a in ("spec.md", "plan.md"):
        _real(tmp_path / a)
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert res.action == "pass"
    assert sess.pending_stage_review is None


@pytest.mark.asyncio
async def test_inert_when_session_has_no_stages(tmp_path):
    p = _plugin()
    sess = _sess(tmp_path, stages=[])
    (tmp_path / "plan.md").write_text("# p\n" + "x" * 200)
    assert (await p.on_iteration_boundary("s1", sess, 1)).action == "pass"
    assert p.on_stop_hook("s1", sess, "final text") is None


@pytest.mark.asyncio
async def test_inert_when_session_config_missing_stages_attr(tmp_path):
    p = _plugin()
    sess = types.SimpleNamespace(
        id="s1",
        config=types.SimpleNamespace(cwd=str(tmp_path)),
    )
    assert (await p.on_iteration_boundary("s1", sess, 1)).action == "pass"
    assert p.on_stop_hook("s1", sess, "final text") is None


# ── multi-artifact tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_artifact_sets_pending_when_all_pass(tmp_path):
    """多 artifact 的 stage 所有 artifact 都通过门禁 → pending。"""
    p = _plugin()
    sess = _sess(tmp_path, stages=STAGES_MULTI)
    _real(tmp_path / "spec.md")
    _exists(tmp_path / "notes.md")
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert res.action == "pass"
    assert sess.pending_stage_review is not None
    assert sess.pending_stage_review["done"]["name"] == "spec"


@pytest.mark.asyncio
async def test_multi_artifact_partial_fail_stays_blocked(tmp_path):
    """多 artifact 的 stage 有一个 artifact 没通过门禁 → 阶段算未完成。"""
    p = _plugin()
    sess = _sess(tmp_path, stages=STAGES_MULTI)
    _real(tmp_path / "spec.md")
    # notes.md missing → spec stage gate should fail → k==0
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert isinstance(res, IterationGateResult)
    assert res.action == "block"
    assert "spec" in res.feedback


@pytest.mark.asyncio
async def test_multi_artifact_jump_on_second_artifact(tmp_path):
    """跳步检测：later stage 的第二个 artifact 存在即判跳。"""
    p = _plugin()
    sess = _sess(tmp_path, stages=STAGES_MULTI)
    _exists(tmp_path / "design.md")  # plan stage 的第二个 artifact
    res = await p.on_iteration_boundary("s1", sess, 1)
    assert isinstance(res, IterationGateResult)
    assert res.action == "block"
    assert "spec" in res.feedback
    assert "plan" in res.feedback


# ── prompt_prefix ───────────────────────────────────────────────────────


def test_prompt_prefix_returns_discipline_when_stages_declared(tmp_path):
    p = _plugin()
    txt = p.prompt_prefix(_sess(tmp_path, stages=STAGES).config)
    assert txt and "阶段" in txt
    assert ("一轮" in txt) or ("一个阶段" in txt)
    assert ("跨阶段" in txt) or ("跳步" in txt)


def test_prompt_prefix_inert_when_no_stages(tmp_path):
    p = _plugin()
    assert p.prompt_prefix(_sess(tmp_path, stages=[]).config) is None
    assert p.prompt_prefix(types.SimpleNamespace(cwd=str(tmp_path))) is None
