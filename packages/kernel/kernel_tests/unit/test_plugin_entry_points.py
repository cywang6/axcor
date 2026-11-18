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

"""插件发现（ARCHITECTURE.md「插件发现」）：entry_points 通道、包内默认与用户覆盖合并。"""

from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import yaml

from workflow_controller.plugin.base import Plugin, PluginConfig
from workflow_controller.plugin.registry import PluginRegistry

if TYPE_CHECKING:
    from pathlib import Path


class _ExternalPlugin(Plugin):
    def uninstall(self) -> None:
        pass


class _LocalPlugin(Plugin):
    def uninstall(self) -> None:
        pass


class _NotAPlugin:
    pass


class _FakeEP:
    """入口点替身：name、load() 与所属 distribution 版本。"""

    def __init__(self, name: str, target: Any, version: str = "0.0.0") -> None:
        self.name = name
        self._target = target
        self.dist = SimpleNamespace(version=version)

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _only_eps(monkeypatch: Any, *eps: _FakeEP) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: list(eps))


def _registry(tmp_path: Path, monkeypatch: Any, *eps: _FakeEP) -> PluginRegistry:
    """空本地目录 + 隔离用户配置目录的 registry。"""
    _only_eps(monkeypatch, *eps)
    return PluginRegistry(
        plugins_dir=tmp_path / "plugins",
        user_config_dir=tmp_path / "user-config",
    )


def _make_local(tmp_path: Path, name: str) -> None:
    sub = tmp_path / "plugins" / name
    sub.mkdir(parents=True)
    (sub / "plugin.py").write_text("# loader patched in test\n", encoding="utf-8")
    (sub / "config.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\nenabled: true\n", encoding="utf-8"
    )


def _install_plugin_package(tmp_path: Path, monkeypatch: Any, config_yaml: str) -> type[Plugin]:
    """落一个可导入的临时插件包（含 config.yaml），返回其中的插件类。"""
    site = tmp_path / "site"
    pkg = site / "ep_demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (pkg / "plugin.py").write_text(
        "from workflow_controller.plugin.base import Plugin\n\n\n"
        "class PackagedPlugin(Plugin):\n"
        "    def uninstall(self) -> None:\n"
        "        pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(site)
    from ep_demo_pkg.plugin import PackagedPlugin

    return PackagedPlugin


def test_package_defaults_and_dist_version(tmp_path: Path, monkeypatch: Any) -> None:
    plugin_cls = _install_plugin_package(tmp_path, monkeypatch, "enabled: false\nthreshold: 7\n")
    registry = _registry(tmp_path, monkeypatch, _FakeEP("ep-demo", plugin_cls, version="1.2.3"))

    (cfg, cfg_path, cls) = registry.discover()[0]

    assert (cfg.name, cfg.version, cfg.enabled, cls) == (
        "ep-demo",
        "1.2.3",
        False,
        plugin_cls,
    )
    assert cfg.extra == {"threshold": 7}
    assert cfg_path == tmp_path / "user-config" / "ep-demo" / "config.yaml"


def test_missing_package_config_disables_plugin(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(PluginRegistry, "_package_config_defaults", staticmethod(lambda cls: {}))
    registry = _registry(tmp_path, monkeypatch, _FakeEP("ep-demo", _ExternalPlugin))

    (cfg, _, _) = registry.discover()[0]

    assert cfg.enabled is False
    assert cfg.version == "0.0.0"


def test_user_config_overrides_package_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        PluginRegistry,
        "_package_config_defaults",
        staticmethod(lambda cls: {"enabled": True, "threshold": 7}),
    )
    registry = _registry(tmp_path, monkeypatch, _FakeEP("ep-demo", _ExternalPlugin))
    user_cfg = tmp_path / "user-config" / "ep-demo" / "config.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text("enabled: false\nthreshold: 9\n", encoding="utf-8")

    (cfg, cfg_path, _) = registry.discover()[0]

    assert cfg.enabled is False
    assert cfg.extra == {"threshold": 9}
    assert cfg_path == user_cfg


def test_legacy_config_inherited_and_persisted(tmp_path: Path, monkeypatch: Any) -> None:
    """旧布局配置在用户覆盖缺失时一次性迁移落盘，旧文件删除后配置保持。"""
    monkeypatch.setattr(
        PluginRegistry,
        "_package_config_defaults",
        staticmethod(lambda cls: {"enabled": True}),
    )
    registry = _registry(tmp_path, monkeypatch, _FakeEP("ep-demo", _ExternalPlugin))
    legacy = tmp_path / "plugins" / "ep-demo" / "config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("name: ep-demo\nenabled: false\ncustom: keep\n", encoding="utf-8")

    (cfg, cfg_path, _) = registry.discover()[0]
    assert (cfg.enabled, cfg.extra) == (False, {"custom": "keep"})
    persisted = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert persisted == {"enabled": False, "custom": "keep"}

    legacy.unlink()  # 旧文件清理后再发现（模拟重启），配置仍保持
    (cfg_after, _, _) = registry.discover()[0]
    assert (cfg_after.enabled, cfg_after.extra) == (False, {"custom": "keep"})


def test_local_directory_shadows_entry_point(tmp_path: Path, monkeypatch: Any) -> None:
    _make_local(tmp_path, "ep-demo")
    monkeypatch.setattr(
        PluginRegistry,
        "_load_plugin_class",
        staticmethod(lambda plugin_file, pkg_dir: _LocalPlugin),
    )
    _only_eps(monkeypatch, _FakeEP("ep-demo", _ExternalPlugin))
    found = PluginRegistry(
        plugins_dir=tmp_path / "plugins", user_config_dir=tmp_path / "user-config"
    ).discover()
    assert [(cfg.name, cls) for cfg, _, cls in found] == [("ep-demo", _LocalPlugin)]


def test_duplicate_entry_point_names_first_wins(tmp_path: Path, monkeypatch: Any) -> None:
    class _OtherPlugin(Plugin):
        pass

    monkeypatch.setattr(PluginRegistry, "_package_config_defaults", staticmethod(lambda cls: {}))
    registry = _registry(
        tmp_path, monkeypatch, _FakeEP("dup", _ExternalPlugin), _FakeEP("dup", _OtherPlugin)
    )
    found = registry.discover()
    assert [(cfg.name, cls) for cfg, _, cls in found] == [("dup", _ExternalPlugin)]


def test_entry_point_bad_target_skipped(tmp_path: Path, monkeypatch: Any) -> None:
    registry = _registry(
        tmp_path,
        monkeypatch,
        _FakeEP("not-plugin", _NotAPlugin),
        _FakeEP("import-fails", ImportError("boom")),
    )
    assert registry.discover() == []


def test_plugin_base_accepts_none_config_path() -> None:
    p = _ExternalPlugin(PluginConfig(name="x", version="0.0.0"), None)
    assert p.config_path is None
