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

"""源提交 1aa0c65 的用户要求和项目切换修复在 Run 入口上的行为。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import yaml

from workflow_controller.api.run_api import RunAPI
from workflow_controller.plugins.graph.plugin import GRAPH_FORMAT

from .run_plane_helpers import GraphRunPlaneEnv, await_run_status

if TYPE_CHECKING:
    from pathlib import Path


async def test_agent_receives_prompt_file_template_and_environment(tmp_path: Path) -> None:
    env = GraphRunPlaneEnv(tmp_path)
    program = tmp_path / "agent.yaml"
    program.write_text(
        yaml.safe_dump(
            {
                "workflow": "prompt",
                "variables": {"PROMPT_FILE": "{{USER_PROMPT}}"},
                "nodes": [
                    {
                        "id": "one",
                        "task_type": "agent",
                        "prompt": "Read {{PROMPT_FILE}}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = await env.start_graph_run(
        yaml_path=program,
        params={
            "starting_prompt": json.dumps("用户要求"),
            "variables": json.dumps({"OP": "fallback"}),
        },
    )
    await await_run_status(env, run_id, {"completed"})
    prompt_file = tmp_path / "wd" / ".workflow" / run_id / "user_prompt.md"
    assert prompt_file.read_text(encoding="utf-8") == "用户要求\n"
    cfg = env.so.spawn_calls[0]["cfg"]
    assert cfg.env_overrides == {
        "WORK_DIR": str(tmp_path / "wd"),
        "USER_PROMPT": str(prompt_file),
    }
    assert f"$USER_PROMPT={prompt_file}" in cfg.initial_prompt
    assert f"Read {prompt_file}" in cfg.initial_prompt


async def test_bash_and_verifier_read_prompt_env_and_restore_keeps_edits(
    tmp_path: Path,
) -> None:
    env = GraphRunPlaneEnv(tmp_path)
    program = tmp_path / "bash.yaml"
    program.write_text(
        yaml.safe_dump(
            {
                "workflow": "prompt",
                "variables": {"OP": "默认要求"},
                "nodes": [
                    {
                        "id": "one",
                        "task_type": "bash",
                        "command": 'cat "$USER_PROMPT" > result.txt',
                        "acceptance": {
                            "enabled": True,
                            "type": "script",
                            "script": 'test "$WORK_DIR" = "$PWD" && test -s "$USER_PROMPT"',
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = await env.start_graph_run(yaml_path=program)
    await await_run_status(env, run_id, {"completed"})
    prompt_file = tmp_path / "wd" / ".workflow" / run_id / "user_prompt.md"
    assert prompt_file.read_text(encoding="utf-8") == "默认要求\n"
    assert (tmp_path / "wd" / "result.txt").read_text(encoding="utf-8") == "默认要求\n"

    prompt_file.write_text("手工修改\n", encoding="utf-8")
    snapshot = await env.coord.load_driver_snapshot(run_id)
    getattr(env.plugin, "_drivers").clear()
    run = await env.coord.get_run(run_id)
    restored = env.plugin.restore_driver(run, snapshot)
    assert restored.snapshot()["user_prompt_path"] == str(prompt_file)
    assert prompt_file.read_text(encoding="utf-8") == "手工修改\n"


async def test_relative_program_uses_current_project_and_reports_bad_path(
    tmp_path: Path,
) -> None:
    env = GraphRunPlaneEnv(tmp_path)
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    (project_b / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": "new-project",
                "nodes": [{"id": "one", "task_type": "bash", "command": "true"}],
            }
        ),
        encoding="utf-8",
    )
    api = RunAPI(env.coord)
    run = await api.start("", "workflow.yaml", fmt=GRAPH_FORMAT, work_dir=str(project_b))
    await await_run_status(env, run.run_id, {"completed"})
    assert run.program_ref == str(project_b / "workflow.yaml")
    assert env.driver_of(run.run_id).work_dir == project_b

    with pytest.raises(ValueError, match="程序文件不存在"):
        await api.start("", "missing.yaml", fmt=GRAPH_FORMAT, work_dir=str(project_b))
    (tmp_path / "outside.yaml").write_text("format: graph-yaml@1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不在当前项目"):
        await api.start("", "../outside.yaml", fmt=GRAPH_FORMAT, work_dir=str(project_b))


@pytest.mark.parametrize(
    "selected,pinned",
    [
        ("provider-model", False),
        ("custom-model", True),
    ],
)
async def test_graph_member_keeps_load_time_model_pin(
    tmp_path: Path,
    selected: str,
    pinned: bool,
) -> None:
    env = GraphRunPlaneEnv(tmp_path)

    def _get_provider_model(_index):
        return "provider-model"

    getattr(env.plugin, "_ctx").services.get_provider_model = _get_provider_model
    run_id = await env.start_graph_run(
        [{"id": "one", "task_type": "agent", "prompt": "work"}],
        params={"model": json.dumps(selected), "provider_index": "0"},
    )
    await await_run_status(env, run_id, {"completed"})
    cfg = env.so.spawn_calls[0]["cfg"]
    assert cfg.model == selected
    assert cfg.model_pinned is pinned
    assert env.driver_of(run_id).snapshot()["options"]["model_pinned"] is pinned


async def test_bad_provider_index_reports_value_error(tmp_path: Path) -> None:
    env = GraphRunPlaneEnv(tmp_path)
    with pytest.raises(ValueError, match="provider_index 需为整数"):
        await env.start_graph_run(
            [{"id": "one", "task_type": "agent", "prompt": "work"}],
            params={"provider_index": "abc"},
        )


async def test_restore_recreates_missing_user_prompt_file(tmp_path: Path) -> None:
    env = GraphRunPlaneEnv(tmp_path)
    program = tmp_path / "wf.yaml"
    program.write_text(
        yaml.safe_dump(
            {
                "workflow": "prompt",
                "variables": {"OP": "默认要求"},
                "nodes": [{"id": "one", "task_type": "bash", "command": "true"}],
            }
        ),
        encoding="utf-8",
    )
    run_id = await env.start_graph_run(yaml_path=program)
    await await_run_status(env, run_id, {"completed"})
    prompt_file = tmp_path / "wd" / ".workflow" / run_id / "user_prompt.md"
    snapshot = await env.coord.load_driver_snapshot(run_id)
    snapshot.pop("user_prompt_path", None)
    prompt_file.unlink()
    getattr(env.plugin, "_drivers").clear()
    run = await env.coord.get_run(run_id)
    restored = env.plugin.restore_driver(run, snapshot)
    assert restored.snapshot()["user_prompt_path"] == str(prompt_file)
    assert prompt_file.read_text(encoding="utf-8") == "默认要求\n"
