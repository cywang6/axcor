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

"""Worktree-local persistence for file provenance evidence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from workflow_controller.plugin.file_changes import FileChangeContext, FileChangeOutcome

SCHEMA_VERSION = 3
SnapshotStage = Literal["before", "after"]
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_FILE_STATE_QUERY_BATCH_SIZE = 900
_HUMAN_AUTHOR_RE = re.compile(r".+ <[^<>\s]+@[^<>\s]+>")
# The DDL constants keep the exact statement text (including indentation) that
# _init_schema executes, so a freshly created database is byte-identical to one
# created before the statements moved out of the method.
_SCHEMA_V1_DDL = """
                    CREATE TABLE IF NOT EXISTS captures (
                        capture_id TEXT PRIMARY KEY, session_id TEXT,
                        actor_kind TEXT NOT NULL, native_session_id TEXT,
                        parent_native_session_id TEXT, runtime TEXT, model TEXT,
                        agent_name TEXT, native_agent_id TEXT,
                        tool_name TEXT NOT NULL, tool_use_id TEXT NOT NULL,
                        cwd TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                        succeeded INTEGER, is_error INTEGER, incomplete INTEGER,
                        conflicted INTEGER NOT NULL DEFAULT 0,
                        tool_input TEXT NOT NULL, tool_output TEXT,
                        attributes TEXT NOT NULL, head_before TEXT NOT NULL DEFAULT '',
                        head_after TEXT
                    );
                    CREATE TABLE IF NOT EXISTS active_captures (
                        capture_id TEXT PRIMARY KEY REFERENCES captures(capture_id)
                            ON DELETE CASCADE,
                        owner_pid INTEGER NOT NULL, started_at TEXT NOT NULL,
                        conflicted INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS capture_files (
                        capture_id TEXT NOT NULL REFERENCES captures(capture_id)
                            ON DELETE CASCADE,
                        stage TEXT NOT NULL CHECK(stage IN ('before', 'after')),
                        path TEXT NOT NULL, content_blob TEXT NOT NULL,
                        PRIMARY KEY (capture_id, stage, path)
                    );
                    CREATE TABLE IF NOT EXISTS mutations (
                        capture_id TEXT NOT NULL REFERENCES captures(capture_id)
                            ON DELETE CASCADE,
                        path TEXT NOT NULL, before_blob TEXT, after_blob TEXT,
                        additions INTEGER NOT NULL, deletions INTEGER NOT NULL,
                        conflict INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (capture_id, path),
                        CHECK(before_blob IS NOT NULL OR after_blob IS NOT NULL)
                    );
                    CREATE TABLE IF NOT EXISTS conflicts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        capture_id TEXT REFERENCES captures(capture_id)
                            ON DELETE SET NULL,
                        kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_captures_session
                        ON captures(session_id, started_at);
                    CREATE INDEX IF NOT EXISTS idx_conflicts_capture
                        ON conflicts(capture_id, id);
                """
_FILE_STATE_DDL = """
                    CREATE TABLE IF NOT EXISTS file_state (
                        path TEXT PRIMARY KEY,
                        content_blob TEXT NOT NULL,
                        attributions TEXT NOT NULL,
                        updated_capture_id TEXT REFERENCES captures(capture_id)
                            ON DELETE SET NULL
                    )
                """
_SCHEMA_V3_DDL = """
                    CREATE TABLE IF NOT EXISTS commit_lines (
                        commit_sha TEXT NOT NULL,
                        path TEXT NOT NULL,
                        line_no INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        attribution TEXT NOT NULL,
                        PRIMARY KEY (commit_sha, path, line_no)
                    );
                    CREATE TABLE IF NOT EXISTS commits (
                        commit_sha TEXT PRIMARY KEY,
                        parent_sha TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        ai_lines INTEGER NOT NULL,
                        total_added_lines INTEGER NOT NULL,
                        generated_lines INTEGER NOT NULL,
                        accepted_lines INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_captures_commit
                        ON captures(committed_sha, head_before, head_after);
                """


@dataclass(frozen=True, slots=True)
class LineAttribution:
    actor_kind: str

    actor_id: str | None = None

    capture_id: str | None = None

    session_id: str | None = None

    runtime: str | None = None

    model: str | None = None

    agent_name: str | None = None

    native_agent_id: str | None = None

    conflict: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LineAttribution:
        return cls(
            actor_kind=str(value.get("actor_kind") or "unknown"),
            actor_id=_optional_string(value.get("actor_id")),
            capture_id=_optional_string(value.get("capture_id")),
            session_id=_optional_string(value.get("session_id")),
            runtime=_optional_string(value.get("runtime")),
            model=_optional_string(value.get("model")),
            agent_name=_optional_string(value.get("agent_name")),
            native_agent_id=_optional_string(value.get("native_agent_id")),
            conflict=bool(value.get("conflict", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProvenanceStore:
    """Persist immutable capture evidence inside one worktree."""

    def __init__(self, worktree: Path, *, readonly: bool = False) -> None:
        self.worktree = worktree.resolve()
        if not self.worktree.is_dir():
            raise ValueError(f"worktree is not a directory: {self.worktree}")
        self.root = self.worktree / ".workflow-controller" / "file-provenance"
        self.blobs = self.root / "blobs"
        self.db_path = self.root / "provenance.db"
        self.readonly = readonly
        self._verified_blobs: dict[str, tuple[int, int, int, int, int]] = {}
        self._verified_blobs_lock = Lock()
        if readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"provenance database does not exist: {self.db_path}")
            self._validate_schema()
        else:
            self.blobs.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @staticmethod
    def _hydrate_capture_value(conn: sqlite3.Connection, value: dict[str, Any]) -> None:
        """单条 capture 行的布尔/JSON 字段水化与 mutations 装配（列表/单取共用）。"""
        for key in ("succeeded", "is_error", "incomplete"):
            if value[key] is not None:
                value[key] = bool(value[key])
        value["conflicted"] = bool(value["conflicted"])
        value["tool_input"] = json.loads(value["tool_input"])
        value["tool_output"] = (
            json.loads(value["tool_output"]) if value["tool_output"] is not None else None
        )
        value["attributes"] = json.loads(value["attributes"])
        value["mutations"] = []
        for item in conn.execute(
            "SELECT * FROM mutations WHERE capture_id=? ORDER BY path",
            (value["capture_id"],),
        ):
            mutation = dict(item)
            mutation["conflict"] = bool(mutation["conflict"])
            value["mutations"].append(mutation)

    @staticmethod
    def _hydrate_captures(
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            ProvenanceStore._hydrate_capture_value(conn, value)
            conflicts = []
            for item in conn.execute(
                """SELECT kind,detail,created_at FROM conflicts
                   WHERE capture_id=? ORDER BY id""",
                (value["capture_id"],),
            ):
                conflicts.append(dict(item))
            value["conflicts"] = conflicts
            values.append(value)
        return values

    @staticmethod
    def _redact_attributions(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        session_id: str,
    ) -> None:
        for row in conn.execute(f"SELECT rowid,{column} FROM {table}"):
            try:
                value = json.loads(str(row[column]))
            except (TypeError, json.JSONDecodeError):
                continue
            items = value if isinstance(value, list) else [value]
            changed = False
            for item in items:
                if isinstance(item, dict) and item.get("session_id") == session_id:
                    item["session_id"] = None
                    item["native_agent_id"] = None
                    changed = True
            if changed:
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (_json_dump(value), row["rowid"]),
                )

    @staticmethod
    def _insert_conflict(
        conn: sqlite3.Connection,
        capture_id: str | None,
        kind: str,
        detail: str,
    ) -> None:
        conn.execute(
            """INSERT INTO conflicts(capture_id,kind,detail,created_at)
               VALUES (?,?,?,?)""",
            (capture_id, kind, detail, datetime.now(tz=UTC).isoformat()),
        )

    @staticmethod
    def _insert_capture_row(
        conn: sqlite3.Connection,
        context: FileChangeContext,
        head_before: str,
        overlap: bool,
    ) -> None:
        try:
            conn.execute(
                """INSERT INTO captures (
                        capture_id,session_id,actor_kind,native_session_id,
                        parent_native_session_id,runtime,model,agent_name,native_agent_id,
                        tool_name,tool_use_id,cwd,started_at,conflicted,tool_input,
                        attributes,head_before
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    context.capture_id,
                    context.workflow_controller_session_id,
                    context.actor_kind,
                    context.native_session_id,
                    context.parent_native_session_id,
                    context.runtime,
                    context.model,
                    context.agent_name,
                    context.native_agent_id,
                    context.tool_name,
                    context.tool_use_id,
                    str(context.cwd),
                    context.started_at.isoformat(),
                    int(overlap),
                    _json_dump(context.tool_input),
                    _json_dump(context.attributes),
                    head_before,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"capture already exists: {context.capture_id}") from exc

    @staticmethod
    def _upgrade_schema_v2(conn: sqlite3.Connection) -> None:
        capture_file_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(capture_files)")
        }
        if "attributions" not in capture_file_columns:
            conn.execute(
                """ALTER TABLE capture_files ADD COLUMN
                           attributions TEXT NOT NULL DEFAULT '[]'"""
            )
        conn.execute(_FILE_STATE_DDL)
        conn.execute("PRAGMA user_version=2")

    @staticmethod
    def _upgrade_schema_v3(conn: sqlite3.Connection) -> None:
        capture_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(captures)")}
        if "committed_sha" not in capture_columns:
            conn.execute("ALTER TABLE captures ADD COLUMN committed_sha TEXT")
        conn.executescript(_SCHEMA_V3_DDL)
        conn.execute("PRAGMA user_version=3")

    def begin_capture(
        self,
        context: FileChangeContext,
        *,
        head_before: str = "",
    ) -> bool:
        """Open an activity window and return whether it overlaps another one."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._remove_stale_windows(conn)
            active_ids = [
                str(row[0]) for row in conn.execute("SELECT capture_id FROM active_captures")
            ]
            overlap = bool(active_ids)
            if overlap:
                self._mark_overlapping(conn, active_ids, context.capture_id)
            self._insert_capture_row(conn, context, head_before, overlap)
            conn.execute(
                "INSERT INTO active_captures VALUES (?,?,?,?)",
                (
                    context.capture_id,
                    os.getpid(),
                    context.started_at.isoformat(),
                    int(overlap),
                ),
            )
            if overlap:
                detail = (
                    f"capture {context.capture_id} overlapped {len(active_ids)} active capture(s)"
                )
                self._insert_conflict(
                    conn,
                    context.capture_id,
                    "overlapping_capture",
                    detail,
                )
        return overlap

    def end_capture_window(self, capture_id: str) -> bool:
        """Close the attribution window before deriving mutations."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            capture = conn.execute(
                "SELECT conflicted FROM captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            if capture is None:
                raise KeyError(f"unknown capture: {capture_id}")
            active = conn.execute(
                "SELECT conflicted FROM active_captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            conflicted = bool(capture["conflicted"])
            if active is None:
                conflicted = True
                self._insert_conflict(
                    conn,
                    capture_id,
                    "missing_active_capture",
                    f"capture {capture_id} completed without an active window",
                )
            else:
                conflicted = conflicted or bool(active["conflicted"])
            conn.execute(
                "UPDATE captures SET conflicted=? WHERE capture_id=?",
                (int(conflicted), capture_id),
            )
            conn.execute("DELETE FROM active_captures WHERE capture_id=?", (capture_id,))
        return conflicted

    def finish_capture(
        self,
        context: FileChangeContext,
        outcome: FileChangeOutcome,
        *,
        head_after: str = "",
    ) -> bool:
        """Persist the final outcome after attribution has finished."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            capture = conn.execute(
                "SELECT finished_at,conflicted,head_before FROM captures WHERE capture_id=?",
                (context.capture_id,),
            ).fetchone()
            if capture is None:
                raise KeyError(f"unknown capture: {context.capture_id}")
            if capture["finished_at"] is not None:
                return bool(capture["conflicted"])
            conflicted = bool(capture["conflicted"]) or outcome.incomplete
            if str(capture["head_before"] or "") != head_after:
                conflicted = True
                self._insert_conflict(
                    conn,
                    context.capture_id,
                    "capture_crossed_commit",
                    f"capture {context.capture_id} crossed a commit boundary",
                )

            conn.execute(
                """UPDATE captures SET finished_at=?,succeeded=?,is_error=?,
                   incomplete=?,conflicted=?,tool_output=?,head_after=?
                   WHERE capture_id=?""",
                (
                    outcome.finished_at.isoformat(),
                    int(outcome.succeeded),
                    int(outcome.is_error),
                    int(outcome.incomplete),
                    int(conflicted),
                    _json_dump(outcome.tool_output),
                    head_after,
                    context.capture_id,
                ),
            )
        return conflicted

    def record_conflict(self, capture_id: str | None, kind: str, detail: str) -> None:
        with self._connect() as conn:
            self._insert_conflict(conn, capture_id, kind, detail)
            if capture_id is not None:
                conn.execute(
                    "UPDATE captures SET conflicted=1 WHERE capture_id=?",
                    (capture_id,),
                )

    def abandon_capture(self, capture_id: str) -> None:
        """Remove a capture that failed before the observed tool started."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM captures WHERE capture_id=?", (capture_id,))

    def record_snapshot(
        self,
        capture_id: str,
        stage: SnapshotStage,
        files: Mapping[str, bytes],
    ) -> None:
        """Persist a set of before or after file snapshots."""
        _validate_stage(stage)
        normalized_files = [(_normalize_path(path), content) for path, content in files.items()]
        if not normalized_files:
            return
        with self._connect() as conn:
            state_by_path: dict[str, sqlite3.Row] = {}
            paths = [path for path, _content in normalized_files]
            for offset in range(0, len(paths), _FILE_STATE_QUERY_BATCH_SIZE):
                batch = paths[slice(offset, offset + _FILE_STATE_QUERY_BATCH_SIZE)]
                placeholders = ",".join("?" for _ in batch)
                for row in conn.execute(
                    f"""SELECT path,content_blob,attributions FROM file_state
                        WHERE path IN ({placeholders})""",
                    batch,
                ):
                    state_by_path[str(row["path"])] = row
            rows = []
            for path, content in normalized_files:
                state = state_by_path.get(path)
                digest = hashlib.sha256(content).hexdigest()
                attributions = (
                    str(state["attributions"])
                    if state is not None and str(state["content_blob"]) == digest
                    else "[]"
                )
                rows.append(
                    (
                        capture_id,
                        stage,
                        path,
                        self._put_blob(content),
                        attributions,
                    )
                )
            conn.executemany(
                """INSERT INTO capture_files(
                       capture_id,stage,path,content_blob,attributions
                   ) VALUES (?,?,?,?,?)
                   ON CONFLICT(capture_id,stage,path) DO UPDATE SET
                   content_blob=excluded.content_blob,
                   attributions=excluded.attributions""",
                rows,
            )

    def record_mutation(
        self,
        capture_id: str,
        path: str,
        *,
        before: bytes | None,
        after: bytes | None,
        additions: int,
        deletions: int,
        conflict: bool = False,
    ) -> None:
        """Record one changed path and its before/after content references."""
        if before is None and after is None:
            raise ValueError("a mutation must have before or after content")
        if additions < 0 or deletions < 0:
            raise ValueError("mutation line counts cannot be negative")
        normalized = _normalize_path(path)
        before_blob = self._put_blob(before) if before is not None else None
        after_blob = self._put_blob(after) if after is not None else None
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO mutations VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(capture_id,path) DO UPDATE SET
                   before_blob=excluded.before_blob,
                   after_blob=excluded.after_blob,
                   additions=excluded.additions,
                   deletions=excluded.deletions,
                   conflict=excluded.conflict""",
                (
                    capture_id,
                    normalized,
                    before_blob,
                    after_blob,
                    additions,
                    deletions,
                    int(conflict),
                ),
            )

    def load_file_state(self, path: str, content: bytes) -> list[LineAttribution]:
        normalized = _normalize_path(path)
        digest = hashlib.sha256(content).hexdigest()
        lines = _text_lines(content)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_blob,attributions FROM file_state WHERE path=?",
                (normalized,),
            ).fetchone()
        if row is None or str(row["content_blob"]) != digest:
            return [LineAttribution("unknown") for _ in lines]
        return _load_attributions(str(row["attributions"]), len(lines))

    def save_file_state(
        self,
        path: str,
        content: bytes,
        attributions: list[LineAttribution],
        capture_id: str | None,
    ) -> None:
        normalized = _normalize_path(path)
        if len(attributions) != len(_text_lines(content)):
            raise ValueError("file attribution count must match its text line count")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO file_state(
                       path,content_blob,attributions,updated_capture_id
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                   content_blob=excluded.content_blob,
                   attributions=excluded.attributions,
                   updated_capture_id=excluded.updated_capture_id""",
                (
                    normalized,
                    self._put_blob(content),
                    _json_dump([item.to_dict() for item in attributions]),
                    capture_id,
                ),
            )

    def delete_file_state(self, path: str) -> None:
        normalized = _normalize_path(path)
        with self._connect() as conn:
            conn.execute("DELETE FROM file_state WHERE path=?", (normalized,))

    def read_snapshot(
        self,
        capture_id: str,
        stage: SnapshotStage,
        path: str,
    ) -> bytes | None:
        """Return snapshot content, or ``None`` when no evidence exists."""
        _validate_stage(stage)
        normalized = _normalize_path(path)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT content_blob FROM capture_files
                   WHERE capture_id=? AND stage=? AND path=?""",
                (capture_id, stage, normalized),
            ).fetchone()
        return self._get_blob(str(row[0])) if row is not None else None

    def load_snapshot(
        self,
        capture_id: str,
        stage: SnapshotStage,
    ) -> dict[str, bytes]:
        """Load a complete persisted snapshot using one metadata query."""
        _validate_stage(stage)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT path,content_blob FROM capture_files
                   WHERE capture_id=? AND stage=? ORDER BY path""",
                (capture_id, stage),
            ).fetchall()
        return {str(row["path"]): self._get_blob(str(row["content_blob"])) for row in rows}

    def get_capture(self, capture_id: str) -> dict[str, Any] | None:
        """Return capture metadata with its mutation and snapshot references."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            self._hydrate_capture_value(conn, value)
            value["snapshots"] = []
            for item in conn.execute(
                """SELECT stage,path,content_blob,attributions FROM capture_files
                   WHERE capture_id=? ORDER BY stage,path""",
                (capture_id,),
            ):
                snapshot = dict(item)
                content = self._get_blob(str(snapshot["content_blob"]))
                attributions = []
                for attribution in _load_attributions(
                    str(snapshot["attributions"]), len(_text_lines(content))
                ):
                    attributions.append(attribution.to_dict())
                snapshot["attributions"] = attributions
                value["snapshots"].append(snapshot)
            conflicts = []
            for item in conn.execute(
                """SELECT kind,detail,created_at FROM conflicts
                   WHERE capture_id=? ORDER BY id""",
                (capture_id,),
            ):
                conflicts.append(dict(item))
            value["conflicts"] = conflicts
        return value

    def list_captures_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return capture metadata, mutations and conflicts without whole-worktree snapshots."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM captures WHERE session_id=? ORDER BY started_at,capture_id",
                (session_id,),
            ).fetchall()
            return self._hydrate_captures(conn, rows)

    def list_captures_for_native_sessions(
        self,
        native_session_ids: list[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Return captures belonging to any native runtime session."""
        unique_ids = tuple(dict.fromkeys(value for value in native_session_ids if value))
        if not unique_ids:
            return []
        values: list[dict[str, Any]] = []
        with self._connect() as conn:
            for offset in range(0, len(unique_ids), _FILE_STATE_QUERY_BATCH_SIZE):
                batch = unique_ids[slice(offset, offset + _FILE_STATE_QUERY_BATCH_SIZE)]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM captures WHERE native_session_id IN ({placeholders})",
                    batch,
                ).fetchall()
                values.extend(self._hydrate_captures(conn, rows))
        values.sort(key=lambda value: (str(value["started_at"]), str(value["capture_id"])))
        return values

    def get_capture_file(
        self,
        capture_id: str,
        stage: str,
        path: str,
    ) -> dict[str, Any] | None:
        _validate_stage(stage)
        normalized = _normalize_path(path)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT content_blob,attributions FROM capture_files
                   WHERE capture_id=? AND stage=? AND path=?""",
                (capture_id, stage, normalized),
            ).fetchone()
        if row is None:
            return None
        content = self._get_blob(str(row["content_blob"]))
        try:
            text = content.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            text = ""
            binary = True
        attribution_rows = []
        for item in _load_attributions(
            str(row["attributions"]),
            len(_text_lines(content)),
        ):
            attribution_rows.append(item.to_dict())
        return {
            "capture_id": capture_id,
            "stage": stage,
            "path": normalized,
            "size": len(content),
            "binary": binary,
            "content": text,
            "attributions": attribution_rows,
        }

    def list_files(self, commit: str = "") -> list[str]:
        with self._connect() as conn:
            if commit:
                rows = conn.execute(
                    "SELECT DISTINCT path FROM commit_lines WHERE commit_sha=? ORDER BY path",
                    (commit,),
                )
            else:
                rows = conn.execute("SELECT path FROM file_state ORDER BY path")
            return [str(row[0]) for row in rows]

    def blame(self, path: str, commit: str = "") -> list[dict[str, Any]]:
        normalized = _normalize_path(path)
        with self._connect() as conn:
            if commit:
                rows = conn.execute(
                    """SELECT line_no,content,attribution FROM commit_lines
                       WHERE commit_sha=? AND path=? ORDER BY line_no""",
                    (commit, normalized),
                ).fetchall()
                return [
                    {
                        "line_no": int(row["line_no"]),
                        "content": str(row["content"]),
                        **_load_attribution(str(row["attribution"])).to_dict(),
                    }
                    for row in rows
                ]
            row = conn.execute(
                "SELECT content_blob,attributions FROM file_state WHERE path=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return []
        lines = _text_lines(self._get_blob(str(row["content_blob"])))
        attributions = _load_attributions(str(row["attributions"]), len(lines))
        return [
            {"line_no": index + 1, "content": line, **attribution.to_dict()}
            for index, (line, attribution) in enumerate(zip(lines, attributions, strict=True))
        ]

    def list_commits(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("commit limit must be between 1 and 500")
        with self._connect() as conn:
            commits = []
            for row in conn.execute(
                "SELECT * FROM commits ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ):
                commits.append(dict(row))
            return commits

    def get_commit_metrics(self, commit: str = "") -> dict[str, Any] | None:
        with self._connect() as conn:
            if commit:
                row = conn.execute(
                    "SELECT * FROM commits WHERE commit_sha=?",
                    (commit,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM commits ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row is not None else None

    def current_content_hash(self, path: str) -> str | None:
        normalized = _normalize_path(path)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_blob FROM file_state WHERE path=?",
                (normalized,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def claim_human(
        self,
        path: str,
        start_line: int,
        end_line: int,
        author: str,
        expected_content_hash: str,
    ) -> int:
        normalized = _normalize_path(path)
        identity = author.strip()
        if _HUMAN_AUTHOR_RE.fullmatch(identity) is None:
            raise ValueError("author must use the form Name <email>")
        if _DIGEST_RE.fullmatch(expected_content_hash) is None:
            raise ValueError("expected content hash must be a SHA-256 digest")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content_blob,attributions FROM file_state WHERE path=?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise ValueError("file has no provenance state")
            lines = self._human_claim_lines(row, normalized, expected_content_hash)
            if start_line < 1 or end_line < start_line or end_line > len(lines):
                raise ValueError("claim line range is outside the file")
            attributions = _load_attributions(str(row["attributions"]), len(lines))
            claimed = _apply_human_claim(attributions, start_line, end_line, identity)
            serialized = _json_dump([item.to_dict() for item in attributions])
            updated = conn.execute(
                """UPDATE file_state SET attributions=?
                   WHERE path=? AND content_blob=? AND attributions=?""",
                (serialized, normalized, expected_content_hash, row["attributions"]),
            )
            if updated.rowcount != 1:
                raise ValueError("file provenance changed; refresh before claiming")
            conn.execute(
                """UPDATE capture_files SET attributions=?
                   WHERE stage='after' AND path=? AND content_blob=?""",
                (serialized, normalized, expected_content_hash),
            )
        return claimed

    def redact_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE captures SET session_id=NULL,native_session_id=NULL,
                   parent_native_session_id=NULL,native_agent_id=NULL,
                   tool_input='{}',tool_output=NULL,attributes='{}'
                   WHERE session_id=?""",
                (session_id,),
            )
            for table, column in (
                ("file_state", "attributions"),
                ("capture_files", "attributions"),
                ("commit_lines", "attribution"),
            ):
                self._redact_attributions(conn, table, column, session_id)

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO state(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def capture_ids_for_commit(self, commit: str, parent: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT capture_id FROM captures
                   WHERE finished_at IS NOT NULL
                     AND conflicted=0 AND COALESCE(incomplete,0)=0
                     AND (committed_sha=? OR (
                       committed_sha IS NULL AND head_before=? AND head_after=?
                     ))""",
                (commit, parent, parent),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def mark_pending_committed(self, commit: str, parent: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE captures SET committed_sha=?
                   WHERE committed_sha IS NULL AND finished_at IS NOT NULL
                     AND conflicted=0 AND COALESCE(incomplete,0)=0
                     AND head_before=? AND head_after=?""",
                (commit, parent, parent),
            )

    def finalize_commit(
        self,
        commit: str,
        parent: str,
        ai_lines: int,
        total_added_lines: int,
        accepted_lines: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                """SELECT COALESCE(SUM(m.additions),0) FROM mutations m
                   JOIN captures c ON c.capture_id=m.capture_id
                   WHERE c.actor_kind='agent' AND c.committed_sha IS NULL
                     AND c.finished_at IS NOT NULL
                     AND c.conflicted=0 AND COALESCE(c.incomplete,0)=0
                     AND m.conflict=0 AND c.head_before=? AND c.head_after=?""",
                (parent, parent),
            ).fetchone()
            previous = conn.execute(
                "SELECT generated_lines FROM commits WHERE commit_sha=?",
                (commit,),
            ).fetchone()
            generated_lines = int(pending[0]) + (int(previous[0]) if previous is not None else 0)
            conn.execute(
                """UPDATE captures SET committed_sha=?
                   WHERE committed_sha IS NULL AND finished_at IS NOT NULL
                     AND conflicted=0 AND COALESCE(incomplete,0)=0
                     AND head_before=? AND head_after=?""",
                (commit, parent, parent),
            )
            conn.execute(
                """INSERT INTO commits VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(commit_sha) DO UPDATE SET
                   parent_sha=excluded.parent_sha,
                   created_at=excluded.created_at,
                   ai_lines=excluded.ai_lines,
                   total_added_lines=excluded.total_added_lines,
                   generated_lines=excluded.generated_lines,
                   accepted_lines=excluded.accepted_lines""",
                (
                    commit,
                    parent,
                    datetime.now(tz=UTC).isoformat(),
                    ai_lines,
                    total_added_lines,
                    generated_lines,
                    accepted_lines,
                ),
            )

    def commit_source(
        self,
        path: str,
        commit: str,
        parent: str,
    ) -> tuple[list[str], list[LineAttribution]]:
        normalized = _normalize_path(path)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT cf.content_blob,cf.attributions FROM capture_files cf
                   JOIN captures c ON c.capture_id=cf.capture_id
                   WHERE cf.stage='after' AND cf.path=?
                     AND c.finished_at IS NOT NULL
                     AND c.conflicted=0 AND COALESCE(c.incomplete,0)=0
                     AND (c.committed_sha=? OR (
                       c.committed_sha IS NULL AND c.head_before=? AND c.head_after=?
                     ))
                   ORDER BY c.finished_at DESC LIMIT 1""",
                (normalized, commit, parent, parent),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT content_blob,attributions FROM file_state WHERE path=?",
                    (normalized,),
                ).fetchone()
        if row is None:
            return [], []
        lines = _text_lines(self._get_blob(str(row["content_blob"])))
        return lines, _load_attributions(str(row["attributions"]), len(lines))

    def record_commit_lines(
        self,
        commit: str,
        path: str,
        lines: list[str],
        attributions: list[LineAttribution],
    ) -> None:
        normalized = _normalize_path(path)
        if len(lines) != len(attributions):
            raise ValueError("commit attribution count must match its line count")
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM commit_lines WHERE commit_sha=? AND path=?",
                (commit, normalized),
            )
            commit_line_rows = []
            for index, (line, attribution) in enumerate(zip(lines, attributions, strict=True)):
                commit_line_rows.append(
                    (
                        commit,
                        normalized,
                        index + 1,
                        line,
                        _json_dump(attribution.to_dict()),
                    )
                )
            conn.executemany(
                """INSERT INTO commit_lines(
                       commit_sha,path,line_no,content,attribution
                   ) VALUES (?,?,?,?,?)""",
                commit_line_rows,
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _validate_schema(self) -> None:
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version <= 0 or version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported provenance schema {version}; expected 1..{SCHEMA_VERSION}"
                )
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {"captures", "capture_files", "mutations", "conflicts"}
            missing = required - tables
            if missing:
                raise RuntimeError(
                    f"provenance database is missing tables: {', '.join(sorted(missing))}"
                )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported provenance schema {version}; "
                    f"maximum supported is {SCHEMA_VERSION}"
                )
            if version == 0:
                conn.executescript(_SCHEMA_V1_DDL)
                conn.execute("PRAGMA user_version=1")
                version = 1
            if version < 2:
                self._upgrade_schema_v2(conn)
                version = 2
            if version < 3:
                self._upgrade_schema_v3(conn)

    def _put_blob(self, content: bytes) -> str:
        """Store content atomically and return its SHA-256 digest."""
        digest = hashlib.sha256(content).hexdigest()
        target = self._blob_path(digest)
        signature = _file_signature(target)
        if signature is not None:
            with self._verified_blobs_lock:
                already_verified = self._verified_blobs.get(digest) == signature
            if already_verified or _file_digest(target) == digest:
                with self._verified_blobs_lock:
                    self._verified_blobs[digest] = signature
                return digest

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".blob-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            signature = _file_signature(target)
            if signature is not None:
                with self._verified_blobs_lock:
                    self._verified_blobs[digest] = signature
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest

    def _get_blob(self, digest: str) -> bytes:
        """Read a blob and reject invalid names or corrupted content."""
        content = self._blob_path(digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"provenance blob failed integrity check: {digest}")
        return content

    def _blob_path(self, digest: str) -> Path:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("blob digest must be 64 lowercase hexadecimal characters")
        return self.blobs / digest[:2] / digest[2:]

    def _remove_stale_windows(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT capture_id,owner_pid FROM active_captures",
        ).fetchall()
        for row in rows:
            owner_pid = int(row["owner_pid"])
            if _pid_alive(owner_pid):
                continue
            capture_id = str(row["capture_id"])
            conn.execute(
                "DELETE FROM active_captures WHERE capture_id=?",
                (capture_id,),
            )
            conn.execute(
                """UPDATE captures SET
                   finished_at=COALESCE(finished_at,?),
                   succeeded=COALESCE(succeeded,0),
                   is_error=COALESCE(is_error,1),
                   incomplete=1,
                   conflicted=1,
                   head_after=COALESCE(head_after,head_before)
                   WHERE capture_id=?""",
                (datetime.now(tz=UTC).isoformat(), capture_id),
            )
            self._insert_conflict(
                conn,
                capture_id,
                "stale_active_capture",
                f"removed capture {capture_id} owned by exited process {owner_pid}",
            )

    def _mark_overlapping(
        self,
        conn: sqlite3.Connection,
        active_ids: list[str],
        capture_id: str,
    ) -> None:
        conn.execute("UPDATE active_captures SET conflicted=1")
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"UPDATE captures SET conflicted=1 WHERE capture_id IN ({placeholders})",
            active_ids,
        )
        for active_id in active_ids:
            self._insert_conflict(
                conn,
                active_id,
                "overlapping_capture",
                f"capture {active_id} overlapped capture {capture_id}",
            )

    def _human_claim_lines(
        self,
        row: sqlite3.Row,
        normalized: str,
        expected_content_hash: str,
    ) -> list[str]:
        if str(row["content_blob"]) != expected_content_hash:
            raise ValueError("file provenance changed; refresh before claiming")
        target = self.worktree / normalized
        if target.is_symlink() or not target.is_file():
            raise ValueError("working tree file is unavailable")
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_content_hash:
            raise ValueError("working tree file changed; refresh before claiming")
        lines = _text_lines(content)
        if not lines:
            raise ValueError("binary or empty files cannot be claimed")
        return lines


def _load_attributions(value: str, line_count: int) -> list[LineAttribution]:
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise TypeError
        attributions = [
            LineAttribution.from_dict(item) for item in decoded if isinstance(item, dict)
        ]
    except (TypeError, ValueError):
        attributions = []
    if len(attributions) != line_count:
        return [LineAttribution("unknown") for _ in range(line_count)]
    return attributions


def _apply_human_claim(
    attributions: list[LineAttribution],
    start_line: int,
    end_line: int,
    identity: str,
) -> int:
    human = LineAttribution(
        "human",
        actor_id="h_" + hashlib.sha256(identity.encode()).hexdigest()[:14],
        agent_name=identity,
    )
    claimed = 0
    for index in range(start_line - 1, end_line):
        if attributions[index].actor_kind == "unknown":
            attributions[index] = human
            claimed += 1
    if not claimed:
        raise ValueError("selected range has no unknown lines to claim")
    return claimed


def _load_attribution(value: str) -> LineAttribution:
    try:
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return LineAttribution.from_dict(decoded)
    except (TypeError, ValueError):
        pass
    return LineAttribution("unknown")


def _optional_string(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _text_lines(content: bytes) -> list[str]:
    try:
        return content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []


def _normalize_path(path: str) -> str:
    if not path or "\\" in path or "\x00" in path:
        raise ValueError(f"invalid worktree-relative path: {path!r}")
    parsed = PurePosixPath(path)
    normalized = parsed.as_posix()
    path_invalid = (
        parsed.is_absolute()
        or normalized in ("", ".")
        or ".." in parsed.parts
        or normalized != path
    )
    if path_invalid:
        raise ValueError(f"invalid worktree-relative path: {path!r}")
    return normalized


def _validate_stage(stage: str) -> None:
    if stage not in ("before", "after"):
        raise ValueError(f"invalid snapshot stage: {stage}")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        value = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
