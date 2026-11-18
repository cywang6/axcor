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

"""Plugin SDK — public API for plugin developers.

Import this module to build a plugin::

    from workflow_controller.plugin.sdk import Plugin, PluginContext, PluginTabDef

    class MyPlugin(Plugin):
        def install(self, ctx: PluginContext):
            ctx.bus.observe("tool_use", self._on_tool_use)
            ctx.register_event_type("myplugin:custom", description="...")
            self.register_hook_routes(ctx.hook_server, [...])

        def get_web_manifest(self):
            return [PluginTabDef(...)]
"""

__all__ = [
    "EventTypeDef",
    "FormatInterpreter",
    "InterceptResult",
    "IterationGateResult",
    "Plugin",
    "PluginConfig",
    "PluginContext",
    "PluginRouter",
    "PluginServices",
    "PluginTabDef",
    "PreToolUseResult",
    "RunPanelDef",
    "StopHookResult",
    "plugin_router",
]

from workflow_controller.event.event_bus import InterceptResult
from workflow_controller.event.type_registry import EventTypeDef
from workflow_controller.plugin.base import (
    FormatInterpreter,
    IterationGateResult,
    Plugin,
    PluginConfig,
    PluginTabDef,
    PreToolUseResult,
    RunPanelDef,
    StopHookResult,
)
from workflow_controller.plugin.context import PluginContext, PluginServices
from workflow_controller.plugin.web_router import PluginRouter, plugin_router
