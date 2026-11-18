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

"""opencode provider 注入（OPENCODE_CONFIG_CONTENT）单元测试。

语义：协议（npm 包）只信事实——探测写回或显式配置的 provider_kind；
无 kind 不注入（不猜测）。
"""

from __future__ import annotations

import json

from workflow_controller.core.auth_rotator.persistence import (
    AuthProvider,
    AuthProviderType,
    KeyStatus,
)


def _make_provider(**kw) -> AuthProvider:
    return AuthProvider(
        provider_type=AuthProviderType.API_KEY,
        status=KeyStatus.UNUSED,
        key=kw.get("key", "sk-test"),
        model=kw.get("model", "volc-gateway/glm-5.2"),
        base_url=kw.get("base_url", "https://gw.example.com/v1"),
        runtime=kw.get("runtime", "opencode"),
        provider_kind=kw.get("provider_kind"),
        provider_kind_url=kw.get("provider_kind_url"),
        provider_kind_source=kw.get("provider_kind_source"),
    )


def _build_env(
    provider: AuthProvider,
    base: dict | None = None,
    *,
    default_model: str = "",
    default_url: str = "",
    runtime: str = "claude_sdk",
) -> dict:
    from workflow_controller.core.auth_rotator import AuthRotator

    class _R(AuthRotator):
        def _load_global_settings(self):
            class _S:
                def __init__(self) -> None:
                    self.default_url = default_url
                    self.default_model = default_model

            s = _S()
            return s

    out = dict(base or {})
    rotator = _R.__new__(_R)
    setattr(rotator, "_runtime", runtime)
    getattr(AuthRotator, "_build_api_env")(rotator, out, provider)
    return out


class TestInjectionSemantics:
    @staticmethod
    def test_kind_given_injects():
        out = _build_env(_make_provider(provider_kind="@ai-sdk/openai-compatible"))
        cc = out.get("OPENCODE_CONFIG_CONTENT")
        assert cc
        cfg = json.loads(cc)
        prov = cfg["provider"]["volc-gateway"]
        assert prov["npm"] == "@ai-sdk/openai-compatible"
        assert prov["options"]["baseURL"] == "https://gw.example.com/v1"
        assert prov["options"]["apiKey"] == "sk-test"
        assert list(prov["models"].keys()) == ["glm-5.2"]

    @staticmethod
    def test_deepseek_v4_pro_injects_limit():
        """已知模型注入 limit.context/output —— 否则 opencode 自动 compaction
        的溢出判断（model.limit.context === 0 短路）永不触发。
        """
        out = _build_env(
            _make_provider(
                model="volc-gateway/deepseek-v4-pro",
                provider_kind="@ai-sdk/openai-compatible",
            )
        )
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        model_def = cfg["provider"]["volc-gateway"]["models"]["deepseek-v4-pro"]
        assert model_def["name"] == "deepseek-v4-pro"
        assert model_def["limit"] == {"context": 1048576, "output": 16384}

    @staticmethod
    def test_unknown_model_omits_limit():
        """未列入已知表的模型不注入 limit，避免编造未知窗口。"""
        out = _build_env(_make_provider(provider_kind="@ai-sdk/openai-compatible"))
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        model_def = cfg["provider"]["volc-gateway"]["models"]["glm-5.2"]
        assert model_def["name"] == "glm-5.2"
        assert "limit" not in model_def

    @staticmethod
    def test_no_kind_skips_injection():
        """未探测/未配置 → 不注入（不猜协议）。"""
        out = _build_env(_make_provider(provider_kind=None))
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_runtime_claude_never_injects():
        out = _build_env(
            _make_provider(
                runtime="claude_sdk",
                provider_kind="@ai-sdk/anthropic",
            )
        )
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_no_base_url_no_injection():
        out = _build_env(
            _make_provider(
                base_url=None,
                provider_kind="@ai-sdk/openai-compatible",
            )
        )
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_stale_kind_url_skips_injection():
        """kind 只对其生效 URL（provider_kind_url）有效：effective URL 已变
        而 sync 未及清理（如运行中手改 default_url）时，不把旧协议注入
        新端点。
        """
        out = _build_env(
            _make_provider(
                base_url=None,
                provider_kind="@ai-sdk/anthropic",
                provider_kind_url="https://old.example/v1",
            ),
            default_url="https://new.example/v1",
        )
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_kind_url_matching_default_url_injects():
        provider = _make_provider(
            base_url=None,
            provider_kind="@ai-sdk/anthropic",
            provider_kind_url="https://gw.example.com/v1",
        )
        out = _build_env(provider, default_url="https://gw.example.com/v1")
        assert json.loads(out["OPENCODE_CONFIG_CONTENT"])["provider"]["volc-gateway"]

    @staticmethod
    def test_bare_model_uses_workflow_controller_pool_name():
        out = _build_env(
            _make_provider(
                model="glm-5.2",
                provider_kind="@ai-sdk/anthropic",
            )
        )
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        assert "workflow-controller-pool" in cfg["provider"]
        assert cfg["provider"]["workflow-controller-pool"]["models"] == {
            "glm-5.2": {"name": "glm-5.2"},
        }

    @staticmethod
    def test_bare_model_rewrites_env_model_to_synthesized_route():
        """裸 model 合成 workflow-controller-pool 后，-m/ANTHROPIC_MODEL 必须切到合成路由，
        否则 opencode 跳过 -m、回落默认 provider，验证的不是池条目。
        """
        out = _build_env(
            _make_provider(
                model="glm-5.2",
                provider_kind="@ai-sdk/anthropic",
            )
        )
        assert out["ANTHROPIC_MODEL"] == "workflow-controller-pool/glm-5.2"

    @staticmethod
    def test_prefixed_model_keeps_env_model():
        out = _build_env(_make_provider(provider_kind="@ai-sdk/openai-compatible"))
        assert out["ANTHROPIC_MODEL"] == "volc-gateway/glm-5.2"

    @staticmethod
    def test_default_model_entry_injects_under_default_prefix():
        """条目依赖 settings.default_model 时，定义用 effective model
        （而非 provider.model=None），否则生成 models: {"": ...} 的废定义。
        """
        provider = _make_provider(
            model=None,
            provider_kind="@ai-sdk/openai-compatible",
        )
        out = _build_env(provider, default_model="zhipuai-coding-plan/glm-5.2")
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        assert cfg["provider"]["zhipuai-coding-plan"]["models"] == {
            "glm-5.2": {"name": "glm-5.2"},
        }
        assert out["ANTHROPIC_MODEL"] == "zhipuai-coding-plan/glm-5.2"

    @staticmethod
    def test_runtime_omitted_falls_back_to_global():
        """settings.conf 的 runtime 是可选字段：省略时按全局 runtime 兜底，
        与 _provider_usable/_probe_worker 的解释一致，否则全局 opencode 下
        条目完全不注入。
        """
        provider = _make_provider(runtime=None, provider_kind="@ai-sdk/anthropic")
        out = _build_env(provider, runtime="opencode")
        assert json.loads(out["OPENCODE_CONFIG_CONTENT"])["provider"]["volc-gateway"]

        out = _build_env(provider, runtime="claude_sdk")
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_workflow_controller_driver_override_injects_under_global_claude(monkeypatch):
        """WORKFLOW_CONTROLLER_DRIVER=opencode 覆盖全局 claude_sdk、条目省略 runtime 时，
        注入必须按 effective runtime（与 create_driver 同规则）生效：否则
        driver 已切 opencode 而 env 仍按 claude_sdk 生成，裸 model 丢
        OPENCODE_CONFIG_CONTENT 且不改写路由，-m 不带、回落默认 provider。
        """
        provider = _make_provider(
            runtime=None,
            model="glm-5.2",
            provider_kind="@ai-sdk/anthropic",
        )
        monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "opencode")
        out = _build_env(provider, runtime="claude_sdk")
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        assert "workflow-controller-pool" in cfg["provider"]
        assert out["ANTHROPIC_MODEL"] == "workflow-controller-pool/glm-5.2"

        monkeypatch.delenv("WORKFLOW_CONTROLLER_DRIVER", raising=False)
        out = _build_env(provider, runtime="claude_sdk")
        assert "OPENCODE_CONFIG_CONTENT" not in out

    @staticmethod
    def test_workflow_controller_driver_override_beats_explicit_provider_runtime(monkeypatch):
        """显式 provider.runtime 同样被 WORKFLOW_CONTROLLER_DRIVER 压过——与
        create_driver 的 env 覆盖一致（覆盖开关就是全局强制的）。
        """
        provider = _make_provider(
            runtime="claude_sdk",
            provider_kind="@ai-sdk/anthropic",
        )
        monkeypatch.setenv("WORKFLOW_CONTROLLER_DRIVER", "opencode")
        out = _build_env(provider, runtime="claude_sdk")
        assert json.loads(out["OPENCODE_CONFIG_CONTENT"])["provider"]["volc-gateway"]

    @staticmethod
    def test_merges_existing_config_content():
        existing = {"provider": {"proj-prov": {"npm": "x"}}, "compaction": {"auto": False}}
        out = _build_env(
            _make_provider(provider_kind="@ai-sdk/openai-compatible"),
            base={"OPENCODE_CONFIG_CONTENT": json.dumps(existing)},
        )
        cfg = json.loads(out["OPENCODE_CONFIG_CONTENT"])
        assert "proj-prov" in cfg["provider"]
        assert "volc-gateway" in cfg["provider"]
        assert cfg["compaction"] == {"auto": False}


class TestProviderKindPersistence:
    @staticmethod
    def test_to_dict_roundtrip():
        p = _make_provider(
            provider_kind="@ai-sdk/openai-compatible", provider_kind_url="https://gw.example.com/v1"
        )
        d = p.to_dict()
        assert d["provider_kind"] == "@ai-sdk/openai-compatible"
        assert d["provider_kind_url"] == "https://gw.example.com/v1"
        p2 = AuthProvider.from_dict(d)
        assert p2.provider_kind == "@ai-sdk/openai-compatible"
        assert p2.provider_kind_url == "https://gw.example.com/v1"

    @staticmethod
    def test_from_dict_missing_kind():
        p = AuthProvider.from_dict(
            {
                "type": "api_key",
                "status": "unused",
                "key": "k",
                "model": "m",
                "base_url": "u",
            }
        )
        assert p.provider_kind is None
        assert p.provider_kind_url is None

    @staticmethod
    def test_parse_key_entry_kind():
        from workflow_controller.core.auth_rotator.key_store import parse_key_entry

        pk = parse_key_entry(
            {"key": "k", "model": "m", "base_url": "u", "provider_kind": "@ai-sdk/anthropic"}
        )
        assert pk.provider_kind == "@ai-sdk/anthropic"
        pk2 = parse_key_entry({"key": "k"})
        assert pk2.provider_kind is None


def _rotator(tmp_path):
    from workflow_controller.core.auth_rotator import AuthRotator

    return AuthRotator(
        state_path=tmp_path / "auth_state.json",
        env_path=tmp_path / ".env",
    )


def _opencode_entry(**overrides) -> dict:
    """标准 opencode 条目字典（key/model 固定，其余字段经 overrides 补充）。"""
    entry = {"key": "sk", "model": "m", "runtime": "opencode"}
    entry.update(overrides)
    return entry


class TestKindConfigRoundTrip:
    @staticmethod
    def test_normalize_keys_preserves_provider_kind():
        from workflow_controller.core.auth_rotator.settings import _normalize_keys

        keys = _normalize_keys(
            [
                {
                    "key": "sk",
                    "model": "m",
                    "base_url": "u",
                    "runtime": "opencode",
                    "provider_kind": "@ai-sdk/anthropic",
                },
                {"key": "sk-2"},
            ]
        )
        assert keys[0]["provider_kind"] == "@ai-sdk/anthropic"
        assert "provider_kind" not in keys[1]

    @staticmethod
    def test_env_for_driver_passes_config_content(tmp_path):
        """session 级 opencode query 经 env_for_driver 拿注入定义，
        白名单漏掉 OPENCODE_CONFIG_CONTENT 即回落 opencode 默认 provider。
        """
        rotator = _rotator(tmp_path)
        rotator.set_keys(
            [
                {
                    "key": "sk-test",
                    "model": "volc-gateway/glm-5.2",
                    "base_url": "https://gw.example.com/v1",
                    "runtime": "opencode",
                    "provider_kind": "@ai-sdk/openai-compatible",
                }
            ]
        )
        env = rotator.env_for_driver({})
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert cfg["provider"]["volc-gateway"]["npm"] == "@ai-sdk/openai-compatible"
        assert env["ANTHROPIC_MODEL"] == "volc-gateway/glm-5.2"

    @staticmethod
    def test_sync_copies_kind_from_settings_conf(tmp_path):
        from workflow_controller.core.auth_rotator.settings import GlobalSettings

        rotator = _rotator(tmp_path)
        rotator.set_keys([{"key": "sk", "model": "m", "runtime": "opencode"}])
        assert rotator.get_provider(0).provider_kind is None

        conf = tmp_path / "settings.conf"
        settings = GlobalSettings.load(conf)
        settings.api_keys[0]["provider_kind"] = "@ai-sdk/openai-compatible"
        settings.save(conf)

        rotator.sync_providers_from_config()
        assert rotator.get_provider(0).provider_kind == "@ai-sdk/openai-compatible"

    @staticmethod
    def test_sync_keeps_probed_kind_when_conf_silent(tmp_path):
        """conf 未写 kind：保留 auth_state.json 里探测写回的 kind（含其生效
        URL），避免一次 sync 把探测结果抹掉。
        """
        rotator = _rotator(tmp_path)
        rotator.set_global_settings(default_url="https://gw.example.com/v1")
        rotator.set_keys([{"key": "sk", "model": "m", "runtime": "opencode"}])
        provider = rotator.get_provider(0)
        provider.provider_kind = "@ai-sdk/anthropic"
        provider.provider_kind_url = "https://gw.example.com/v1"
        getattr(rotator, "_persist")()

        rotator.sync_providers_from_config()
        provider = rotator.get_provider(0)
        assert provider.provider_kind == "@ai-sdk/anthropic"
        assert provider.provider_kind_url == "https://gw.example.com/v1"

    @staticmethod
    def test_sync_clears_probed_kind_when_base_url_changes(tmp_path):
        """kind 是对旧 URL 的实测事实：conf 换了 base_url 而未写 kind 时必须清空，
        否则 validate_provider 把非空旧 kind 当显式值跳过重探测，新 URL 被注入成
        旧 npm 包（Anthropic → OpenAI 可稳定复现）。
        """
        from workflow_controller.core.auth_rotator.settings import GlobalSettings

        rotator = _rotator(tmp_path)
        rotator.set_keys([_opencode_entry(base_url="https://old.example/v1")])
        rotator.get_provider(0).provider_kind = "@ai-sdk/anthropic"
        getattr(rotator, "_persist")()

        conf = tmp_path / "settings.conf"
        settings = GlobalSettings.load(conf)
        settings.api_keys[0]["base_url"] = "https://new.example/v1"
        settings.save(conf)

        rotator.sync_providers_from_config()
        provider = rotator.get_provider(0)
        assert provider.base_url == "https://new.example/v1"
        assert provider.provider_kind is None

    @staticmethod
    def test_sync_clears_probed_kind_when_default_url_changes(tmp_path):
        """条目省略 base_url 依赖全局 default_url 时，改 default_url 等效换
        端点：条目自身 base_url 前后都是 None，比较 raw base_url 发现不了
        变化，必须按 effective URL（provider_kind_url）清空旧 kind。
        """
        rotator = _rotator(tmp_path)
        rotator.set_global_settings(default_url="https://old.example/v1")
        rotator.set_keys([{"key": "sk", "model": "m", "runtime": "opencode"}])
        provider = rotator.get_provider(0)
        provider.provider_kind = "@ai-sdk/anthropic"
        provider.provider_kind_url = "https://old.example/v1"
        getattr(rotator, "_persist")()

        rotator.set_global_settings(default_url="https://new.example/v1")

        provider = rotator.get_provider(0)
        assert provider.provider_kind is None
        assert provider.provider_kind_url is None

    @staticmethod
    def test_sync_keeps_explicit_kind_when_default_url_changes(tmp_path):
        """conf 显式写的 kind 是配置而非探测缓存：改 default_url 也以其为准
        （并随新 effective URL 重新绑定）。
        """
        rotator = _rotator(tmp_path)
        rotator.set_global_settings(default_url="https://old.example/v1")
        rotator.set_keys([_opencode_entry(provider_kind="@ai-sdk/anthropic")])

        rotator.set_global_settings(default_url="https://new.example/v1")

        provider = rotator.get_provider(0)
        assert provider.provider_kind == "@ai-sdk/anthropic"
        assert provider.provider_kind_url == "https://new.example/v1"

    @staticmethod
    def test_sync_keeps_explicit_kind_when_base_url_changes(tmp_path):
        """conf 显式写的 kind 是配置而非探测缓存：换 URL 也以其为准。"""
        from workflow_controller.core.auth_rotator.settings import GlobalSettings

        rotator = _rotator(tmp_path)
        rotator.set_keys([_opencode_entry(base_url="https://old.example/v1")])

        conf = tmp_path / "settings.conf"
        settings = GlobalSettings.load(conf)
        settings.api_keys[0]["base_url"] = "https://new.example/v1"
        settings.api_keys[0]["provider_kind"] = "@ai-sdk/openai-compatible"
        settings.save(conf)

        rotator.sync_providers_from_config()
        provider = rotator.get_provider(0)
        assert provider.base_url == "https://new.example/v1"
        assert provider.provider_kind == "@ai-sdk/openai-compatible"

    @staticmethod
    def test_sync_clears_explicit_kind_when_removed_from_conf(tmp_path):
        """显式 kind 经一次 sync 后 provider_kind_url 已等于当前 URL；同 URL
        从 conf 删除该字段时不能当探测缓存保留（URL 未变的比较发现不了），
        否则错误的人工纠偏无法撤销——验证会把非空旧 kind 当已知协议跳过
        重探测。来源标记为 explicit 即可区分。
        """
        from workflow_controller.core.auth_rotator.settings import GlobalSettings

        rotator = _rotator(tmp_path)
        rotator.set_keys(
            [
                {
                    "key": "sk",
                    "model": "m",
                    "runtime": "opencode",
                    "base_url": "https://gw.example.com/v1",
                    "provider_kind": "@ai-sdk/anthropic",
                }
            ]
        )
        provider = rotator.get_provider(0)
        assert provider.provider_kind == "@ai-sdk/anthropic"
        assert provider.provider_kind_source == "explicit"

        conf = tmp_path / "settings.conf"
        settings = GlobalSettings.load(conf)
        del settings.api_keys[0]["provider_kind"]
        settings.save(conf)

        rotator.sync_providers_from_config()
        provider = rotator.get_provider(0)
        assert provider.provider_kind is None
        assert provider.provider_kind_url is None
        assert provider.provider_kind_source is None

    @staticmethod
    def test_sync_keeps_probe_sourced_kind_when_conf_silent(tmp_path):
        """来源为探测（provider_kind_source == "probe"）的 kind 在 URL 未变
        且 conf 未写 kind 时照常作为缓存保留——显式来源的清理不误伤探测值。
        """
        rotator = _rotator(tmp_path)
        rotator.set_global_settings(default_url="https://gw.example.com/v1")
        rotator.set_keys([{"key": "sk", "model": "m", "runtime": "opencode"}])
        provider = rotator.get_provider(0)
        provider.provider_kind = "@ai-sdk/anthropic"
        provider.provider_kind_url = "https://gw.example.com/v1"
        provider.provider_kind_source = "probe"
        getattr(rotator, "_persist")()

        rotator.sync_providers_from_config()
        provider = rotator.get_provider(0)
        assert provider.provider_kind == "@ai-sdk/anthropic"
        assert provider.provider_kind_url == "https://gw.example.com/v1"
        assert provider.provider_kind_source == "probe"

    @staticmethod
    def test_provider_kind_source_round_trip():
        """auth_state.json 序列化往返保留 kind 来源——重启后 explicit/probe
        的区分不丢失（丢失即显式纠偏删除后又清不掉）。
        """
        provider = _make_provider(
            provider_kind="@ai-sdk/anthropic",
            provider_kind_url="https://gw.example.com/v1",
            provider_kind_source="explicit",
        )
        assert AuthProvider.from_dict(provider.to_dict()).provider_kind_source == "explicit"

    @staticmethod
    def test_null_default_url_yaml_normalizes_to_empty(tmp_path):
        """YAML `default_url:`（显式 null）曾把 None 传入 _provider_fingerprint
        的 "\\0".join 抛 TypeError，带已有 provider 时启动即失败；load 必须
        归一化为空串。
        """
        from workflow_controller.core.auth_rotator.settings import GlobalSettings

        conf = tmp_path / "settings.conf"
        conf.write_text(
            "default_model:\ndefault_url:\napi_keys:\n"
            "  - key: sk-1\n    model: m\n    runtime: opencode\n",
            encoding="utf-8",
        )
        settings = GlobalSettings.load(conf)
        assert settings.default_model == ""
        assert settings.default_url == ""

        # 带持久化 provider 重启：sync → _provider_fingerprint 不再抛 TypeError
        _rotator(tmp_path).set_keys([{"key": "sk-1", "model": "m", "runtime": "opencode"}])
        restarted = _rotator(tmp_path)
        provider = restarted.get_provider(0)
        assert provider.key == "sk-1"
        assert provider.provider_kind is None

    @staticmethod
    def test_get_api_keys_for_ui_returns_kind(tmp_path):
        rotator = _rotator(tmp_path)
        rotator.set_keys([_opencode_entry(provider_kind="@ai-sdk/anthropic")])
        keys = rotator.get_api_keys_for_ui()
        assert keys[0]["provider_kind"] == "@ai-sdk/anthropic"
