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

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from workflow_controller.session.events import EventType

if TYPE_CHECKING:
    from workflow_controller.session.events import Event


@dataclass
class QuestionOption:
    label: str
    description: str = ""


@dataclass
class AskQuestion:
    tool_use_id: str
    question: str
    header: str = ""
    multi_select: bool = False
    options: list[QuestionOption] = field(default_factory=list)


def ask_question_from_raw(raw_q: dict[str, Any], tool_use_id: str) -> AskQuestion:
    """单条原始 question 字典映射为 AskQuestion（事件轨与 AUQ 工具轨共用）。"""
    options = [
        QuestionOption(
            label=str(o.get("label", "")),
            description=str(o.get("description", "")),
        )
        for o in raw_q.get("options", [])
        if isinstance(o, dict)
    ]
    return AskQuestion(
        tool_use_id=tool_use_id,
        question=str(raw_q.get("question", "")),
        header=str(raw_q.get("header", "")),
        multi_select=bool(raw_q.get("multiSelect", False)),
        options=options,
    )


def extract_questions(event: Event) -> list[AskQuestion]:
    """Extract AskUserQuestion items from a TOOL_USE event.

    Accepts a TOOL_USE event whose ``name == "AskUserQuestion"`` and parses
    its ``input`` into AskQuestion items (one AUQ tool_use can carry multiple
    questions).
    """
    if event.event_type is not EventType.TOOL_USE:
        return []
    if event.data.get("name") != "AskUserQuestion":
        return []

    tool_use_id = str(event.data.get("id", ""))
    raw_input = event.data.get("input", {})
    if not isinstance(raw_input, dict):
        return []

    questions: list[AskQuestion] = []
    for raw_q in raw_input.get("questions", []):
        if not isinstance(raw_q, dict):
            continue
        questions.append(ask_question_from_raw(raw_q, tool_use_id))
    return questions
