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

import io
import json
import logging
from typing import Any

import pytest

from workflow_controller.driver import hooks
from workflow_controller.driver.hooks import CodexHookConfigWriter, relay_hook


def _patch_stop_urlopen_payload(monkeypatch, payload: bytes) -> None:
    """把 hooks 的 urlopen 打成固定返回 payload 的 200 响应。"""

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return payload

    monkeypatch.setattr(
        hooks.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )


def _relay_stop_hook() -> str:
    """以标准 Stop payload 与本地 hook server 地址调用 relay_hook。"""
    return relay_hook(
        "Stop",
        {"session_id": "codex-1"},
        {
            "WORKFLOW_CONTROLLER_HOOK_SERVER_URL": "http://127.0.0.1:18765",
        },
    )


def test_hook_config_writer_rejects_unknown_configuration(tmp_path):
    writer = CodexHookConfigWriter()
    writer.prepare(tmp_path)
    config = json.loads((tmp_path / ".codex" / "hooks.json").read_text())

    assert config["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 330
    writer.prepare(tmp_path)
    (tmp_path / ".codex" / "hooks.json").write_text("{}")
    try:
        writer.prepare(tmp_path)
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown hooks must be rejected")


def test_make_driver_hooks_requires_effective_runtime_and_active_hook_server(
    tmp_path,
    monkeypatch,
):
    """验证 hooks 装配使用 effective runtime 且要求服务在役。"""
    from workflow_controller.config.settings import SessionConfig
    from workflow_controller.core.orchestrator import Orchestrator
    from workflow_controller.session.session import Session

    monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "codex_cli")
    orchestrator = Orchestrator.create_default(config_dir=tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    session = Session(
        config=SessionConfig(
            name="codex",
            role="worker",
            cwd=str(project_dir),
        )
    )
    getattr(orchestrator.pool, "_sessions")[session.id] = session

    driver_hooks = orchestrator.make_driver_hooks(session.id)
    assert driver_hooks.runtime_hook_config_valid is None
    assert not (project_dir / ".codex" / "hooks.json").exists()

    monkeypatch.setattr(orchestrator, "_hook_server", object())
    driver_hooks = orchestrator.make_driver_hooks(session.id)
    assert driver_hooks.runtime_hook_config_valid is not None
    assert driver_hooks.runtime_hook_config_valid()
    assert (project_dir / ".codex" / "hooks.json").exists()


@pytest.mark.parametrize(
    "event,path",
    [
        ("Stop", "stop"),
        ("SubagentStart", "subagent-start"),
    ],
)
def test_relay_hook_routes_event_and_maps_session(monkeypatch, event, path):
    request_data = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            result = {"decision": "block"} if event == "Stop" else {}
            return json.dumps(result).encode()

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        request_data["url"] = request.full_url
        request_data["auth"] = request.headers.get("Authorization")
        request_data["timeout"] = timeout
        request_data["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)
    output = relay_hook(
        event,
        {
            "session_id": "codex-1",
            "last_assistant_message": None,
        },
        {
            "WORKFLOW_CONTROLLER_HOOK_SERVER_URL": "http://127.0.0.1:18765/",
        },
    )

    assert request_data["url"] == f"http://127.0.0.1:18765/hooks/{path}"
    assert "Authorization" not in request_data
    expected_timeout = 320 if event == "Stop" else 10
    assert request_data["timeout"] == expected_timeout
    payload = request_data["payload"]
    assert payload["session_id"] == "codex-1"
    assert payload["codex_input"]["session_id"] == "codex-1"
    if event == "Stop":
        assert (
            json.loads(output)["reason"] == "Workflow Controller reviewer requested continuation."
        )
    else:
        assert output is None


def test_relay_hook_retries_transient_stop_failure(monkeypatch):
    """Stop 转发快速失败时会重试。"""
    calls = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"decision":"block","reason":"continue"}'

    def fake_urlopen(*_args: object, **_kwargs: object):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("unavailable")
        return FakeResponse()

    monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)
    output = _relay_stop_hook()

    assert len(calls) == 2
    assert json.loads(output)["reason"] == "continue"


def test_relay_hook_defaults_invalid_stop_reason(monkeypatch, caplog):
    """Stop block 的 reason 类型非法时使用默认继续文案。"""
    _patch_stop_urlopen_payload(
        monkeypatch, json.dumps({"decision": "block", "reason": 7}).encode()
    )
    with caplog.at_level(logging.ERROR, logger="workflow_controller.driver.hooks"):
        output = _relay_stop_hook()

    assert json.loads(output) == {
        "decision": "block",
        "reason": "Workflow Controller reviewer requested continuation.",
    }
    assert "Invalid Codex stop block reason" in caplog.text


def test_relay_hook_defaults_blank_stop_reason(monkeypatch):
    """Stop block 的空白 reason 使用默认继续文案。"""
    _patch_stop_urlopen_payload(monkeypatch, b'{"decision":"block","reason":"   "}')
    output = _relay_stop_hook()

    assert json.loads(output) == {
        "decision": "block",
        "reason": "Workflow Controller reviewer requested continuation.",
    }


def test_codex_hook_main_reads_input_and_fails_open(monkeypatch, capsys):
    captured = {}

    def fake_relay(event, codex_input, env):
        captured.update(event=event, codex_input=codex_input, env=env)
        return '{"decision":"block","reason":"continue"}'

    monkeypatch.setattr(hooks, "relay_hook", fake_relay)
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO('{"session_id":"codex-1"}'))
    assert hooks.run_codex_hook("Stop") == 0
    assert captured["event"] == "Stop"
    assert captured["codex_input"]["session_id"] == "codex-1"
    assert capsys.readouterr().out == '{"decision":"block","reason":"continue"}\n'

    def failing_relay(*_args: object) -> str | None:
        raise RuntimeError("unreachable")

    monkeypatch.setattr(hooks, "relay_hook", failing_relay)
    assert hooks.run_codex_hook("Stop") == 0
    assert capsys.readouterr().out == "{}\n"
