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

"""graph 插件的面板贡献声明：format → 前端组件 id 的映射元数据。"""

from __future__ import annotations

from pathlib import Path

from workflow_controller.plugin.base import PluginConfig
from workflow_controller.plugins.graph.plugin import GraphPlugin
from workflow_controller.plugins.graph.projection import PROJECTION_SCHEMA_VERSION


def test_run_panel_declaration_matches_driver_factory_format() -> None:
    """声明的格式串与驱动工厂派发键一致、版本上限与投影版本一致（漂移即面板路由失效）。"""
    plugin = GraphPlugin(
        PluginConfig(name="graph", version="0.1.0", enabled=True),
        Path("config.yaml"),
    )

    panels = plugin.get_run_panels()
    assert [p.format for p in panels] == list(plugin.get_run_driver_factories())
    assert panels[0].component == "graph.RunDagPanel"
    assert panels[0].schema_version == PROJECTION_SCHEMA_VERSION
