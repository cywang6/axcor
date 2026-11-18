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

"""Unit tests for AnthropicHttpClient — the deterministic raw-HTTP client.
No live network: the HTTP POST is injected via the ``post`` callable so the
request / body-shaping logic is tested in isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from workflow_controller.driver.anthropic_http import (
    AnthropicHttpClient,
    HttpResult,
    _parse_response,
    _resolve_endpoint,
    _strip_model_variant,
)

_CREDS = {
    "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "tok",
}


def _make_client(
    *,
    text: str = "OK",
    is_error: bool = False,
    error: str = "",
    captured: dict[str, Any] | None = None,
) -> AnthropicHttpClient:
    """Build a client whose HTTP POST is a fake returning a fixed result,
    optionally capturing the request body/endpoint for assertions.
    """

    async def fake_post(base_url, auth_headers, body, timeout):  # type: ignore[no-untyped-def]
        if captured is not None:
            captured["base"] = base_url
            captured["auth"] = dict(auth_headers)
            captured["body"] = body
        return HttpResult(text=text, is_error=is_error, error=error)

    return AnthropicHttpClient(post=fake_post)  # type: ignore[arg-type]


def _request(
    client: AnthropicHttpClient,
    *,
    model: str = "",
    env: dict[str, str] | None = None,
    temperature: float | None = None,
) -> HttpResult:
    return asyncio.run(
        client.request(
            "hello",
            model=model,
            env=env if env is not None else _CREDS,
            temperature=temperature,
        )
    )


# ---- _strip_model_variant ----


class TestStripModelVariant:
    @staticmethod
    def test_strips_bracket_variant() -> None:
        assert _strip_model_variant("glm-5.2[1m]") == "glm-5.2"

    @staticmethod
    def test_no_bracket_unchanged() -> None:
        assert _strip_model_variant("glm-4.6") == "glm-4.6"

    @staticmethod
    def test_claude_name_unchanged() -> None:
        assert _strip_model_variant("claude-sonnet-4-6") == "claude-sonnet-4-6"

    @staticmethod
    def test_empty() -> None:
        assert _strip_model_variant("") == ""

    @staticmethod
    def test_none() -> None:
        assert _strip_model_variant(None) == ""

    @staticmethod
    def test_trims_whitespace() -> None:
        assert _strip_model_variant("  glm-5.2[1m]  ") == "glm-5.2"


# ---- _resolve_endpoint ----


class TestResolveEndpoint:
    @staticmethod
    def test_bearer_auth() -> None:
        base, auth = _resolve_endpoint(
            {
                "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic/",
                "ANTHROPIC_AUTH_TOKEN": "tok",
            }
        )
        assert base == "https://open.bigmodel.cn/api/anthropic"
        assert auth == {"Authorization": "Bearer tok"}

    @staticmethod
    def test_apikey_auth() -> None:
        _, auth = _resolve_endpoint(
            {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "ANTHROPIC_API_KEY": "key",
            }
        )
        assert auth == {"x-api-key": "key"}

    @staticmethod
    def test_bearer_precedence_over_apikey() -> None:
        _, auth = _resolve_endpoint(
            {
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_API_KEY": "key",
            }
        )
        assert auth == {"Authorization": "Bearer tok"}

    @staticmethod
    def test_missing_base_defaults_to_anthropic() -> None:
        """无 BASE_URL（原生 Anthropic key）→ 默认 https://api.anthropic.com,
        让 check_auth 等轻量探测能跨原生 key + 第三方 endpoint 通用。
        """
        base, _ = _resolve_endpoint({"ANTHROPIC_AUTH_TOKEN": "tok"})
        assert base == "https://api.anthropic.com"

    @staticmethod
    def test_missing_token() -> None:
        _, auth = _resolve_endpoint({"ANTHROPIC_BASE_URL": "https://x"})
        assert auth == {}


# ---- request ----


class TestRequest:
    @staticmethod
    def test_success_returns_text() -> None:
        client = _make_client(text='{"action":"let_exit"}')
        result = _request(client)
        assert result.is_error is False
        assert result.text == '{"action":"let_exit"}'

    @staticmethod
    def test_temperature_in_body_when_passed() -> None:
        cap: dict[str, Any] = {}
        _request(_make_client(captured=cap), temperature=0.0)
        assert cap["body"]["temperature"] == 0.0

    @staticmethod
    def test_no_temperature_key_when_none() -> None:
        cap: dict[str, Any] = {}
        _request(_make_client(captured=cap))
        assert "temperature" not in cap["body"]

    @staticmethod
    def test_model_variant_stripped_in_body() -> None:
        cap: dict[str, Any] = {}
        _request(_make_client(captured=cap), model="glm-5.2[1m]")
        assert cap["body"]["model"] == "glm-5.2"

    @staticmethod
    def test_empty_model_falls_back_to_env() -> None:
        cap: dict[str, Any] = {}
        env = {**_CREDS, "ANTHROPIC_MODEL": "glm-5.2[1m]"}
        _request(_make_client(captured=cap), model="", env=env)
        assert cap["body"]["model"] == "glm-5.2"

    @staticmethod
    def test_http_error_returns_error_result() -> None:
        client = _make_client(is_error=True, error="HTTP 429: throttled")
        result = _request(client)
        assert result.is_error is True
        assert "429" in result.error

    @staticmethod
    def test_missing_creds_returns_error_result() -> None:
        client = _make_client()
        result = _request(client, env={})
        assert result.is_error is True

    @staticmethod
    def test_posts_to_v1_messages_under_base() -> None:
        cap: dict[str, Any] = {}
        _request(_make_client(captured=cap))
        assert cap["base"] == "https://open.bigmodel.cn/api/anthropic"
        assert cap["auth"] == {"Authorization": "Bearer tok"}

    @staticmethod
    def test_max_tokens_default_1024() -> None:
        cap: dict[str, Any] = {}
        _request(_make_client(captured=cap))
        assert cap["body"]["max_tokens"] == 1024

    @staticmethod
    def test_max_tokens_overridable() -> None:
        cap: dict[str, Any] = {}
        client = _make_client(captured=cap)
        asyncio.run(client.request("hello", env=_CREDS, max_tokens=2048))
        assert cap["body"]["max_tokens"] == 2048

    @staticmethod
    def test_thinking_mode_shapes_request() -> None:
        caps: dict[str, dict[str, Any]] = {}
        for mode in ("disabled", "enabled", "adaptive", "omitted"):
            cap: dict[str, Any] = {}
            client = _make_client(captured=cap)
            asyncio.run(
                client.request(
                    "review",
                    env=_CREDS,
                    temperature=0.0,
                    thinking_type=mode,
                    thinking_budget_tokens=3072 if mode == "enabled" else 2048,
                )
            )
            caps[mode] = cap

        assert caps["disabled"]["body"]["thinking"] == {"type": "disabled"}
        assert caps["disabled"]["body"]["temperature"] == 0.0
        assert caps["enabled"]["body"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 3072,
        }
        assert caps["enabled"]["body"]["max_tokens"] == 4096
        assert caps["adaptive"]["body"]["thinking"] == {"type": "adaptive"}
        assert "temperature" not in caps["adaptive"]["body"]
        assert "thinking" not in caps["omitted"]["body"]

    @staticmethod
    def test_thinking_truncation_is_an_error() -> None:
        result = _parse_response(
            {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"},
            {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        )
        assert result.is_error is True
        assert "max_tokens" in result.error

    @staticmethod
    def test_openai_response_shape_is_rejected() -> None:
        result = _parse_response(
            {"choices": [{"message": {"content": "OK"}}]},
            {},
        )
        assert result.is_error is True
        assert result.error == "response is not Anthropic Messages format"

    @staticmethod
    def test_empty_text_response_is_valid_for_health_check() -> None:
        result = _parse_response({"content": [{"type": "text", "text": " "}]}, {})
        assert result.is_error is False
        assert result.text == " "


# ---- lifecycle ----


class TestLifecycle:
    @staticmethod
    def test_not_running_initially() -> None:
        assert AnthropicHttpClient().is_running is False
