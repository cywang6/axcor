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

from workflow_controller.core.evaluator import (
    MaxIterationEvaluator,
    PassThroughEvaluator,
    ThresholdEvaluator,
)


class TestPassThroughEvaluator:
    async def test_returns_incomplete(self):
        evaluator = PassThroughEvaluator()
        result = await evaluator.evaluate([], {}, 1)
        assert result.is_complete is False
        assert result.progress == 0.0

    async def test_summary(self):
        evaluator = PassThroughEvaluator()
        result = await evaluator.evaluate([], {}, 5)
        assert "Pass-through" in result.summary


class TestMaxIterationEvaluator:
    async def test_delegates_to_inner_when_below_max(self):
        inner = PassThroughEvaluator()
        evaluator = MaxIterationEvaluator(inner, max_iterations=10)
        result = await evaluator.evaluate([], {}, iteration=5)
        assert result.is_complete is False

    async def test_completes_at_max_iteration(self):
        inner = PassThroughEvaluator()
        evaluator = MaxIterationEvaluator(inner, max_iterations=3)
        result = await evaluator.evaluate([], {}, iteration=3)
        assert result.is_complete is True
        assert result.progress == 1.0

    async def test_summary_contains_max_iterations(self):
        inner = PassThroughEvaluator()
        evaluator = MaxIterationEvaluator(inner, max_iterations=5)
        result = await evaluator.evaluate([], {}, iteration=5)
        assert "5" in result.summary


class TestThresholdEvaluator:
    async def test_completes_when_progress_meets_threshold(self):
        evaluator = ThresholdEvaluator(threshold=1.0)
        result = await evaluator.evaluate(
            [],
            {"progress": 1.0, "summary": "Done"},
            iteration=1,
        )
        assert result.is_complete is True
        assert result.feedback is None

    async def test_incomplete_when_below_threshold(self):
        evaluator = ThresholdEvaluator(threshold=1.0)
        result = await evaluator.evaluate(
            [],
            {"progress": 0.5, "summary": "Halfway", "feedback": "Keep going"},
            iteration=1,
        )
        assert result.is_complete is False
        assert result.feedback == "Keep going"

    async def test_custom_threshold(self):
        evaluator = ThresholdEvaluator(threshold=0.8)
        result = await evaluator.evaluate(
            [],
            {"progress": 0.8, "summary": "Good enough"},
            iteration=3,
        )
        assert result.is_complete is True
