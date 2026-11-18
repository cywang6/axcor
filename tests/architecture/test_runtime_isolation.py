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

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def _install_support(pytester: pytest.Pytester) -> None:
    plugin = Path(__file__).parents[1] / "support" / "pytest_hermetic.py"
    pytester.makepyfile(pytest_hermetic=plugin.read_text(encoding="utf-8"))
    pytester.makeconftest('pytest_plugins = ["pytest_hermetic"]')


def test_collection_time_external_network_is_red(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        test_collection="""
        import socket
        socket.create_connection(("example.com", 443))

        def test_unreachable():
            pass
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*External network and DNS are disabled in tests*"])


def test_runtime_external_network_is_red(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import socket

        def test_external():
            socket.create_connection(("203.0.113.1", 443))
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(failed=1)


def test_all_public_dns_entry_points_are_red(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import socket

        import pytest

        from pytest_hermetic import ExternalNetworkBlockedError

        @pytest.mark.parametrize(
            "query",
            [
                lambda: socket.getaddrinfo("example.com", 443),
                lambda: socket.gethostbyname("example.com"),
                lambda: socket.gethostbyname_ex("example.com"),
                lambda: socket.gethostbyaddr("203.0.113.1"),
                lambda: socket.getfqdn("example.com"),
            ],
        )
        def test_external_dns(query):
            with pytest.raises(ExternalNetworkBlockedError):
                query()
        """
    )
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=5)


def test_live_mode_does_not_install_network_guard(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import socket

        def test_live_network_api_is_original():
            assert socket.create_connection.__module__ == "socket"
        """
    )
    pytester.runpytest_subprocess("--live", "-q").assert_outcomes(passed=1)


def test_loopback_is_green(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import socket

        def test_loopback():
            result = socket.socket().connect_ex(("127.0.0.1", 9))
            assert isinstance(result, int)
            socket.getaddrinfo("localhost", 80)
        """
    )
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_environment_leak_is_red(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import os

        def test_leak():
            os.environ["WORKFLOW_CONTROLLER_LEAK_SENTINEL"] = "yes"
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*Test leaked environment changes*monkeypatch.setenv/delenv*"])


def test_monkeypatch_environment_is_green(pytester: pytest.Pytester) -> None:
    _install_support(pytester)
    pytester.makepyfile(
        """
        import os

        def test_scoped(monkeypatch):
            monkeypatch.setenv("WORKFLOW_CONTROLLER_SCOPED_SENTINEL", "yes")
            assert os.environ["WORKFLOW_CONTROLLER_SCOPED_SENTINEL"] == "yes"
        """
    )
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_app_config_uses_session_temp_directory() -> None:
    import os

    from workflow_controller.core.auth_rotator import default_config_dir

    assert default_config_dir().is_relative_to(Path(os.environ["HOME"]))


def test_app_config_ignores_test_specific_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow_controller.core.auth_rotator import default_config_dir

    monkeypatch.setenv("WORKFLOW_CONTROLLER_TEST_CONFIG_DIR", str(tmp_path))

    assert default_config_dir() != tmp_path
