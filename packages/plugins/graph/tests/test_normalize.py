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

"""translator 格式归一（xlsx 规范 → 引擎格式）单元测试。"""

from __future__ import annotations

from workflow_controller.plugins.graph.translator import normalize_config, normalize_node


class TestNormalizeNode:
    @staticmethod
    def test_normal_task_type_maps_to_agent():
        n = normalize_node({"id": "a", "task_type": "normal", "executor": "developer"})
        assert n["task_type"] == "agent"

    @staticmethod
    def test_subgraph_maps_to_generator_with_output():
        n = normalize_node({"id": "run-x", "task_type": "subgraph", "file": "d/x.yaml"})
        assert n["task_type"] == "generator"
        assert n["subgraph"] == {"output": "d/x.yaml"}
        assert n["_xlsx_subgraph"] is True
        # 不兜底 executor：短路路径用不到，避免误导
        assert "executor" not in n

    @staticmethod
    def test_subgraph_without_file_falls_back_to_agent():
        n = normalize_node({"id": "run-x", "task_type": "subgraph"})
        assert n["task_type"] == "agent"

    @staticmethod
    def test_acceptance_list_with_verifier_becomes_agent():
        n = normalize_node(
            {
                "id": "a",
                "task_type": "normal",
                "executor": "d",
                "verifier": "verifier",
                "acceptance": ["验收 X：条件一", "条件二"],
                "max_retries": 3,
                "on_exhaust": "exit",
            }
        )
        acc = n["acceptance"]
        assert acc["type"] == "agent"
        assert acc["verifier"] == "verifier"
        assert acc["verifier_prompt"] == "验收 X：条件一\n条件二"
        assert acc["max_retries"] == 3
        assert acc["on_exhaust"] == "stop"  # exit → stop

    @staticmethod
    def test_acceptance_list_without_verifier_becomes_script():
        n = normalize_node(
            {
                "id": "a",
                "task_type": "normal",
                "executor": "d",
                "acceptance": ["test -s f", "grep -q x f"],
                "max_retries": 1,
                "on_exhaust": "stop",
            }
        )
        acc = n["acceptance"]
        assert acc["type"] == "script"
        assert acc["script"] == "test -s f && grep -q x f"
        assert acc["max_retries"] == 1

    @staticmethod
    def test_acceptance_dict_passthrough_with_verifier_sync():
        """engine 格式子 yaml：dict acceptance 原样保留，verifier 同步顶层。"""
        n = normalize_node(
            {
                "id": "a",
                "task_type": "agent",
                "executor": "d",
                "acceptance": {
                    "enabled": True,
                    "type": "agent",
                    "verifier": "st-verifier",
                    "verifier_prompt": "p",
                    "max_retries": 2,
                    "on_exhaust": "exit",
                },
            }
        )
        acc = n["acceptance"]
        assert acc["type"] == "agent"
        assert acc["verifier"] == "st-verifier"
        assert acc["on_exhaust"] == "stop"  # exit 归一
        assert n["verifier"] == "st-verifier"  # 同步到顶层

    @staticmethod
    def test_old_format_untouched():
        """旧格式（我们跑过的）完全透传。"""
        raw = {
            "id": "init-log",
            "task_type": "agent",
            "executor": "developer",
            "task_description": "x",
            "task_goal": "y",
            "acceptance": {
                "enabled": True,
                "type": "script",
                "script": "true",
                "max_retries": 1,
                "on_exhaust": "stop",
            },
        }
        n = normalize_node(raw)
        assert n["task_type"] == "agent"
        assert n["task_description"] == "x"
        assert n["task_goal"] == "y"
        assert n["acceptance"]["type"] == "script"
        assert n["acceptance"]["script"] == "true"

    @staticmethod
    def test_title_goal_approach_prompt_fields():
        n = normalize_node(
            {
                "id": "a",
                "task_type": "normal",
                "title": "做某事",
                "goal": ["目标一", "目标二"],
                "approach": ["路径一"],
                "out_of_scope": ["禁区一"],
            }
        )
        assert n["task_description"] == "做某事\n达成路径：路径一\n禁止事项：禁区一"
        assert n["task_goal"] == "目标一\n目标二"


class TestNormalizeConfig:
    @staticmethod
    def test_op_name_literal_to_variables():
        cfg = normalize_config({"op_name": "relu", "nodes": []})
        assert cfg["variables"]["OP"] == "relu"

    @staticmethod
    def test_op_name_template_to_argv():
        cfg = normalize_config({"op_name": "{{OP}}", "nodes": []})
        assert cfg["variables"]["OP"] == "{{argv.OP}}"

    @staticmethod
    def test_external_variables_win():
        cfg = normalize_config(
            {
                "op_name": "relu",
                "variables": {"OP": "softmax"},
                "nodes": [],
            }
        )
        assert cfg["variables"]["OP"] == "softmax"

    @staticmethod
    def test_max_rollback_maps():
        cfg = normalize_config({"max_rollback": 1, "nodes": []})
        assert cfg["rollback_budget"] == 1

    @staticmethod
    def test_existing_rollback_budget_wins():
        cfg = normalize_config({"max_rollback": 1, "rollback_budget": 2, "nodes": []})
        assert cfg["rollback_budget"] == 2

    @staticmethod
    def test_agents_dir_fallback():
        cfg = normalize_config({"nodes": []})
        assert cfg["agents_dir"] == "../agents"
        cfg2 = normalize_config({"agents_dir": "my-agents", "nodes": []})
        assert cfg2["agents_dir"] == "my-agents"
