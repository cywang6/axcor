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

"""stages 现在是项目级配置：AppConfig 从 workflow-controller.yaml 解析、SessionConfig 随身携带，
plugin 在门禁时从 session 读。覆盖 config 侧 plumbing。
"""

from __future__ import annotations

from workflow_controller.config.settings import AppConfig, SessionConfig


def test_appconfig_from_yaml_parses_stages(tmp_path) -> None:
    yaml = tmp_path / "workflow-controller.yaml"
    yaml.write_text(
        "runtime: claude_sdk\n"
        'starting_prompt: "go"\n'
        "stages:\n"
        "  - name: spec\n"
        "    artifacts:\n"
        "      - artifact: spec.md\n"
        "        gate: non_placeholder\n"
        "  - name: verify\n"
        "    artifacts:\n"
        "      - artifact: verify.md\n"
        "        gate: command\n"
        '        command: "pytest -q"\n',
        encoding="utf-8",
    )
    cfg = AppConfig.from_yaml(yaml)
    assert len(cfg.stages) == 2
    assert cfg.stages[0]["name"] == "spec"
    assert cfg.stages[0]["artifacts"][0]["artifact"] == "spec.md"
    assert cfg.stages[1]["artifacts"][0]["command"] == "pytest -q"


def test_appconfig_parses_multi_artifact_stage(tmp_path) -> None:
    yaml = tmp_path / "workflow-controller.yaml"
    yaml.write_text(
        "runtime: claude_sdk\n"
        'starting_prompt: "go"\n'
        "stages:\n"
        "  - name: dev\n"
        "    artifacts:\n"
        "      - artifact: LOG.md\n"
        "        gate: non_placeholder\n"
        "      - artifact: issues/\n"
        "        gate: exists\n",
        encoding="utf-8",
    )
    cfg = AppConfig.from_yaml(yaml)
    assert len(cfg.stages) == 1
    artifacts = cfg.stages[0]["artifacts"]
    assert len(artifacts) == 2
    assert artifacts[0]["artifact"] == "LOG.md"
    assert artifacts[1]["artifact"] == "issues/"


def test_appconfig_parses_shorthand_artifact_list(tmp_path) -> None:
    """artifact 为列表的缩略写法能正确解析。"""
    yaml = tmp_path / "workflow-controller.yaml"
    yaml.write_text(
        "runtime: claude_sdk\n"
        'starting_prompt: "go"\n'
        "stages:\n"
        "  - name: design\n"
        "    artifacts:\n"
        "      - artifact:\n"
        "          - DESIGN.md\n"
        "          - PLAN.md\n"
        "        gate: non_placeholder\n",
        encoding="utf-8",
    )
    cfg = AppConfig.from_yaml(yaml)
    assert len(cfg.stages) == 1
    artifact_val = cfg.stages[0]["artifacts"][0]["artifact"]
    assert isinstance(artifact_val, list)
    assert artifact_val == ["DESIGN.md", "PLAN.md"]


def test_appconfig_defaults_to_empty_stages(tmp_path) -> None:
    yaml = tmp_path / "workflow-controller.yaml"
    yaml.write_text("runtime: claude_sdk\n", encoding="utf-8")
    cfg = AppConfig.from_yaml(yaml)
    assert cfg.stages == []


def test_sessionconfig_carries_stages() -> None:
    stages = [
        {"name": "spec", "artifacts": [{"artifact": "spec.md", "gate": "non_placeholder"}]},
    ]
    cfg = SessionConfig(name="t", role="worker", stages=stages)
    assert cfg.stages == stages


def test_sessionconfig_defaults_to_empty_stages() -> None:
    cfg = SessionConfig(name="t", role="worker")
    assert cfg.stages == []
