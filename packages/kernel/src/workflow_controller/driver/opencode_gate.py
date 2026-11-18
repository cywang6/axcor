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

"""Stage 3b: opencode MCP-gate — AskUserQuestion-equivalent for the opencode
runtime.

opencode has no ``AskUserQuestion`` tool and its events arrive post-completion
(a tool call's result is already in the event), so the cc PreToolUse deny model
does not apply. Instead Workflow Controller exposes an MCP tool the worker calls to ask a
question; the call BLOCKS while Workflow Controller resolves an answer (auto: reviewer; or
manual: a human via the Dashboard manual-inject queue) and the answer is
returned as the tool result, flowing back into the worker's same turn. This is
cleaner than cc's deny-with-answer trick and is the only synchronous
interception point opencode offers.

Lifecycle: the opencode driver starts one :class:`PerQueryGate` per ``query()``
(in-process, session-scoped via closure → zero routing), injects its URL into
``OPENCODE_CONFIG_CONTENT`` so opencode connects, and stops it when the run
ends.
"""

from __future__ import annotations

# 兼容旧导入路径；待调用方全面迁移到 runtime_gate 后删除 gate 相关 re-export。
__all__ = [
    "GATE_MCP_NAME",
    "GATE_TOOL_NAME",
    "PROGRESS_INTERVAL_SECONDS",
    "WORKER_AGENT_NAME",
    "PerQueryGate",
    "answer_via_gate",
    "answer_via_gate_with_keepalive",
    "build_config_content",
]

import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

import yaml

from workflow_controller.core._subagent import wrap_subagent_hint
from workflow_controller.driver.runtime_gate import (
    GATE_MCP_NAME,
    GATE_TOOL_NAME,
    PROGRESS_INTERVAL_SECONDS,
    PerQueryGate,
    answer_via_gate,
    answer_via_gate_with_keepalive,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

# Named worker agent that carries the project's system_prompt (opencode has no
# --system-prompt flag; per-agent `prompt` is its system-instruction channel).
# Selected at run via `--agent workflow-controller-worker`; mirrors the restricted-agent path.
WORKER_AGENT_NAME = "workflow-controller-worker"


def build_config_content(
    gate_url: str | None = None,
    restricted_agent: dict[str, Any] | None = None,
    system_prompt: str = "",
    subagent_hints: dict[str, list[str]] | None = None,
    agents_dir: Path | None = None,
    instructions: list[str] | None = None,
    session_id: str = "",
    plugins: list[str] | None = None,
    base_content: str = "",
    auto_compaction: bool = False,
) -> str:
    """Build the ``OPENCODE_CONFIG_CONTENT`` JSON for opencode, composing present sections.

    各注入段（mcp gate / restricted+worker agent / subagent hints / 全局
    instructions / 插件与 compaction）的机制细节见各 ``_apply_*`` helper 的
    注释；任一段可缺省：worker 用 gate（可选 system_prompt）但无需工具限制，
    reviewer 限制工具但无 gate、无 system_prompt。
    """
    cfg: dict[str, Any] = {}
    _apply_base_content(cfg, base_content)
    raw_compaction = cfg.get("compaction")
    compaction = dict(raw_compaction) if isinstance(raw_compaction, dict) else {}
    compaction["auto"] = auto_compaction
    cfg["compaction"] = compaction
    if plugins:
        configured_plugins = list(cfg["plugin"]) if isinstance(cfg.get("plugin"), list) else []
        cfg["plugin"] = list(dict.fromkeys([*configured_plugins, *plugins]))
    _apply_gate_mcp(cfg, gate_url)
    # Merge named agents (restricted reviewer + worker system_prompt + hinted
    # subagents) into one map so co-occurring agents don't overwrite each other.
    agents: dict[str, Any] = dict(cfg["agent"]) if isinstance(cfg.get("agent"), dict) else {}
    if restricted_agent:
        agents.update(restricted_agent.get("agent", {}))
        _apply_restricted_permission(cfg, restricted_agent)
    if system_prompt:
        # mode=primary：v2 起 run --agent 只对 primary 模式的 agent 生效，
        # 缺省（subagent）会被静默回退到默认 build agent
        agents[WORKER_AGENT_NAME] = {"prompt": system_prompt, "mode": "primary"}
    _apply_subagent_hints(agents, subagent_hints, agents_dir)
    if agents:
        cfg["agent"] = agents
    _apply_session_instructions(cfg, instructions, session_id)
    return json.dumps(cfg)


def _apply_base_content(cfg: dict[str, Any], base_content: str) -> None:
    """解析既有 OPENCODE_CONFIG_CONTENT 并入 cfg（非 JSON object 则告警忽略）。"""
    if not base_content:
        return
    try:
        parsed = json.loads(base_content)
        if isinstance(parsed, dict):
            cfg.update(parsed)
        else:
            logger.warning("OPENCODE_CONFIG_CONTENT is not a JSON object; ignoring it")
    except json.JSONDecodeError:
        logger.warning("OPENCODE_CONFIG_CONTENT is invalid JSON; ignoring it")


def _apply_gate_mcp(cfg: dict[str, Any], gate_url: str | None) -> None:
    """gate_url 给定时注入 ``mcp.workflow_controller_gate``（remote，无 OAuth）。

    The session is conveyed by the per-query gate instance itself (closure),
    so the URL carries no session id.
    """
    if not gate_url:
        return
    raw_mcp = cfg.get("mcp")
    mcp = dict(raw_mcp) if isinstance(raw_mcp, dict) else {}
    mcp[GATE_MCP_NAME] = {
        "type": "remote",
        "url": gate_url,
        "enabled": True,
        "oauth": False,
    }
    cfg["mcp"] = mcp


def _apply_restricted_permission(cfg: dict[str, Any], restricted_agent: dict[str, Any]) -> None:
    """顶层 permission deny（v2 的强制通道）——用户已显式配置的键不覆盖。"""
    raw_perm = cfg.get("permission")
    perm = dict(raw_perm) if isinstance(raw_perm, dict) else {}
    for cat, effect in (restricted_agent.get("permission") or {}).items():
        perm.setdefault(cat, effect)
    if perm:
        cfg["permission"] = perm


def _apply_subagent_hints(
    agents: dict[str, Any],
    subagent_hints: dict[str, list[str]] | None,
    agents_dir: Path | None,
) -> None:
    """Prepend each subagent_type's hints (combined) to its on-disk def.

    保留磁盘 ``.opencode/agents/{type}.md`` 的 frontmatter；无磁盘文件的
    type 跳过（不伪造 agent）；同一 subagent_type 的多条 hints 合成一块。
    """
    if not (subagent_hints and agents_dir is not None):
        return
    for stype, hints in subagent_hints.items():
        combined = "\n\n".join(h for h in hints if h)
        if not combined:
            continue
        parsed = _read_opencode_agent_prompt(agents_dir / f"{stype}.md")
        if parsed is None:
            continue
        fields, body = parsed
        fields["prompt"] = wrap_subagent_hint(combined) + "\n\n" + body
        agents[stype] = fields


def _apply_session_instructions(
    cfg: dict[str, Any],
    instructions: list[str] | None,
    session_id: str,
) -> None:
    """Global instructions: write to a temp file, reference via cfg["instructions"].

    opencode appends file content to ALL agents' system prompts (worker + every
    subagent). Complementary to subagent_hints (per-type prepend): one
    registration reaches every agent. Rewritten each query so the latest
    registry state (register/clear at iteration boundaries) takes effect.
    """
    if not instructions:
        return
    combined_instructions = "\n\n".join(i for i in instructions if i)
    if not combined_instructions:
        return
    sid = session_id or "default"
    path = os.path.join(tempfile.gettempdir(), f"workflow-controller-instructions-{sid}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_wrap_session_instruction(combined_instructions))
        existing_instructions = (
            list(cfg["instructions"]) if isinstance(cfg.get("instructions"), list) else []
        )
        cfg["instructions"] = list(dict.fromkeys([*existing_instructions, path]))
    except OSError:
        logger.warning("opencode gate: failed to write instructions file %s", path)


def _read_opencode_agent_prompt(path: Path) -> tuple[dict[str, Any], str] | None:
    """Parse an opencode agent ``.md`` (YAML frontmatter + body).

    Returns ``(fields, body)`` — frontmatter minus ``name`` (it's the agent key,
    not a value field) and the body (the system prompt). ``None`` when the file
    is missing/unreadable/has no valid frontmatter (caller then skips overlaying
    that agent_type). A file with no frontmatter is body-only.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return {}, text
    # leading "---" → split out the YAML block + body. maxsplit=2 keeps any
    # "---" inside the body intact.
    parts = text.split("---", 2)
    if len(parts) < 3:
        # opening "---" but no closing delimiter → malformed frontmatter
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    fm.pop("name", None)  # name is the agent key, not a value field
    body = parts[2].lstrip("\n")
    return fm, body


# Instruction-wrap block written to the temp file referenced by
# cfg["instructions"]. opencode appends file content to ALL agents' system
# prompts (worker + every subagent); the wrapper delimits the session-scoped
# user supplement so agents can distinguish it from their default prompt.
_INSTRUCTION_HEADER = (
    "## ⚠️ 本次会话全局指令（Workflow Controller 注入，对所有 agent 生效，优先级高于工作流默认）"
)
_INSTRUCTION_FOOTER = "## ⚠️ 全局指令结束"


def _wrap_session_instruction(text: str) -> str:
    """Wrap global instruction text in a delimited block.

    The block is written to the temp file referenced by ``cfg["instructions"]``;
    opencode appends the file's content to every agent's system prompt.
    """
    return f"{_INSTRUCTION_HEADER}\n{text}\n{_INSTRUCTION_FOOTER}"
