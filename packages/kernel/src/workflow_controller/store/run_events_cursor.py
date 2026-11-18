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

"""run 事件游标——run_events 与 events 两表的合并增量读取。

切页全序键为 (id, source)：页内各表行始终是该表读集的主键前缀，
页外行主键必大于页内该表最大主键，高水位翻页因此不漏行不重复。
timestamp 不能作切页键——events 的 uuid 重放在原主键行上改写
时间戳、run_events 批量写的时间戳取点可倒挂，时间戳并不随主键
单调。(timestamp, source, id) 的全量终排是另一条呈现序，与切页序
解耦。
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_controller.store.event_store import EventStore

_SOURCE_RUN_EVENTS = "run_events"
_SOURCE_EVENTS = "events"
# 同主键表源定序：run_events < events
_SOURCE_RANK = {_SOURCE_RUN_EVENTS: 0, _SOURCE_EVENTS: 1}
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


class RunEventsCursor:
    """run 事件增量游标：一次调用返回合并序事件与下轮游标。"""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @staticmethod
    def _encode_cursor(run_events_id: int, events_id: int) -> str:
        """高水位编码为不可透写游标。"""
        raw = json.dumps({"re": run_events_id, "ev": events_id}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, int]:
        """解游标为两表高水位；非法游标 raise ValueError。"""
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            doc = json.loads(base64.urlsafe_b64decode(padded.encode()))
            re_id, ev_id = int(doc["re"]), int(doc["ev"])
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"游标不合法：{cursor!r}") from e
        if re_id < 0 or ev_id < 0:
            raise ValueError(f"游标不合法：{cursor!r}")
        return re_id, ev_id

    @staticmethod
    def _cursor_for(rows: list[dict[str, Any]], re_since: int, ev_since: int) -> str:
        """按返回批次推进两表读取进度；未触达的表保持原进度。"""
        re_max, ev_max = re_since, ev_since
        for row in rows:
            if row["source"] == _SOURCE_RUN_EVENTS:
                re_max = max(re_max, row["id"])
            else:
                ev_max = max(ev_max, row["id"])
        return RunEventsCursor._encode_cursor(re_max, ev_max)

    def fetch(
        self, run_id: str, cursor: str | None = None, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], str, int]:
        """自游标起拉合并序事件；返回 (事件列表, 下轮游标, 实际生效页长)。

        生效页长随应答回传：请求值可被 _MAX_LIMIT 钳制，翻页方「页内
        不足一页 = 取尽」的终止判据以此为准，不自持镜像常量。
        """
        bound = min(max(limit or _DEFAULT_LIMIT, 1), _MAX_LIMIT)
        re_since, ev_since = self._decode_cursor(cursor) if cursor else (0, 0)
        merged = sorted(
            [
                *(
                    dict(r, source=_SOURCE_RUN_EVENTS)
                    for r in self._store.get_run_events_sync(run_id, re_since, limit=bound)
                ),
                *(
                    dict(r, source=_SOURCE_EVENTS)
                    for r in self._store.get_stamped_events_sync(run_id, ev_since, limit=bound)
                ),
            ],
            key=lambda r: (r["id"], _SOURCE_RANK[r["source"]]),
        )
        page = merged[:bound]
        return page, self._cursor_for(page, re_since, ev_since), bound
