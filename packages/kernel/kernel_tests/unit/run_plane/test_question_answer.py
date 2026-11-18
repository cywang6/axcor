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

from workflow_controller.core.question_answer import (
    QuestionAnswer,
    parse_question_answer,
)


class TestParseQuestionAnswer:
    @staticmethod
    def test_valid_json_with_index_and_label() -> None:
        raw = '{"selected_index": 2, "selected_label": "跳过", "rationale": "省时间"}'
        ans = parse_question_answer(raw, options=["生成", "跳过"])
        assert ans.selected_index == 2
        assert ans.selected_label == "跳过"
        assert ans.rationale == "省时间"
        assert ans.fallback is False

    @staticmethod
    def test_label_only_resolves_to_index() -> None:
        raw = '{"selected_label": "Postgres"}'
        ans = parse_question_answer(raw, options=["MongoDB", "Postgres", "MySQL"])
        assert ans.selected_index == 2
        assert ans.selected_label == "Postgres"

    @staticmethod
    def test_index_only_resolves_to_label() -> None:
        raw = '{"selected_index": 1}'
        ans = parse_question_answer(raw, options=["生成", "跳过"])
        assert ans.selected_index == 1
        assert ans.selected_label == "生成"

    @staticmethod
    def test_invalid_json_falls_back_to_first_option() -> None:
        raw = "garbage @@@@~~~~ no JSON here"
        ans = parse_question_answer(raw, options=["A", "B", "C"])
        assert ans.fallback is True
        assert ans.selected_index == 1
        assert ans.selected_label == "A"

    @staticmethod
    def test_label_match_in_free_text() -> None:
        raw = "I think we should go with the 跳过 option because it's faster"
        ans = parse_question_answer(raw, options=["生成", "跳过"])
        assert ans.selected_label == "跳过"
        assert ans.selected_index == 2

    @staticmethod
    def test_out_of_range_index_falls_back() -> None:
        raw = '{"selected_index": 99}'
        ans = parse_question_answer(raw, options=["A", "B"])
        assert ans.fallback is True
        assert ans.selected_index == 1
        assert ans.selected_label == "A"

    @staticmethod
    def test_empty_options_list_still_parses() -> None:
        raw = '{"selected_index": 1, "selected_label": "anything"}'
        ans = parse_question_answer(raw, options=[])
        assert ans.fallback is True
        assert ans.selected_label == ""

    @staticmethod
    def test_dataclass_defaults() -> None:
        ans = QuestionAnswer(
            selected_index=1,
            selected_label="X",
        )
        assert ans.rationale == ""
        assert ans.fallback is False
