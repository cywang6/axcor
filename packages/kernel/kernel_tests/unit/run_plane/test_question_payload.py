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

from workflow_controller.core.question_payload import (
    AskQuestion,
    QuestionOption,
    extract_questions,
)
from workflow_controller.session.events import Event, EventType


def _auq_tool_use_event(
    raw_input: dict,
    tool_use_id: str = "toolu_01ABC",
    session_id: str = "s1",
    iteration: int = 1,
) -> Event:
    """构造一个 name == "AskUserQuestion" 的 TOOL_USE 事件，data = {id, name, input}。"""
    return Event.create(
        session_id=session_id,
        iteration=iteration,
        event_type=EventType.TOOL_USE,
        data={"id": tool_use_id, "name": "AskUserQuestion", "input": raw_input},
    )


class TestExtractQuestions:
    @staticmethod
    def test_single_question_extracted() -> None:
        ev = _auq_tool_use_event(
            {
                "questions": [
                    {
                        "question": "spec/ref/ 下没有参考文档。是否生成？",
                        "header": "Ref docs",
                        "multiSelect": False,
                        "options": [
                            {"label": "生成", "description": "用于交叉验证"},
                            {"label": "跳过", "description": "省时间但质量略降"},
                        ],
                    }
                ],
            }
        )
        questions = extract_questions(ev)
        assert len(questions) == 1
        q = questions[0]
        assert q.tool_use_id == "toolu_01ABC"
        assert q.question == "spec/ref/ 下没有参考文档。是否生成？"
        assert q.header == "Ref docs"
        assert q.multi_select is False
        assert len(q.options) == 2
        assert q.options[0].label == "生成"
        assert q.options[0].description == "用于交叉验证"

    @staticmethod
    def test_multiple_questions_in_one_tool_use() -> None:
        # One AUQ tool_use can carry multiple questions in its input.
        ev = _auq_tool_use_event(
            {
                "questions": [
                    {
                        "question": "用什么 auth？",
                        "header": "Auth",
                        "multiSelect": False,
                        "options": [{"label": "OAuth", "description": ""}],
                    },
                    {
                        "question": "用什么 DB？",
                        "header": "DB",
                        "multiSelect": False,
                        "options": [{"label": "Postgres", "description": ""}],
                    },
                ],
            },
            tool_use_id="toolu_01XYZ",
        )
        questions = extract_questions(ev)
        assert len(questions) == 2
        assert questions[0].header == "Auth"
        assert questions[1].header == "DB"

    @staticmethod
    def test_missing_optional_fields_default_safely() -> None:
        ev = _auq_tool_use_event(
            {
                "questions": [
                    {
                        "question": "Pick one",
                        "options": [{"label": "A"}],
                    }
                ],
            },
            tool_use_id="toolu_01M",
        )
        questions = extract_questions(ev)
        assert len(questions) == 1
        q = questions[0]
        assert q.header == ""
        assert q.multi_select is False
        assert q.options[0].description == ""

    @staticmethod
    def test_non_auq_tool_use_skipped() -> None:
        # TOOL_USE with name != "AskUserQuestion" → empty (Read is not AUQ).
        ev = Event.create(
            session_id="s1",
            iteration=1,
            event_type=EventType.TOOL_USE,
            data={"id": "toolu_R", "name": "Read", "input": {"file_path": "x.md"}},
        )
        assert extract_questions(ev) == []

    @staticmethod
    def test_non_tool_use_event_returns_empty() -> None:
        # 非 TOOL_USE 事件（如 RESULT）返回空。
        ev = Event.create(
            session_id="s1",
            iteration=1,
            event_type=EventType.STEP_FINISH,
            data={"num_turns": 1},
        )
        assert extract_questions(ev) == []

    @staticmethod
    def test_input_not_dict_returns_empty() -> None:
        ev = Event.create(
            session_id="s1",
            iteration=1,
            event_type=EventType.TOOL_USE,
            data={"id": "t", "name": "AskUserQuestion", "input": "not a dict"},
        )
        assert extract_questions(ev) == []

    @staticmethod
    def test_dataclasses_construct() -> None:
        opt = QuestionOption(label="X", description="some desc")
        q = AskQuestion(
            tool_use_id="t",
            question="qq",
            header="h",
            multi_select=True,
            options=[opt],
        )
        assert q.options[0].label == "X"
