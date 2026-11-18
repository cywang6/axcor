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

"""Shared helper to extract a JSON object from arbitrary LLM output."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first parseable JSON *object* in *text*, or None.

    Tries the whole text first, then the greedy ``{...}`` slice — this handles
    both clean JSON and JSON embedded in surrounding prose/markdown.
    """
    candidates = [text]
    match = _JSON_OBJECT_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
