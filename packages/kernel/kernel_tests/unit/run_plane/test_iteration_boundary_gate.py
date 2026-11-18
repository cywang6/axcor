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

import asyncio

from workflow_controller.plugin.base import IterationGateResult, Plugin, PluginConfig


def test_iteration_gate_result_defaults():
    r = IterationGateResult(action="block", feedback="do X", reason="jump")
    assert r.action == "block"
    assert r.feedback == "do X"
    assert r.reason == "jump"
    s = IterationGateResult(action="stop")
    assert s.feedback == "" and s.reason == ""


def test_base_on_iteration_boundary_returns_pass():
    class _P(Plugin):
        def install(self, ctx):
            pass

        def uninstall(self) -> None:
            pass

    p = _P(PluginConfig(name="x", version="0", enabled=True), None)  # type: ignore[arg-type]
    result = asyncio.run(p.on_iteration_boundary("sid", None, 1))
    assert result.action == "pass"
