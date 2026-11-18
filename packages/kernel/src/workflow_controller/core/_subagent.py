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

"""Runtime-agnostic sub-agent helpers shared by the cc and opencode drivers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MAX_CONTENT_CHARS = 2000

# Hint-wrap block prepended to a subagent's system prompt (runtime-agnostic).
_HINT_HEADER = "## ⚠️ 本次会话用户补充指令（Workflow Controller 注入，优先级高于工作流默认）"
_HINT_FOOTER = "## ⚠️ 补充指令结束"


def wrap_subagent_hint(hint: str) -> str:
    return f"{_HINT_HEADER}\n{hint}\n{_HINT_FOOTER}"


def truncate_subagent_content(text: str, max_chars: int = _MAX_CONTENT_CHARS) -> str:
    """Cap subagent display text at ``max_chars`` with a trailing ellipsis.

    Shared by the transcript path (cc, via ``summarize_subagent_transcript``)
    and the inline-content path (opencode, via
    ``SubagentTracker.on_stop``) so both runtimes cap SUBAGENT event content at the
    same length for the Dashboard.
    """
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _output_tokens(obj: Any) -> int:
    if not isinstance(obj, dict):
        return 0
    usage = obj.get("usage")
    if isinstance(usage, dict):
        return int(usage.get("output_tokens", 0) or 0)
    msg = obj.get("message")
    if isinstance(msg, dict):
        return _output_tokens(msg)
    return 0


def _assistant_texts(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return []
    container = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = container.get("content") if isinstance(container, dict) else None
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(str(block.get("text", "")))
    return out


def summarize_subagent_transcript(
    path: str,
    max_chars: int = _MAX_CONTENT_CHARS,
) -> tuple[int, str]:
    """Return (total_output_tokens, truncated_assistant_text) for a transcript."""
    if not path:
        return 0, ""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0, ""

    total_tokens = 0
    texts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        total_tokens += _output_tokens(obj)
        texts.extend(t for t in _assistant_texts(obj) if t.strip())

    content = "\n".join(texts)
    return total_tokens, truncate_subagent_content(content, max_chars)
