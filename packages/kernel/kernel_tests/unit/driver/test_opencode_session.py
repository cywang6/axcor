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

"""opencode_session — best-effort subagent token lookup in opencode's session db.

Hermetic: each test builds a throwaway sqlite db under XDG_DATA_HOME so nothing
touches the host's real ~/.local/share/opencode/opencode.db.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from workflow_controller.driver.opencode_session import lookup_subagent_output_tokens
from workflow_controller.transcript.opencode_store import opencode_db_path

if TYPE_CHECKING:
    from pathlib import Path


def _make_db(tmp_path: Path, rows: list[tuple[str, int]]) -> Path:
    dbdir = tmp_path / "opencode"
    dbdir.mkdir()
    db = dbdir / "opencode.db"
    con = sqlite3.connect(db)
    # Real opencode has many columns; the lookup only reads id + tokens_output,
    # so a minimal table exercises the query without mirroring the full schema.
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, tokens_output INTEGER)")
    con.executemany("INSERT INTO session (id, tokens_output) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return db


def test_lookup_returns_output_tokens(tmp_path: Path):
    _make_db(tmp_path, [("ses_a", 4356), ("ses_b", 99)])
    assert lookup_subagent_output_tokens("ses_a", {"XDG_DATA_HOME": str(tmp_path)}) == 4356


def test_lookup_missing_id_returns_zero(tmp_path: Path):
    _make_db(tmp_path, [("ses_a", 4356)])
    assert lookup_subagent_output_tokens("ses_zzz", {"XDG_DATA_HOME": str(tmp_path)}) == 0


def test_lookup_missing_db_returns_zero(tmp_path: Path):
    # XDG_DATA_HOME points at an empty tmp_path — no opencode/opencode.db.
    assert lookup_subagent_output_tokens("ses_a", {"XDG_DATA_HOME": str(tmp_path)}) == 0


def test_lookup_empty_id_returns_zero():
    assert lookup_subagent_output_tokens("", {"XDG_DATA_HOME": "/tmp"}) == 0


def test_lookup_zero_output_is_zero(tmp_path: Path):
    """A subagent row with 0 output tokens reads as 0 (same as missing — both
    are 'no tokens to show', the honest value).
    """
    _make_db(tmp_path, [("ses_zero", 0)])
    assert lookup_subagent_output_tokens("ses_zero", {"XDG_DATA_HOME": str(tmp_path)}) == 0


def test_lookup_schema_drift_returns_zero(tmp_path: Path):
    """If opencode renames the column, the query fails → graceful 0 (never raises)."""
    dbdir = tmp_path / "opencode"
    dbdir.mkdir()
    con = sqlite3.connect(dbdir / "opencode.db")
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")  # no tokens_output
    con.execute("INSERT INTO session (id) VALUES ('ses_a')")
    con.commit()
    con.close()
    assert lookup_subagent_output_tokens("ses_a", {"XDG_DATA_HOME": str(tmp_path)}) == 0


def test_db_path_resolves_xdg(tmp_path: Path):
    assert opencode_db_path({"XDG_DATA_HOME": str(tmp_path)}) == (
        tmp_path / "opencode" / "opencode.db"
    )


def test_db_path_defaults_to_home_share(tmp_path: Path):
    # No XDG_DATA_HOME → ~/.local/share/opencode/opencode.db
    assert opencode_db_path({"HOME": str(tmp_path)}) == (
        tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    )
