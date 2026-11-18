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

"""agent_traceback 插件测试装配：PluginContext/端口经生产装配路径接线，防装配漂移。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugin.context import PluginContext
from workflow_controller.plugin.registry import _build_plugin_services
from workflow_controller.plugins.agent_traceback.plugin import AgentTracebackPlugin

_DEFAULT_EXTRA: dict[str, Any] = {
    "snapshot_timeout_seconds": 10.0,
    "max_checkpoints": 200,
    "snapshot_dir": ".workflow-controller/snapshots",
    "ignore": [],
}


def install_plugin(
    orch: Any,
    *,
    enabled: bool = True,
    extra: dict[str, Any] | None = None,
) -> AgentTracebackPlugin:
    """构造并 install agent_traceback 插件（services 走生产装配函数）。"""
    plugin = AgentTracebackPlugin(
        PluginConfig(
            name="agent_traceback",
            version="0.1.0",
            enabled=enabled,
            extra={**_DEFAULT_EXTRA, **(extra or {})},
        ),
        Path(__file__),
    )
    plugin.install(
        PluginContext(
            bus=orch.bus,
            type_registry=orch.bus.type_registry,
            hook_server=None,
            work_dir=Path(getattr(orch.config, "project_dir", ".") or ".").expanduser(),
            services=_build_plugin_services(orch),
        )
    )
    getattr(getattr(orch, "_plugin_registry"), "_plugins").append(plugin)
    return plugin
