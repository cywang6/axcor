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

"""Shared Claude permission-mode normalization (claude_cli + claude_sdk).

Claude Code refuses ``bypassPermissions`` (mapped to the CLI flag
``--dangerously-skip-permissions``) when the effective user is root/sudo and
the subprocess exits immediately. Both Claude drivers funnel the requested
mode through :func:`effective_permission_mode` so a root run degrades to
``acceptEdits`` instead of hard-failing.

Operators that run workflow-controller as root inside a *genuinely* sandboxed container can
opt back into full autonomy with ``WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS=1``
(see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_warned_root_downgrade = False


def _is_root() -> bool:
    """Return True when the effective user id is 0 (root / sudo).

    Kept as a narrow seam so tests patch ``_is_root`` instead of the process
    global ``os.geteuid`` attribute.
    """
    return os.geteuid() == 0


def effective_permission_mode(permission_mode: str) -> str:
    """Return the permission mode to actually pass to Claude Code.

    ``bypassPermissions`` requested by a root process is downgraded to
    ``acceptEdits`` (logged once per process — multi-turn orchestrator loops
    must not repeat the same warning every query). Non-root users and any
    explicitly configured non-bypass mode pass through unchanged.
    ``WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS=1`` opts out of the downgrade for
    deployments running inside a genuinely sandboxed container.
    """
    global _warned_root_downgrade

    if permission_mode != "bypassPermissions" or not _is_root():
        return permission_mode
    if _keep_bypass_permissions():
        return permission_mode
    if not _warned_root_downgrade:
        _warned_root_downgrade = True
        logger.warning(
            "Claude driver runs as root; downgrading permission mode from "
            "bypassPermissions to acceptEdits "
            "(set WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS=1 to keep "
            "bypassPermissions inside a genuine sandbox)",
        )
    return "acceptEdits"


def _keep_bypass_permissions() -> bool:
    value = os.environ.get("WORKFLOW_CONTROLLER_CLAUDE_KEEP_BYPASS_PERMISSIONS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}
