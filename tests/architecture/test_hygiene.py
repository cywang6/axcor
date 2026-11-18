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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


import pytest

from tests.architecture.hygiene import RULES, scan_hygiene_source, scan_hygiene_tree
from tests.architecture.layout import tree_roots


def test_test_suite_obeys_hygiene_rules() -> None:
    violations = []
    for tests_root in tree_roots():
        violations.extend(item.format() for item in scan_hygiene_tree(tests_root))
    assert not violations, "\n" + "\n".join(violations)


def test_every_hygiene_rule_has_synthetic_red_and_green_cases() -> None:
    cases = {
        "no-module-attribute-assignment": (
            "from workflow_controller.driver import factory as factory_mod\n"
            "factory_mod.create_driver = fake\n",
            "from workflow_controller.driver import factory as factory_mod\n"
            "monkeypatch.setattr(factory_mod, 'create_driver', fake)\n",
        ),
        "no-os-environ-mutation": (
            "import os\nos.environ['TOKEN'] = 'secret'\n",
            "monkeypatch.setenv('TOKEN', 'secret')\n",
        ),
    }
    assert {rule.name for rule in RULES} == set(cases)
    for rule in RULES:
        red, green = cases.get(rule.name, ("", ""))
        assert rule.name in {item.rule for item in scan_hygiene_source(red)}
        assert rule.name not in {item.rule for item in scan_hygiene_source(green)}


@pytest.mark.parametrize(
    "source",
    [
        "import workflow_controller\nworkflow_controller.config.VALUE = 1\n",
        "import workflow_controller.driver.factory\nworkflow_controller.driver.factory.create_driver = fake\n",
        "from workflow_controller.driver import factory\nfactory.create_driver = fake\n",
        "from workflow_controller.driver import factory as driver_factory\n"
        "driver_factory.create_driver = fake\n",
    ],
)
def test_import_forms_cannot_bypass_module_assignment_rule(source: str) -> None:
    violations = scan_hygiene_source(source)
    assert {item.rule for item in violations} == {"no-module-attribute-assignment"}


def test_similar_package_prefix_is_not_treated_as_workflow_controller() -> None:
    source = (
        "from workflow_controller_legacy import helper as helper_mod\nhelper_mod.value = fake\n"
    )
    assert not scan_hygiene_source(source)


@pytest.mark.parametrize(
    "mutation",
    [
        '_os.environ["TOKEN"] = "secret"',
        'del _os.environ["TOKEN"]',
        '_os.environ.update({"TOKEN": "secret"})',
        '_os.environ |= {"TOKEN": "secret"}',
        '_os.putenv("TOKEN", "secret")',
        '_os.unsetenv("TOKEN")',
    ],
)
def test_os_alias_cannot_bypass_environment_rule(mutation: str) -> None:
    violations = scan_hygiene_source(f"import os as _os\n{mutation}\n")
    assert {item.rule for item in violations} == {"no-os-environ-mutation"}


@pytest.mark.parametrize(
    "source",
    [
        'from os import environ\nenviron["TOKEN"] = "secret"\n',
        'from os import environ as env\ndel env["TOKEN"]\n',
        'from os import environ\nenviron.update({"TOKEN": "secret"})\n',
        'from os import environ\nenviron |= {"TOKEN": "secret"}\n',
        'from os import environ as env\nenv |= {"TOKEN": "secret"}\n',
        'from os import putenv\nputenv("TOKEN", "secret")\n',
        'from os import unsetenv as remove_env\nremove_env("TOKEN")\n',
    ],
)
def test_os_member_import_cannot_bypass_environment_rule(source: str) -> None:
    violations = scan_hygiene_source(source)
    assert {item.rule for item in violations} == {"no-os-environ-mutation"}


def test_hygiene_tree_respects_python_encoding_declaration(tmp_path: Path) -> None:
    source = tmp_path / "encoded.py"
    source.write_bytes(
        '# -*- coding: gbk -*-\nlabel = "中文"\n'
        'from os import environ\nenviron["TOKEN"] = label\n'.encode("gbk")
    )

    violations = scan_hygiene_tree(tmp_path)

    assert {item.rule for item in violations} == {"no-os-environ-mutation"}
