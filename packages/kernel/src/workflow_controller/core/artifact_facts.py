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

"""Domain-agnostic artifact-facts for the Stop reviewer.

The Stop reviewer's contract (see ``_stop_prompts._STOP_JUDGE_PROMPT``) requires
verifying that the artifacts a worker claims to have produced actually landed on
disk, and catching placeholder/stub "fake completion". Originally a
migration-specific helper (``build_a2h_artifact_facts``) globbed a fixed set of
stage product paths and injected a markdown truth table into the prompt; that
helper carried domain structure (the a2h stage layout) and was deleted during
neutralization, leaving the prompt's ``{artifact_facts}`` slot permanently empty
and forcing the reviewer to re-discover disk state itself via Read/Glob —
non-deterministic and flaky.

This module restores that mechanism in a **domain-agnostic** form: instead of a
hard-coded stage layout, it globs the file paths the worker itself mentions in
its narrative. No domain knowledge required, so it fits the generic framework.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from workflow_controller.plugin.stages import normalize_artifacts

if TYPE_CHECKING:
    from pathlib import Path

# Recognized file extensions used to spot path-like tokens in worker narrative.
_EXTENSIONS = (
    "md",
    "markdown",
    "py",
    "ts",
    "tsx",
    "js",
    "jsx",
    "yaml",
    "yml",
    "json",
    "txt",
    "rst",
    "toml",
    "cfg",
    "ini",
    "sh",
    "go",
    "rs",
    "java",
    "kt",
    "c",
    "cpp",
    "h",
    "hpp",
    "proto",
    "sql",
    "csv",
    "html",
    "htm",
    "css",
    "scss",
    "vue",
    "svelte",
    "xml",
    "log",
)
_EXT_GROUP = "|".join(_EXTENSIONS)

# ``token.ext`` anywhere in a backtick-quoted span.
_HAS_EXT = re.compile(rf"\.(?:{_EXT_GROUP})\b", re.IGNORECASE)
# Backtick-quoted spans — the worker's explicit code/path references.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Bare ``dir/.../file.ext`` tokens (at least one directory segment + extension),
# caught even when the worker does not wrap them in backticks.
_BARE_PATH_RE = re.compile(
    rf"(?:[A-Za-z0-9_\-.]+/)+[A-Za-z0-9_\-.]+\.(?:{_EXT_GROUP})\b",
    re.IGNORECASE,
)

# Shell brace shorthand, e.g. ``{__init__,core}`` inside a worker-mentioned
# path. Workers often compress an artifact list this way; see ``_expand_braces``.
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _expand_braces(token: str) -> list[str]:
    """Expand shell brace shorthand in a worker-mentioned path token.

    A worker often compresses its artifact list as ``{__init__,core}.py``.
    Left unexpanded, the truth-table glob tests that literal string (which
    never exists on disk) and reports every brace-compressed path as
    ✗ 缺失 — a false miss that made the reviewer block genuine completion.
    This expands the first ``{a,b,c}`` group and recurses so multiple groups
    yield a cartesian product (``{a,b}/{c,d}.py`` → four paths). Tokens
    without braces return ``[token]`` unchanged.
    """
    m = _BRACE_RE.search(token)
    if not m:
        return [token]
    out: list[str] = []
    suffix = token[slice(m.end(), None)]
    for opt in m.group(1).split(","):
        out.extend(_expand_braces(token[: m.start()] + opt.strip() + suffix))
    return out


# Files below this size, or whose body is a known stub token, are flagged as
# placeholder/stub so the reviewer can catch "fake completion".
_STUB_MAX_BYTES = 32
_STUB_TOKENS = {"placeholder", "todo", "tbd", "wip", "stub", "占位", "待补充"}


def _looks_like_path(tok: str) -> bool:
    """True if a backtick-quoted span looks like a file/dir path."""
    if not re.search(r"[A-Za-z]", tok):  # drop purely-numeric refs like 5/5
        return False
    return ("/" in tok) or tok.endswith("/") or bool(_HAS_EXT.search(tok))


def extract_claimed_artifact_paths(context: str) -> list[str]:
    """Pull the file/dir paths the worker mentions in its narrative.

    High-precision by design: only backtick-quoted spans that look like paths,
    plus bare ``dir/.../file.ext`` tokens. Numeric/task refs (``5/5``,
    ``#5-13``, ``CHECK-4/5/6/9``) and plain slash-joined words are ignored so
    the truth table is not polluted with non-paths. Brace shorthand
    (``{a,b}.py``) is expanded to its literal paths before globbing. Order of
    first appearance is preserved; duplicates removed.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        for cand in _expand_braces(raw.strip()):
            if cand and cand not in seen and _looks_like_path(cand):
                seen.add(cand)
                found.append(cand)

    for tok in _BACKTICK_RE.findall(context):
        _add(tok)
    for match in _BARE_PATH_RE.findall(context):
        _add(match)
    return found


def _classify(full: Path) -> str:
    """Classify one resolved path for the truth table."""
    if not full.exists():
        return "✗ 缺失"
    if full.is_dir():
        return "✓ 目录"
    size = full.stat().st_size
    try:
        body = full.read_text(encoding="utf-8", errors="ignore").strip().lower()
    except OSError:
        body = ""
    if size < _STUB_MAX_BYTES or body in _STUB_TOKENS:
        return f"✓ 占位/空壳 ({size}B)"
    return f"✓ 有内容 ({size}B)"


def _classify_with_parent(rel: str, sandbox_root: Path) -> str:
    """Like _classify, but if sandbox_root/rel doesn't exist, also check
    sandbox_root.parent (project dir). Files like workflows/op-dev.yaml
    live one level above the graph work_dir; without this fallback the
    truth table marks them "缺失" and the reviewer LLM blocks the worker
    on a false positive.
    """
    full = sandbox_root / rel
    if full.exists():
        return _classify(full)
    parent = sandbox_root.parent / rel
    if parent.exists():
        # 父目录回退同样要过占位/空壳检测——0B 或 TODO 占位产物若被
        # 一律报告成"存在"，reviewer 会据此放过未完成产物。
        return f"{_classify(parent)}（项目目录）"
    return "✗ 缺失"


def build_artifact_facts(paths: list[str], sandbox_root: Path) -> str:
    """Build the markdown truth table injected into the Stop prompt.

    Returns ``""`` when there are no paths, so callers can pass the result
    straight into ``render_stop_prompt(artifact_facts=...)`` and the prompt's
    slot simply renders empty.
    """
    if not paths:
        return ""
    lines = ["| 路径 | 磁盘状态 |", "|---|---|"]
    for rel in paths:
        lines.append(f"| `{rel}` | {_classify_with_parent(rel, sandbox_root)} |")
    return "\n".join(lines)


def build_stop_artifact_facts(context: str, sandbox_root: Path) -> str:
    """Extract paths from the worker narrative and build the truth table."""
    return build_artifact_facts(
        extract_claimed_artifact_paths(context),
        sandbox_root,
    )


def _dir_fact_lines(full: Path) -> list[str]:
    """目录产物的磁盘真值行：非空文件清单 + 总体积（空目录明确标注）。"""
    files = [f for f in full.rglob("*") if f.is_file() and f.stat().st_size > 0]
    if not files:
        return ["磁盘状态: ✗ 空目录（无非空文件）"]
    total = sum(f.stat().st_size for f in files)
    lines = [f"磁盘状态: ✓ 目录，{len(files)} 个非空文件，共 {total}B", "文件清单:"]
    for f in files[:20]:
        rel = f.relative_to(full)
        lines.append(f"  - {rel} ({f.stat().st_size}B)")
    return lines


def build_stage_artifact_facts(sandbox_root: Path, stage: dict[str, Any]) -> str:
    """针对**声明的** stage artifact 产磁盘真值 + 内容摘要，供 per-stage reviewer 评判实质。

    区别于 ``build_stop_artifact_facts``（从 worker 叙述抽路径）：这里按 stages 声明的
    artifacts 精确取，不依赖 worker 自述——reviewer 能看到「该阶段该产的产物到底在不在、
    是不是占位、内容是什么」。文件→体积 + 截断正文(~2KB)；目录→非空文件清单 + 总体积；
    缺失/空壳→明确标注（reviewer 据此 reject）。
    """
    name = str(stage.get("name", "?"))
    entries = normalize_artifacts(stage)
    if not entries:
        return f"### 阶段「{name}」\n磁盘状态: artifacts 未声明"

    sections: list[str] = []
    for i, entry in enumerate(entries):
        artifact = entry["artifact"]
        prefix = f"### 阶段「{name}」产物 `{artifact}`" if i == 0 else f"#### 产物 `{artifact}`"
        full = sandbox_root / artifact
        lines = [prefix]
        if not full.exists():
            lines.append("磁盘状态: ✗ 缺失（未落盘）")
            sections.append("\n".join(lines))
            continue
        if full.is_dir():
            lines.extend(_dir_fact_lines(full))
        else:
            size = full.stat().st_size
            body = full.read_text(encoding="utf-8", errors="ignore")
            if size < _STUB_MAX_BYTES or body.strip().lower() in _STUB_TOKENS:
                lines.append(f"磁盘状态: ✗ 占位/空壳 ({size}B)")
            else:
                lines.append(f"磁盘状态: ✓ 有内容 ({size}B)")
            snippet = body.strip()[:2000]
            lines.append(f"产物正文(前 2000 字符):\n<<<\n{snippet}\n>>>")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
