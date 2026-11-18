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

import json
from typing import TYPE_CHECKING

from workflow_controller.hooks.hook_config import HookConfigGenerator

if TYPE_CHECKING:
    from pathlib import Path


class TestWriteSettings:
    @staticmethod
    def test_write_settings_creates_dot_claude_settings_json_in_project_dir(
        tmp_path: Path,
    ) -> None:
        gen = HookConfigGenerator(port=18999, host="127.0.0.1")
        gen.write_settings(tmp_path)

        expected = tmp_path / ".claude" / "settings.json"
        assert expected.exists()
        data = json.loads(expected.read_text())
        assert "hooks" in data
        assert "PreToolUse" in data["hooks"]

    @staticmethod
    def test_write_settings_preserves_existing_non_hook_keys(
        tmp_path: Path,
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_file = claude_dir / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(*)"]},
                    "hooks": {"old": "should-be-replaced"},
                }
            )
        )

        gen = HookConfigGenerator()
        gen.write_settings(tmp_path)

        data = json.loads(settings_file.read_text())
        assert data["permissions"] == {"allow": ["Bash(*)"]}
        assert data["hooks"] != {"old": "should-be-replaced"}
        assert "PreToolUse" in data["hooks"]


class TestWriteSettingsToFile:
    @staticmethod
    def test_writes_to_exact_path_no_double_dot_claude(
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "any" / "place" / "settings.json"

        gen = HookConfigGenerator()
        gen.write_settings_to_file(target)

        assert target.exists()
        assert not (target.parent / ".claude").exists()
        data = json.loads(target.read_text())
        assert "hooks" in data
        assert "Stop" in data["hooks"]

    @staticmethod
    def test_creates_parent_dirs_if_missing(tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dirs" / "settings.json"

        gen = HookConfigGenerator()
        gen.write_settings_to_file(target)

        assert target.exists()

    @staticmethod
    def test_preserves_existing_keys_at_target_path(
        tmp_path: Path,
    ) -> None:
        target = tmp_path / ".claude" / "settings.json"
        target.parent.mkdir()
        target.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["x"]},
                }
            )
        )

        gen = HookConfigGenerator()
        gen.write_settings_to_file(target)

        data = json.loads(target.read_text())
        assert data["permissions"] == {"allow": ["x"]}
        assert "PreToolUse" in data["hooks"]

    @staticmethod
    def test_emitted_hook_timeout_is_5_minutes(tmp_path: Path) -> None:
        gen = HookConfigGenerator()
        target = tmp_path / ".claude" / "settings.json"
        gen.write_settings_to_file(target)

        data = json.loads(target.read_text())
        for hook_event in ("PreToolUse", "Stop", "PostToolUse"):
            timeout = data["hooks"][hook_event][0]["hooks"][0]["timeout"]
            assert timeout >= 120, f"{hook_event} hook timeout {timeout} too short for LLM mode"
