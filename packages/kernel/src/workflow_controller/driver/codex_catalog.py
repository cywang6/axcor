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

"""Provider-bound Codex model catalog resolution."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

MODEL_CATALOG_ERROR = (
    "请配置第三方 provider 官方推荐的 model catalog JSON 文件，并将所在绝对路径输入设置中"
)
INVALID_MODEL_CATALOG_ERROR = "请输入合法且包含正确模型名的 model catalog JSON 文件路径"


@dataclass(frozen=True)
class CodexCatalogPreset:
    id: str
    display_name: str
    catalog: dict[str, object]


@dataclass(frozen=True)
class CodexCatalogResolution:
    status: Literal[
        "builtin",
        "preset",
        "custom_required",
        "custom_invalid",
        "custom",
    ]
    model_catalog_path: Path | None = None
    preset_display_name: str | None = None


@lru_cache(maxsize=1)
def _presets() -> tuple[CodexCatalogPreset, ...]:
    resource = Path(__file__).parents[1] / "resources" / "codex_catalog_presets.json"
    data = json.loads(resource.read_text(encoding="utf-8"))
    presets: list[CodexCatalogPreset] = []
    for raw in data.get("presets", []):
        if not isinstance(raw, dict):
            continue
        catalog = raw.get("catalog")
        if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
            continue
        presets.append(
            CodexCatalogPreset(
                id=str(raw["id"]),
                display_name=str(raw["display_name"]),
                catalog=catalog,
            )
        )
    return tuple(presets)


@lru_cache(maxsize=1)
def _bundled_model_slugs() -> frozenset[str]:
    binary = shutil.which("codex") or "codex"
    try:
        result = subprocess.run(
            [binary, "debug", "models", "--bundled"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return frozenset()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return frozenset()
    return frozenset(
        str(model.get("slug")) for model in models if isinstance(model, dict) and model.get("slug")
    )


def resolve_codex_catalog(
    *,
    model: str,
    config_dir: Path,
    provided_path: str = "",
) -> CodexCatalogResolution:
    """Resolve one provider invocation's catalog without host Codex fallback."""
    if provided_path.strip():
        catalog_path = validate_provider_catalog(provided_path, model)
        if catalog_path is None:
            return CodexCatalogResolution(status="custom_invalid")
        return CodexCatalogResolution(
            status="custom",
            model_catalog_path=catalog_path,
        )
    if model.strip() in _bundled_model_slugs():
        return CodexCatalogResolution(status="builtin")

    for preset in _presets():
        if model.strip() in _catalog_model_slugs(preset.catalog):
            return CodexCatalogResolution(
                status="preset",
                model_catalog_path=_install_preset(preset, config_dir),
                preset_display_name=preset.display_name,
            )
    return CodexCatalogResolution(status="custom_required")


def validate_provider_catalog(
    raw_path: str,
    model: str,
) -> Path | None:
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as stream:
            catalog = json.load(stream)
        if not isinstance(catalog, dict) or model not in _catalog_model_slugs(catalog):
            return None
        return path.resolve()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _catalog_model_slugs(catalog: dict[str, object]) -> frozenset[str]:
    models = catalog.get("models")
    if not isinstance(models, list):
        return frozenset()
    return frozenset(
        str(model.get("slug")) for model in models if isinstance(model, dict) and model.get("slug")
    )


def _install_preset(preset: CodexCatalogPreset, config_dir: Path) -> Path:
    catalog_dir = config_dir / "codex-models"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    content = json.dumps(preset.catalog, ensure_ascii=False, indent=2) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    path = catalog_dir / f"{preset.id}-{digest}.json"
    if not path.is_file() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    return path
