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

"""AnthropicHttpClient — deterministic raw-HTTP client for the reviewer.

A single ``POST /v1/messages`` to an Anthropic-compatible endpoint. No agent
loop, no tools, no Claude-Code system-prompt framing — one request, one text
reply. The reviewer is a single-call LLM judge, so routing it through this
client (instead of the worker's agent-loop driver) removes the run-to-run
variance (tool exploration + CC framing) that made its decisions flaky.

Works with GLM (``open.bigmodel.cn/api/anthropic``) or any Anthropic-compatible
endpoint via the ``ANTHROPIC_*`` env — same creds the other drivers use.

This is intentionally NOT a worker runtime: workers need the agent loop and
tools, which a single HTTP call can't provide. It is instantiated directly by
the reviewer runner, not routed through ``create_driver`` (so the worker's
``WORKFLOW_CONTROLLER_DRIVER`` setting can't pull the reviewer off the deterministic path).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A trailing CLI-only context-variant tag like ``glm-5.2[1m]`` or
# ``claude-sonnet-4-6[udc]`` is rejected by the raw /v1/messages API (the
# brackets are a CLI-layer context-selection label, not part of the model id).
# Strip any trailing [...] so the driver is robust to a caller passing the
# env's variant-tagged model name.
_VARIANT_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")


def _strip_model_variant(model: str | None) -> str:
    """Strip a trailing ``[...]`` CLI variant tag and trim whitespace."""
    if not model:
        return ""
    return _VARIANT_SUFFIX.sub("", model.strip())


_DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_MESSAGES_FORMAT_ERROR = "response is not Anthropic Messages format"


def _resolve_endpoint(env: dict[str, str] | None) -> tuple[str, dict[str, str]]:
    """Resolve ``(base_url, auth_headers)`` from ANTHROPIC_* env.

    Bearer (``ANTHROPIC_AUTH_TOKEN``) takes precedence over x-api-key
    (``ANTHROPIC_API_KEY``), matching the auth rotator's third-party handling.
    ``base_url`` defaults to the native Anthropic endpoint when unset, so a
    native ``sk-ant-`` key (no custom base_url) works without extra config —
    check_auth's lightweight probe uses this to test any key uniformly. Returns
    ``(base, {})`` when credentials are absent (the driver surfaces a RESULT
    error so the reviewer / check_auth fall back safely).
    """
    env = env or {}
    raw = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/") or _DEFAULT_BASE_URL
    # Strip trailing /v1 so we don't double it (e.g. dashscope base already has /v1)
    base = raw[:-3] if raw.endswith("/v1") else raw
    auth: dict[str, str] = {}
    bearer = env.get("ANTHROPIC_AUTH_TOKEN")
    api_key = env.get("ANTHROPIC_API_KEY")
    if bearer:
        auth["Authorization"] = f"Bearer {bearer}"
    elif api_key:
        auth["x-api-key"] = api_key
    return base, auth


@dataclass
class HttpResult:
    text: str
    is_error: bool
    error: str


async def _default_post(
    base_url: str,
    auth_headers: dict[str, str],
    body: dict[str, object],
    timeout: float,
) -> HttpResult:
    """Real HTTP POST via httpx. Isolated as a module function so tests inject
    a fake ``post`` callable without touching network code.

    Single Anthropic ``/v1/messages`` protocol
    """
    import httpx

    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        **auth_headers,
    }
    endpoint = f"{base_url}/v1/messages"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
    except httpx.HTTPError as exc:
        return HttpResult(
            text="",
            is_error=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    if resp.status_code != 200:
        return HttpResult(
            text="",
            is_error=True,
            error=f"HTTP {resp.status_code}: {resp.text}",
        )
    return _parse_response(resp.json(), body)


def _parse_response(data: dict[str, Any], body: dict[str, Any]) -> HttpResult:
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        return HttpResult(
            text="",
            is_error=True,
            error=ANTHROPIC_MESSAGES_FORMAT_ERROR,
        )
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    thinking = body.get("thinking")
    thinking_on = isinstance(thinking, dict) and thinking.get("type") in (
        "enabled",
        "adaptive",
    )
    if thinking_on and data.get("stop_reason") == "max_tokens":
        return HttpResult(text=text, is_error=True, error="thinking output reached max_tokens")
    if thinking_on and not text.strip():
        return HttpResult(text="", is_error=True, error="thinking response has no text block")
    return HttpResult(text=text, is_error=False, error="")


class AnthropicHttpClient:
    """Single-call raw-HTTP client. See module docstring."""

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        post: Callable[..., Awaitable[HttpResult]] | None = None,
    ) -> None:
        self._timeout = timeout
        self._post = post or _default_post
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def request(
        self,
        prompt: str,
        *,
        model: str = "",
        env: dict[str, str] | None = None,
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int = 1024,
        thinking_budget_tokens: int = 2048,
        thinking_type: str = "omitted",
    ) -> HttpResult:
        base, auth = _resolve_endpoint(env)
        if not base or not auth:
            return HttpResult(
                text="",
                is_error=True,
                error=(
                    "anthropic_http: a token (ANTHROPIC_AUTH_TOKEN / "
                    "ANTHROPIC_API_KEY) is required in env"
                ),
            )

        # Empty model falls back to the env's ANTHROPIC_MODEL (mirrors the SDK
        # driver's `model or None` → env defaulting). Variant tags stripped.
        resolved_model = _strip_model_variant(model)
        if not resolved_model:
            resolved_model = _strip_model_variant((env or {}).get("ANTHROPIC_MODEL", ""))

        body: dict[str, object] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            body["system"] = system_prompt
        mode = thinking_type
        if mode not in ("omitted", "enabled", "adaptive", "disabled"):
            return HttpResult(
                text="",
                is_error=True,
                error=f"unsupported thinking type: {mode}",
            )
        if mode == "enabled":
            budget = max(1024, int(thinking_budget_tokens))
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(int(max_tokens), budget + 1024)
        elif mode in ("adaptive", "disabled"):
            body["thinking"] = {"type": mode}
        if temperature is not None and mode in ("omitted", "disabled"):
            body["temperature"] = temperature

        self._running = True
        try:
            return await self._post(base, auth, body, self._timeout)
        finally:
            self._running = False
