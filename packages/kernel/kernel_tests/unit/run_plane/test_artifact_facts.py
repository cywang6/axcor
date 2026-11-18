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

"""Unit tests for the generic artifact-facts module.

This restores (in domain-agnostic form) the truth-table injection that was
deleted along with the a2h/migration-specific ``build_a2h_artifact_facts``.
The Stop reviewer's contract requires verifying artifacts actually landed on
disk and catching placeholder/stub "fake completion"; these functions gather
those disk facts by globbing the file paths the worker itself mentions — no
domain-specific stage structure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflow_controller.core.artifact_facts import (
    build_artifact_facts,
    build_stop_artifact_facts,
    extract_claimed_artifact_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# extract_claimed_artifact_paths
# ---------------------------------------------------------------------------


class TestExtractClaimedArtifactPaths:
    @staticmethod
    def test_extracts_backtick_doc_paths() -> None:
        ctx = "已生成 `docs/requirements.md`、`docs/design.md` 两个产物。"
        assert extract_claimed_artifact_paths(ctx) == [
            "docs/requirements.md",
            "docs/design.md",
        ]

    @staticmethod
    def test_extracts_backtick_directory_with_trailing_slash() -> None:
        ctx = "写了 `docs/design-pages/` 目录下的分页设计。"
        assert extract_claimed_artifact_paths(ctx) == ["docs/design-pages/"]

    @staticmethod
    def test_extracts_bare_slash_paths_with_extension() -> None:
        ctx = "src/main.py 和 tests/test_main.py 都已落盘。"
        assert extract_claimed_artifact_paths(ctx) == [
            "src/main.py",
            "tests/test_main.py",
        ]

    @staticmethod
    def test_ignores_numeric_and_task_refs() -> None:
        ctx = "完成度 5/5。任务 #5-13 pending。CHECK-4/5/6/9 FAIL。"
        assert extract_claimed_artifact_paths(ctx) == []

    @staticmethod
    def test_ignores_plain_backtick_words_without_path_shape() -> None:
        ctx = "confidence = `medium`，status = `PASS`。"
        assert extract_claimed_artifact_paths(ctx) == []

    @staticmethod
    def test_dedups_repeated_paths() -> None:
        ctx = "`docs/x.md` 已写，再确认一遍 `docs/x.md`。"
        assert extract_claimed_artifact_paths(ctx) == ["docs/x.md"]

    @staticmethod
    def test_preserves_first_seen_order() -> None:
        ctx = "`b/y.md` 先，`a/x.md` 后。"
        assert extract_claimed_artifact_paths(ctx) == ["b/y.md", "a/x.md"]


# ---------------------------------------------------------------------------
# brace-shorthand expansion (worker may list artifacts as `{a,b}.py`)
# ---------------------------------------------------------------------------


class TestBraceExpansion:
    """A worker often compresses its artifact list with shell brace
    shorthand (``{__init__,core}.py``). The extractor must expand these to
    the literal paths actually on disk, or the truth table reports every
    brace-compressed path as ✗ 缺失 and the reviewer blocks real completion.
    """

    @staticmethod
    def test_backtick_brace_in_dir_expands_to_full_paths() -> None:
        ctx = "已交付 `impl/romanconv/{__init__,core}.py` 两个文件。"
        assert extract_claimed_artifact_paths(ctx) == [
            "impl/romanconv/__init__.py",
            "impl/romanconv/core.py",
        ]

    @staticmethod
    def test_brace_in_filename_expands_all_options() -> None:
        ctx = "测试 `impl/tests/test_{to_roman,from_roman,roundtrip}.py` 全绿。"
        assert extract_claimed_artifact_paths(ctx) == [
            "impl/tests/test_to_roman.py",
            "impl/tests/test_from_roman.py",
            "impl/tests/test_roundtrip.py",
        ]

    @staticmethod
    def test_brace_at_start_expands() -> None:
        ctx = "产出 `{spec,plan}.md` 两份。"
        assert extract_claimed_artifact_paths(ctx) == ["spec.md", "plan.md"]

    @staticmethod
    def test_multiple_brace_groups_cartesian_product() -> None:
        ctx = "建了 `{a,b}/{c,d}.py`。"
        assert extract_claimed_artifact_paths(ctx) == [
            "a/c.py",
            "a/d.py",
            "b/c.py",
            "b/d.py",
        ]

    @staticmethod
    def test_path_without_braces_unchanged() -> None:
        ctx = "`docs/x.md` 已写。"
        assert extract_claimed_artifact_paths(ctx) == ["docs/x.md"]

    @staticmethod
    def test_expanded_paths_dedup_against_later_verbatim() -> None:
        # brace shorthand expands impl/core.py first; the later verbatim
        # mention of the same path collapses to one row, not two.
        ctx = "做了 `impl/{core,utils}.py`，其中 `impl/core.py` 再确认。"
        assert extract_claimed_artifact_paths(ctx) == [
            "impl/core.py",
            "impl/utils.py",
        ]


# ---------------------------------------------------------------------------
# build_artifact_facts
# ---------------------------------------------------------------------------


class TestBuildArtifactFacts:
    @staticmethod
    def test_empty_paths_returns_empty_string(tmp_path: Path) -> None:
        assert build_artifact_facts([], tmp_path) == ""

    @staticmethod
    def test_missing_file_marked_absent(tmp_path: Path) -> None:
        table = build_artifact_facts(["docs/missing.md"], tmp_path)
        assert "docs/missing.md" in table
        assert "✗" in table
        assert "缺失" in table

    @staticmethod
    def test_real_file_marked_substantive(tmp_path: Path) -> None:
        p = tmp_path / "docs" / "requirements.md"
        p.parent.mkdir(parents=True)
        p.write_text("# 需求文档\n\n" + "这是一份真实的需求说明内容。\n" * 5)
        table = build_artifact_facts(["docs/requirements.md"], tmp_path)
        assert "✓" in table
        assert "有内容" in table

    @staticmethod
    def test_placeholder_file_flagged_as_stub(tmp_path: Path) -> None:
        p = tmp_path / "docs" / "x.md"
        p.parent.mkdir(parents=True)
        p.write_text("placeholder")
        table = build_artifact_facts(["docs/x.md"], tmp_path)
        assert "占位" in table or "空壳" in table

    @staticmethod
    def test_directory_marked_as_dir(tmp_path: Path) -> None:
        (tmp_path / "docs" / "pages").mkdir(parents=True)
        table = build_artifact_facts(["docs/pages/"], tmp_path)
        assert "✓" in table
        assert "目录" in table

    @staticmethod
    def test_parent_dir_fallback_keeps_stub_detection(tmp_path: Path) -> None:
        # Regression: parent-directory fallback used to blanket-report "存在",
        # bypassing placeholder detection — a TODO stub in the project dir
        # would pass the reviewer as a real artifact.
        p = tmp_path / "docs" / "x.md"
        p.parent.mkdir(parents=True)
        p.write_text("TODO")
        sandbox = tmp_path / "work"
        sandbox.mkdir()
        table = build_artifact_facts(["docs/x.md"], sandbox)
        assert "占位" in table or "空壳" in table
        assert "项目目录" in table

    @staticmethod
    def test_parent_dir_fallback_substantive_file(tmp_path: Path) -> None:
        (tmp_path / "workflows").mkdir()
        (tmp_path / "workflows" / "op-dev.yaml").write_text(
            "# op-dev workflow\n" + "stages: []\n" * 10
        )
        sandbox = tmp_path / "work"
        sandbox.mkdir()
        table = build_artifact_facts(["workflows/op-dev.yaml"], sandbox)
        assert "有内容" in table
        assert "项目目录" in table


class TestBuildStopArtifactFacts:
    @staticmethod
    def test_extracts_from_context_then_classifies(tmp_path: Path) -> None:
        p = tmp_path / "docs" / "research.md"
        p.parent.mkdir(parents=True)
        p.write_text("# 调研报告\n\n四节齐备的真实内容。\n" * 3)
        ctx = "技术调研文档 `docs/research.md` 已完成，四节齐备。"
        table = build_stop_artifact_facts(ctx, tmp_path)
        assert "docs/research.md" in table
        assert "有内容" in table

    @staticmethod
    def test_no_paths_in_context_returns_empty(tmp_path: Path) -> None:
        ctx = "本轮工作完成，没有提到任何文件路径。"
        assert build_stop_artifact_facts(ctx, tmp_path) == ""

    @staticmethod
    def test_brace_shorthand_does_not_false_report_missing(
        tmp_path: Path,
    ) -> None:
        # Regression: worker lists real artifacts with brace shorthand;
        # disk has the literal expanded files. Truth table must NOT mark
        # them ✗ 缺失 (which made the reviewer block genuine completion).
        roman = tmp_path / "impl" / "romanconv"
        roman.mkdir(parents=True)
        (roman / "__init__.py").write_text("from .core import to_roman\n")
        (roman / "core.py").write_text("def to_roman(n):\n    ...\n" * 5)
        ctx = "已交付 `impl/romanconv/{__init__,core}.py`，全部阶段完成，任务结束。"
        table = build_stop_artifact_facts(ctx, tmp_path)
        assert "✗" not in table
        assert "缺失" not in table
        assert "impl/romanconv/__init__.py" in table
        assert "impl/romanconv/core.py" in table
        assert "有内容" in table
