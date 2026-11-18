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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class HookConfigGenerator:
    def __init__(self, port: int = 18765, host: str = "localhost") -> None:
        self.base_url = f"http://{host}:{port}"

    def generate_settings(self) -> dict[str, Any]:
        hook_events = [
            "PostToolUse",
            "PostToolUseFailure",
            "PreToolUse",
            "Stop",
            "Notification",
            "SessionStart",
            "SessionEnd",
            "SubagentStart",
            "SubagentStop",
        ]
        hooks: dict[str, list[dict[str, Any]]] = {}
        for event in hook_events:
            path = (
                event.lower()
                .replace("sessionstart", "session-start")
                .replace("sessionend", "session-end")
                .replace("posttooluse", "post-tool-use")
                .replace("post-tool-usefailure", "post-tool-use-failure")
                .replace("pretooluse", "pre-tool-use")
                .replace("subagentstart", "subagent-start")
                .replace("subagentstop", "subagent-stop")
            )
            hooks[event] = [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "http",
                            "url": f"{self.base_url}/hooks/{path}",
                            "timeout": 300,
                        }
                    ],
                }
            ]
        return {"hooks": hooks}

    def write_settings(self, project_dir: Path) -> None:
        self.write_settings_to_file(project_dir / ".claude" / "settings.json")

    def write_settings_to_file(self, settings_file: Path) -> None:
        settings = self.generate_settings()
        settings_file.parent.mkdir(parents=True, exist_ok=True)

        existing: dict[str, Any] = {}
        if settings_file.exists():
            existing = json.loads(settings_file.read_text())

        existing["hooks"] = settings["hooks"]
        settings_file.write_text(json.dumps(existing, indent=2))
