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
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Schedule a fire-and-forget coroutine safely.

    Keeps a strong reference so the task is not garbage-collected before it
    finishes, and logs any exception instead of letting it vanish silently.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background task %r failed", task.get_name(), exc_info=exc)
