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

import logging
from typing import TYPE_CHECKING

import pytest

from workflow_controller.logging_config import configure_logging

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Strip handlers from the workflow_controller logger between tests so each starts clean."""
    yield
    root = logging.getLogger("workflow_controller")
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    root.setLevel(logging.NOTSET)


def test_configure_logging_creates_log_file_in_project_dir(tmp_path: Path) -> None:
    configure_logging(tmp_path, level="DEBUG")
    log_path = tmp_path / ".workflow-controller" / "workflow-controller.log"

    logging.getLogger("workflow_controller.test").info("hello world")
    for h in logging.getLogger("workflow_controller").handlers:
        h.flush()

    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello world" in contents


def test_configure_logging_captures_warnings(tmp_path: Path) -> None:
    configure_logging(tmp_path, level="DEBUG")
    log_path = tmp_path / ".workflow-controller" / "workflow-controller.log"

    logging.getLogger("workflow_controller.session").warning("subprocess died: code 137")
    for h in logging.getLogger("workflow_controller").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "WARNING" in contents
    assert "subprocess died: code 137" in contents
    assert "workflow_controller.session" in contents


def test_configure_logging_preserves_traceback_for_exception(tmp_path: Path) -> None:
    configure_logging(tmp_path, level="DEBUG")
    log_path = tmp_path / ".workflow-controller" / "workflow-controller.log"

    logger = logging.getLogger("workflow_controller.session")
    try:
        raise RuntimeError("boom — stdout limit overrun")
    except RuntimeError:
        logger.exception("Session abc query error")

    for h in logging.getLogger("workflow_controller").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "Session abc query error" in contents
    assert "Traceback" in contents
    assert "RuntimeError: boom — stdout limit overrun" in contents


def test_configure_logging_respects_level(tmp_path: Path) -> None:
    configure_logging(tmp_path, level="WARNING")
    log_path = tmp_path / ".workflow-controller" / "workflow-controller.log"

    logger = logging.getLogger("workflow_controller.test")
    logger.debug("hidden debug")
    logger.info("hidden info")
    logger.warning("visible warning")
    for h in logging.getLogger("workflow_controller").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "hidden debug" not in contents
    assert "hidden info" not in contents
    assert "visible warning" in contents


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling twice should not stack handlers (would duplicate every log line)."""
    configure_logging(tmp_path, level="DEBUG")
    configure_logging(tmp_path, level="DEBUG")
    log_path = tmp_path / ".workflow-controller" / "workflow-controller.log"

    logger = logging.getLogger("workflow_controller.test")
    logger.info("once")
    for h in logging.getLogger("workflow_controller").handlers:
        h.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert contents.count("once") == 1


def test_configure_logging_creates_dot_workflow_controller_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "fresh-project"
    project_dir.mkdir()
    assert not (project_dir / ".workflow-controller").exists()

    configure_logging(project_dir, level="INFO")

    assert (project_dir / ".workflow-controller").is_dir()
