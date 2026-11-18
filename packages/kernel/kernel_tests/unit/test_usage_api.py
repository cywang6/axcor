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

from types import SimpleNamespace
from unittest.mock import Mock

from workflow_controller.api.usage_api import UsageAPI


def test_snapshot_reuses_short_lived_result_but_force_reads_new_events() -> None:
    store = Mock()
    first = SimpleNamespace(event_type="result", data={"usage": {"input_tokens": 1}})
    second = SimpleNamespace(event_type="result", data={"usage": {"input_tokens": 2}})
    store.get_usage_events_sync.side_effect = [[first], [first, second]]
    usage = UsageAPI(store)

    assert usage.snapshot().tokens_in == 1
    assert usage.snapshot().tokens_in == 1
    assert store.get_usage_events_sync.call_count == 1
    assert usage.snapshot(force=True).tokens_in == 3
    assert store.get_usage_events_sync.call_count == 2
