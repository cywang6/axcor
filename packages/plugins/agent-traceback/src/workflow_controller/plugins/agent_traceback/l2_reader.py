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

"""主 agent 对话史读取 + 预制 session（L2 重建）。

从 opencode.db 读主 session 的 part 流，按 **message 级**截断到检查点时刻（subagent X
所在 message 的全部 part 及其之后）。``seed_prebuilt_session`` 把截断 part 流写回
opencode.db 成新 session，新 worker ``--resume`` 它续跑（原生对话史保真）。

低层只读原语（路径解析 / 只读连接 / 行查询 / part 解码）来自
``workflow_controller.transcript.opencode_store``——opencode.db 只读访问的单一定义处。
"""

from __future__ import annotations

__all__ = [
    "find_exclude_message_id",
    "find_main_part_id_for_subagent",
    "get_last_seed_error",
    "read_main_session_messages",
    "read_main_session_parts",
    "seed_prebuilt_session",
]

import contextlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from workflow_controller.transcript.opencode_store import (
    BUSY_TIMEOUT_MS,
    fetch_rows,
    normalize_part,
    open_ro_db,
    opencode_db_path,
    parse_part,
)

logger = logging.getLogger(__name__)

# ── 主 agent 对话史读取 + 预制 session ─────────────────────────────────


def read_main_session_parts(
    main_session_id: str,
    env: dict[str, str] | None = None,
    up_to_part_id: str | None = None,
    max_parts: int = 2000,
    exclude_part_id: str | None = None,
    exclude_message_id: str | None = None,
) -> list[tuple[str, int, str, str]]:
    """Read the main agent session's part stream as ``(part_id, time_created, message_id, data_json)``.

    Ordered by ``time_created ASC, id ASC``. ``up_to_part_id`` truncates at that
    part (inclusive) — only parts at or before the checkpoint moment.

    ``exclude_message_id`` (message 级截断)：一旦遇到该 message_id 的 part 立即
    ``break``——丢弃该 message 的全部 part（含「我将调用 X」的前置 text/reasoning）及
    其之后所有 part。消除「孤儿文本」：worker 看不到「说了要派 X 但没有调用」的矛盾 L2，
    从上一条完整 message 自然续跑。优先于 ``exclude_part_id``。

    ``exclude_part_id`` (part 级截断，旧路径)：排除该 part 及之后所有。当
    ``exclude_message_id`` 不可得（schema 漂移 / 旧 db 无 message_id）时退化为 part 级。
    优先于 ``up_to_part_id``。

    Best-effort; ``[]`` on any error.
    """
    if not main_session_id:
        return []
    rows = fetch_rows(
        "SELECT id, time_created, message_id, data FROM part "
        "WHERE session_id = ? ORDER BY time_created ASC, id ASC",
        (main_session_id,),
        env,
    )
    if not rows:
        return []
    out: list[tuple[str, int, str, str]] = []
    for pid, tc, mid, raw in rows:
        # message 级截断：命中 X 所在 message 的任意 part → 立即 break
        # （丢该 message 全部 + 之后，含同 message 的「我将调用 X」前置 text/reasoning）
        if exclude_message_id and mid and str(mid) == exclude_message_id:
            break
        if exclude_part_id and str(pid) == exclude_part_id:
            break
        out.append((str(pid), int(tc) if tc else 0, str(mid or ""), str(raw)))
        if up_to_part_id and str(pid) == up_to_part_id:
            break
        if len(out) >= max_parts:
            break
    return out


def read_main_session_messages(
    main_session_id: str,
    env: dict[str, str] | None = None,
    up_to_part_id: str | None = None,
    max_parts: int = 2000,
    exclude_part_id: str | None = None,
    exclude_message_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten :func:`read_main_session_parts` into normalized messages.

    Truncation params (``up_to_part_id`` / ``exclude_part_id`` / ``exclude_message_id``)
    are forwarded; see that function. Best-effort; never raises.
    """
    messages: list[dict[str, Any]] = []
    for _pid, _tc, _mid, raw in read_main_session_parts(
        main_session_id,
        env,
        up_to_part_id,
        max_parts,
        exclude_part_id,
        exclude_message_id,
    ):
        part = parse_part(raw)
        if part is not None:
            messages.extend(normalize_part(part))
    return messages


def find_main_part_id_for_subagent(
    main_session_id: str,
    sub_agent_id: str,
    env: dict[str, str] | None = None,
) -> str | None:
    """Best-effort: the main session's ``task`` part id whose output references ``sub_agent_id``.

    opencode's ``task`` is a post-completion event — when the main agent calls it,
    the subagent has finished, and ``state.output`` carries the sub-session id.
    This locates that part id (the checkpoint truncation point). ``None`` on failure.
    """
    if not main_session_id or not sub_agent_id:
        return None
    rows = fetch_rows(
        "SELECT id, data FROM part WHERE session_id = ? ORDER BY time_created ASC, id ASC",
        (main_session_id,),
        env,
    )
    if not rows:
        return None
    for pid, raw in rows:
        part = parse_part(raw)
        if part is None:
            continue
        if part.get("type") != "tool" or part.get("tool") != "task":
            continue
        # task part's state.output carries the sub-session id (ses_xxx)
        if sub_agent_id in str((part.get("state") or {}).get("output", "")):
            return str(pid)
    return None


def find_exclude_message_id(
    main_session_id: str,
    part_id: str,
    env: dict[str, str] | None = None,
) -> str | None:
    """找 X 的 task part 所属的 ``message_id``（message 级截断用）。

    opencode 的一条 assistant 消息可含多 part（reasoning + text + task），共享同一
    ``message_id``。part 级截断（只丢 task part）会留下同 message 的「我将调用 X」前置
    text/reasoning——形成「孤儿文本」，worker 可能误以为自己已派过 X → 跳过 X。

    message 级截断：丢弃该 message_id 的全部 part 及其之后，worker 看不到「我将调用 X」
    的前置声明，从上一条完整 message 自然续跑。

    ``None`` on failure（part 不存在 / 无 message_id / schema 漂移）——调用方退化为
    part 级截断（``exclude_part_id``，best-effort）。
    """
    if not main_session_id or not part_id:
        return None
    rows = fetch_rows(
        "SELECT id, message_id FROM part WHERE session_id = ? AND id = ?",
        (main_session_id, part_id),
        env,
    )
    if not rows or not rows[0][1]:
        return None
    return str(rows[0][1])


# Module-level slot for the last seed error (read by get_last_seed_error for
# diagnostics; traceback_runner reports it when seeding fails).
_last_seed_error: list[str] = [""]


def get_last_seed_error() -> str:
    """Return the last seed_prebuilt_session error string (for diagnostics)."""
    return _last_seed_error[0]


# fork db 初始化时拷数据行的全局小表：迁移版本表（新旧 schema 记账表都列上）
# + project/agent 元数据（session.project_id 的 FK 目标行）。
# session 范围表只建 schema，行由 seed / transcribe 显式写入；
# credential/account 类敏感表刻意不拷。
_FORK_DB_COPY_DATA_TABLES: tuple[str, ...] = (
    "__drizzle_migrations",
    "migration",
    "data_migration",
    "project",
    "project_directory",
    "agent",
)

# 已知的全部表，schema 自检据此发现 opencode 漂移新增的未知表。
# account/credential 等为全局表：只建 schema、不拷行（敏感数据不进 fork 目录）。
_FORK_DB_KNOWN_TABLES: frozenset[str] = frozenset(
    {
        "part",
        "message",
        "session",
        "todo",
        "event",
        "event_sequence",
        "session_input",
        "session_message",
        "session_share",
        "session_context_epoch",
        "sqlite_sequence",
        "account",
        "account_state",
        "control_account",
        "credential",
        "permission",
        "workspace",
    }
) | frozenset(_FORK_DB_COPY_DATA_TABLES)


def _copy_schema_statements(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """按 table → view → index → trigger 依赖序回放 CREATE 语句。"""
    schema_rows = src.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL",
    ).fetchall()
    type_order = {"table": 0, "view": 1, "index": 2, "trigger": 3}
    schema_rows.sort(key=lambda r: type_order.get(str(r[0]), 4))
    for _type, name, sql in schema_rows:
        if str(name).startswith("sqlite_"):
            continue
        dest.execute(str(sql))


def _copy_user_version(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """复制 ``PRAGMA user_version``（保真零成本；drizzle 用迁移表，不用它）。"""
    uv = int(src.execute("PRAGMA user_version").fetchone()[0] or 0)
    if uv:
        dest.execute(f"PRAGMA user_version = {uv}")


def _copy_global_table_rows(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """拷全局小表数据行（动态列发现，缺表/空表跳过）。"""
    for table in _FORK_DB_COPY_DATA_TABLES:
        cols = []
        for c in src.execute(
            f'PRAGMA table_info("{table}")',
        ).fetchall():
            cols.append(c[1])
        if not cols:
            continue
        data_rows = src.execute(
            f'SELECT {",".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"',
        ).fetchall()
        if not data_rows:
            continue
        placeholders = ",".join("?" for _ in cols)
        dest.executemany(
            f'INSERT OR REPLACE INTO "{table}" '
            f"({','.join(chr(34) + c + chr(34) for c in cols)}) "
            f"VALUES ({placeholders})",
            data_rows,
        )


def _warn_unknown_tables(src: sqlite3.Connection) -> None:
    """schema 漂移自检：未知表 warning，提醒评估是否需要拷其数据。"""
    try:
        names = []
        for (n,) in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        ).fetchall():
            names.append(str(n))
        unknown = [n for n in names if n not in _FORK_DB_KNOWN_TABLES]
        if unknown:
            logger.warning(
                "traceback: fork db has %d unknown table(s) not in known list: %s "
                "— evaluate if seeding must copy their rows",
                len(unknown),
                ", ".join(unknown),
            )
    except sqlite3.Error:
        pass


def _close_fork_db_conns(*cons: sqlite3.Connection | None) -> None:
    """关闭已建立的 fork db 连接（None 跳过；close 失败不抛）。"""
    for con in cons:
        if con is not None:
            with contextlib.suppress(sqlite3.Error):
                con.close()


def _remove_partial_fork_db(dest_path: Path) -> None:
    """初始化失败时清理半成品 fork db 文件（含 ``-wal``/``-shm``）。"""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{dest_path}{suffix}").unlink(missing_ok=True)


def _init_fork_db_from_source(dest_path: Path, source_path: Path) -> bool:
    """初始化 fork db：从 source 做 schema-only 拷贝（不拷 session 数据行）。

    只复制 CREATE 语句（按 table → view → index → trigger 依赖序回放）、
    ``PRAGMA user_version``、全局小表数据行（``_FORK_DB_COPY_DATA_TABLES``），
    代价 O(表数量)，与源库体积无关。敏感表（credential/account 类）只建 schema
    不拷行。失败时清理半成品 dest 文件（含 ``-wal``/``-shm``）。
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src: sqlite3.Connection | None = None
    dest: sqlite3.Connection | None = None
    ok = True
    try:
        src = sqlite3.connect(
            f"file:{source_path}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        src.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        dest = sqlite3.connect(str(dest_path), timeout=BUSY_TIMEOUT_MS / 1000)
        dest.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # journal 模式与源库一致（WAL）
        with contextlib.suppress(sqlite3.Error):
            dest.execute("PRAGMA journal_mode=WAL")
        # 1. schema：按依赖顺序回放 CREATE 语句（view/index/trigger 依赖 table）
        _copy_schema_statements(src, dest)
        # 2. user_version
        _copy_user_version(src, dest)
        # 3. 全局小表数据行
        _copy_global_table_rows(src, dest)
        dest.commit()
        # 4. schema 漂移自检
        _warn_unknown_tables(src)
    except (sqlite3.Error, OSError):
        ok = False
    finally:
        _close_fork_db_conns(dest, src)
        if not ok:
            _remove_partial_fork_db(dest_path)
    return ok


@dataclass
class _SeedSource:
    """seed 的源侧输入快照：session 行（已 override）+ 截断 part 流 + message 行 + id 重映射。"""

    cols: list[str]
    src_dict: dict[str, Any]
    origin_cwd: str
    now: int
    parts: list[tuple[str, int, str, str]]
    msg_cols: list[str]
    msg_rows: list[tuple[Any, ...]]
    msg_id_remap: dict[str, str]


def _load_source_session_row(
    source_path: Path,
    source_session_id: str,
) -> tuple[dict[str, Any], list[str]] | None:
    """读源 session 行（全列，动态适配 schema 漂移）；失败返回 None。"""
    try:
        con_ro = open_ro_db(source_path)
        if con_ro is None:
            return None
        cols = [c[1] for c in con_ro.execute("PRAGMA table_info(session)").fetchall()]
        if not cols or "id" not in cols:
            con_ro.close()
            return None
        src_row = con_ro.execute(
            f"SELECT {','.join(cols)} FROM session WHERE id = ?",
            (source_session_id,),
        ).fetchone()
        con_ro.close()
    except sqlite3.Error:
        return None
    if not src_row:
        return None
    return dict(zip(cols, src_row, strict=False)), cols


def _override_session_row(
    src_dict: dict[str, Any],
    *,
    new_session_id: str,
    title: str,
    directory: str | None,
    now: int,
) -> str:
    """按 seed 需求覆盖 session 行字段；返回 rewrite 前保存的源 cwd。"""
    # 源 session 的 cwd（rewrite 前保存）——seed 的 part/message data 含源 cwd 的
    # 绝对路径（tool filePath、cwd 文本等）。若不 rewrite 为 fork cwd，resume 后主
    # agent 沿用旧绝对路径读写文件 → 文件落到源目录而非 fork cwd → 快照引擎镜像
    # fork cwd 捕获不到新文件 → 重放 session 的检查点不含 subagent 新产物。
    origin_cwd: str = src_dict.get("directory") or ""
    # override：新 id、title、parent_id=NULL（顶层 session，见 seed docstring）、fresh timestamps
    src_dict["id"] = new_session_id
    if "title" in src_dict:
        src_dict["title"] = title
    if "parent_id" in src_dict:
        # 置空：重放 session 是顶层 workflow，不是源 session 的子 agent。opencode 的
        # task 工具按 parentID 链算 subagent depth；留 parent_id=source 会让重放 session
        # 被算作 depth 1 → subagent_depth（默认 1）已满 → 重放里无法再派 subagent。
        # fork 溯源由 Workflow Controller 的 birth_traceback plugin_metadata 记录。
        src_dict["parent_id"] = None
    for tcol in ("time_created", "time_updated"):
        if tcol in src_dict:
            src_dict[tcol] = now
    if "time_compacting" in src_dict:
        src_dict["time_compacting"] = None
    # 覆盖 directory / path 到 fork cwd：resume 时 --dir 须与 session 存储目录一致，
    # 否则 opencode 悬死。path 设为空串（与 opencode 真实 session 一致）。
    if directory:
        if "directory" in src_dict:
            src_dict["directory"] = directory
        if "path" in src_dict:
            src_dict["path"] = ""
    return origin_cwd


def _load_message_rows(
    source_path: Path,
    source_session_id: str,
    msg_ids: set[str],
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """读 kept parts 所属的 message 行（role/parentID 链）；失败返回空。

    parts 的 message_id 引用这些 id；不复制则 opencode --session resume 时
    parts 悬空 → 零输出悬死。同样从 source db 读。
    """
    cols: list[str] = []
    rows: list[tuple[Any, ...]] = []
    if not msg_ids:
        return cols, rows
    try:
        con_ro2 = open_ro_db(source_path)
        if con_ro2 is not None:
            mcols = []
            for c in con_ro2.execute(
                "PRAGMA table_info(message)",
            ).fetchall():
                mcols.append(c[1])
            if mcols and "id" in mcols and "session_id" in mcols:
                all_msgs = con_ro2.execute(
                    f"SELECT {','.join(mcols)} FROM message "
                    "WHERE session_id = ? ORDER BY time_created ASC",
                    (source_session_id,),
                ).fetchall()
                id_idx = mcols.index("id")
                rows = [r for r in all_msgs if r[id_idx] in msg_ids]
                cols = mcols
            con_ro2.close()
    except sqlite3.Error:
        pass
    return cols, rows


def _build_msg_id_remap(
    parts: list[tuple[str, int, str, str]],
    msg_cols: list[str],
    msg_rows: list[tuple[Any, ...]],
) -> dict[str, str]:
    """把 ``m_*`` 前缀的 message id 重映射为 opencode schema 要求的 ``msg`` 前缀。

    opencode MessageV2 schema requires message id to start with "msg";
    older transcriber versions produced "m_rc_*" ids. Remap "m_*" → "msg_*"
    (idempotent for already-compliant "msg_*" ids).
    """
    remap: dict[str, str] = {}
    for _pid, _tc, mid, _raw in parts:
        if mid and mid not in remap and not str(mid).startswith("msg"):
            remap[str(mid)] = "msg" + str(mid)[1:]
    if msg_cols:
        id_idx = msg_cols.index("id")
        for row in msg_rows:
            mid = row[id_idx]
            if mid and str(mid) not in remap and not str(mid).startswith("msg"):
                remap[str(mid)] = "msg" + str(mid)[1:]
    return remap


def _load_seed_snapshot(
    source_session_id: str,
    read_env: dict[str, str] | None,
    source_path: Path,
    up_to_part_id: str | None,
    exclude_part_id: str | None,
    exclude_message_id: str | None,
    *,
    new_session_id: str,
    title: str,
    directory: str | None,
) -> _SeedSource | None:
    """读源 session 行（override 后）+ 截断 part 流 + message 行 + id 重映射。"""
    loaded = _load_source_session_row(source_path, source_session_id)
    if loaded is None:
        return None
    src_dict, cols = loaded
    now = int(datetime.now(tz=UTC).timestamp() * 1000)
    origin_cwd = _override_session_row(
        src_dict,
        new_session_id=new_session_id,
        title=title,
        directory=directory,
        now=now,
    )
    # 读截断后的 part 流（exclude_message_id 优先：message 级截断消除「孤儿文本」）。
    # 从 source db（read_env）读，不从 fork db 读——fork db 此刻可能尚不存在。
    parts = read_main_session_parts(
        source_session_id,
        read_env,
        up_to_part_id,
        exclude_part_id=exclude_part_id,
        exclude_message_id=exclude_message_id,
    )
    if not parts and not exclude_part_id and not exclude_message_id:
        # re-call X 的首 subagent 可能无前置 part（task part 是第一条）→ 允许空 part
        # 流（仅 session 行 + starting_prompt 驱动 cold resume）。非 re-call 模式无
        # part 则视为失败（源 session 无对话可灌）。
        return None
    msg_ids = {mid for _pid, _tc, mid, _raw in parts if mid}
    msg_cols, msg_rows = _load_message_rows(source_path, source_session_id, msg_ids)
    msg_id_remap = _build_msg_id_remap(parts, msg_cols, msg_rows)
    return _SeedSource(
        cols=cols,
        src_dict=src_dict,
        origin_cwd=origin_cwd,
        now=now,
        parts=parts,
        msg_cols=msg_cols,
        msg_rows=msg_rows,
        msg_id_remap=msg_id_remap,
    )


def _open_dest_db(dest_path: Path) -> sqlite3.Connection | None:
    """连接目标 db（busy timeout）；失败返回 None。"""
    try:
        con = sqlite3.connect(str(dest_path), timeout=BUSY_TIMEOUT_MS / 1000)
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except sqlite3.Error:
        return None
    return con


def _insert_seeded_parts(
    con: sqlite3.Connection,
    snap: _SeedSource,
    *,
    new_session_id: str,
    directory: str | None,
    rewrite: bool,
) -> None:
    """写 part 行：新 part id + 新 session_id，保留 message_id/time/data。"""
    for i, (_pid, tc, mid, raw) in enumerate(snap.parts):
        new_pid = f"prt_tb_{new_session_id[:8]}_{i:04d}"
        if rewrite and directory:
            raw = _rewrite_part_data(raw, snap.origin_cwd, directory)
        effective_mid = (
            snap.msg_id_remap.get(mid, mid) if mid else f"msg_tb_{new_session_id[:8]}_{i:04d}"
        )
        con.execute(
            "INSERT OR REPLACE INTO part "
            "(id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_pid, effective_mid, new_session_id, tc or snap.now, tc or snap.now, raw),
        )


def _insert_seeded_messages(
    con: sqlite3.Connection,
    snap: _SeedSource,
    *,
    new_session_id: str,
    directory: str | None,
    rewrite: bool,
) -> None:
    """写 message 行：保留原 id（remap 后）使 parts 的 message_id 引用有效，改 session_id。"""
    if not snap.msg_rows or not snap.msg_cols:
        return
    # data 含 role/parentID 链，opencode resume 据此重建对话。
    msg_placeholders = ",".join("?" for _ in snap.msg_cols)
    data_col_idx = snap.msg_cols.index("data") if "data" in snap.msg_cols else -1
    for row in snap.msg_rows:
        mdict = dict(zip(snap.msg_cols, row, strict=False))
        mdict["session_id"] = new_session_id
        old_mid = str(mdict.get("id", ""))
        if old_mid in snap.msg_id_remap:
            mdict["id"] = snap.msg_id_remap[old_mid]
        should_rewrite_data = (
            rewrite and directory and data_col_idx >= 0 and isinstance(mdict.get("data"), str)
        )
        if directory is not None and should_rewrite_data:
            mdict["data"] = _rewrite_content_string(
                mdict["data"],
                snap.origin_cwd,
                directory,
            )
        if data_col_idx >= 0 and isinstance(mdict.get("data"), str):
            mdict["data"] = _remap_msg_ids_in_data(
                mdict["data"],
                snap.msg_id_remap,
            )
        con.execute(
            f"INSERT OR REPLACE INTO message ({','.join(snap.msg_cols)}) "
            f"VALUES ({msg_placeholders})",
            [mdict[c] for c in snap.msg_cols],
        )


def _rollback_seeded_rows(con: sqlite3.Connection, new_session_id: str) -> None:
    """写入失败——best-effort 回滚本次（删掉可能写入的新 session/message/part 行）。"""
    try:
        con.execute("DELETE FROM session WHERE id = ?", (new_session_id,))
        con.execute(
            "DELETE FROM message WHERE session_id = ?",
            (new_session_id,),
        )
        con.execute(
            "DELETE FROM part WHERE session_id = ?",
            (new_session_id,),
        )
        con.commit()
    except sqlite3.Error:
        pass


def _write_seed_snapshot(
    dest_path: Path,
    source_path: Path,
    snap: _SeedSource,
    *,
    new_session_id: str,
    directory: str | None,
) -> bool:
    """初始化（若需）并连接目标 db，写入 seeded session/parts/messages；失败回滚。"""
    # 若 dest_path 不存在 → 从 source db schema-only 初始化（migrations 已应用，
    # opencode 打开时不重跑 CREATE TABLE）。fork db 仅含本 fork 的 seeded session。
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    need_init = not dest_path.is_file() or dest_path.stat().st_size == 0
    if need_init and not _init_fork_db_from_source(dest_path, source_path):
        return False
    con = _open_dest_db(dest_path)
    if con is None:
        return False
    # path rewrite：源 cwd → fork cwd（结构化 JSON walk + 规范化比对）。
    # part/message data 里的路径字段（_PATH_FIELDS: filePath/path/filepath/directory/cwd/
    # absPath/workdir）做 resolve + prefix-match 替换；**所有**其他字符串值做 path-prefix-aware
    # 替换（不限字段名，覆盖 text/output/command/content/prompt/description 等全部未知字段）；
    # 非 origin_cwd 绝对路径显式不重写。见 _rewrite_part_data docstring。
    # 注意：origin_cwd 用 db 里的原始字符串做 content 匹配（opencode 写入的是原始形式，
    # 含 symlink 未展开），但 _rewrite_one 内部对 origin_cwd 与 p 都做 resolve 做规范化比对
    # （处理 .. / symlink）。fork_cwd（= directory 参数）l2_reader 不额外 resolve——
    # **调用方（traceback_runner.run）应传 resolve 后的 fork_cwd**：opencode 对 --dir 做
    # symlink 展开（macOS /var → /private/var）算 project worktree，若 directory 未 resolve，
    # seeded session.directory 与 L2 路径不匹配 opencode 的 resolved project worktree → 文件
    # 访问被判 external_directory → 弹权限询问 → headless 模式无用户 → 工具调用悬死。
    # 调用方传 resolved 后，l2_reader 沿用 directory 原字符串即可保持一致。
    rewrite = bool(snap.origin_cwd and directory and snap.origin_cwd != directory)
    try:
        # session 行（INSERT OR REPLACE 以防 new_session_id 已存在）
        placeholders = ",".join("?" for _ in snap.cols)
        con.execute(
            f"INSERT OR REPLACE INTO session ({','.join(snap.cols)}) VALUES ({placeholders})",
            [snap.src_dict[c] for c in snap.cols],
        )
        _insert_seeded_parts(
            con,
            snap,
            new_session_id=new_session_id,
            directory=directory,
            rewrite=rewrite,
        )
        _insert_seeded_messages(
            con,
            snap,
            new_session_id=new_session_id,
            directory=directory,
            rewrite=rewrite,
        )
        con.commit()
    except sqlite3.Error as exc:
        _rollback_seeded_rows(con, new_session_id)
        _last_seed_error[0] = f"{type(exc).__name__}: {exc}"
        con.close()
        return False
    con.close()
    _last_seed_error[0] = ""
    return True


def seed_prebuilt_session(
    *,
    source_session_id: str,
    new_session_id: str,
    up_to_part_id: str | None,
    title: str,
    env: dict[str, str] | None = None,
    source_env: dict[str, str] | None = None,
    exclude_part_id: str | None = None,
    exclude_message_id: str | None = None,
    directory: str | None = None,
) -> bool:
    """往 opencode.db 写一个预制 session + 截断 part 流 + 对应 message 行，供新 worker ``--resume``。

    克隆源 session 行（override id/title/time/parent_id=NULL，``directory`` 覆盖目录
    字段）+ 复制截断点之前的 part + 复制这些 part 所属的 message 行（保留原 id 使
    parts 的 message_id 引用有效、改 session_id 指向新 session）。

    **读写分离**：``source_env`` 读源 db；``env`` 写目标 db（均 ``None`` → 宿主默认）。
    两者不同且 fork db 不存在时从 source db schema-only 初始化——worker 读不到
    origin / 兄弟 session 的 L2「未来」（隔离）。

    截断优先级 ``exclude_message_id`` > ``exclude_part_id`` > ``up_to_part_id``（语义
    见 :func:`read_main_session_parts`）；part / message 的 data 做源 cwd → fork cwd
    路径重写（见 :func:`_rewrite_part_data`）。Best-effort + 防御性：schema 漂移或写入
    失败 → 返回 False（调用方据此中止重放），绝不抛异常（不破坏 opencode.db）。
    """
    if not source_session_id or not new_session_id:
        return False
    # 读写分离：source_path 读 origin 的 db；dest_path 写 fork 的 db（可不同）。
    # source_env=None → 用宿主默认 db（origin 是原始 session / 测试同库场景）。
    # source_env=dict → origin 是重放 session，读其 fork db（嵌套重放）。
    read_env = source_env
    source_path = opencode_db_path(read_env)
    if not source_path.is_file():
        return False
    dest_path = opencode_db_path(env)
    snap = _load_seed_snapshot(
        source_session_id,
        read_env,
        source_path,
        up_to_part_id,
        exclude_part_id,
        exclude_message_id,
        new_session_id=new_session_id,
        title=title,
        directory=directory,
    )
    if snap is None:
        return False
    return _write_seed_snapshot(
        dest_path, source_path, snap, new_session_id=new_session_id, directory=directory
    )


# ── 路径重写（结构化 JSON walk + 规范化比对） ───────────


# 路径字段白名单：值是绝对路径，做 resolve + prefix-match 替换为 fork_cwd/rel。
# schema 漂移时新增字段名进白名单即可。
_PATH_FIELDS: frozenset[str] = frozenset(
    {
        "filePath",
        "filepath",
        "path",
        "directory",
        "cwd",
        "absPath",
        "workdir",
    }
)


def _remap_msg_ids_in_data(raw: str, remap: dict[str, str]) -> str:
    """Remap ``id`` / ``parentID`` fields in message data JSON to "msg" prefix.

    Keeps part→message→parent chain consistent with the column-level id remap.
    Parse failure → returned as-is.
    """
    if not raw or not remap:
        return raw
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(d, dict):
        return raw
    changed = False
    for k in ("id", "parentID"):
        v = d.get(k)
        if v and str(v) in remap:
            d[k] = remap[str(v)]
            changed = True
    if not changed:
        return raw
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


# 注：``_rewrite_paths`` 对所有非 ``_PATH_FIELDS`` 的字符串值均做 path-prefix-aware
# 替换（不限字段名），故不再需要内容字段白名单做过滤。常见的可含路径的字符串字段有：
# output / command / content / text / prompt / description。


def _rewrite_part_data(raw: str, origin_cwd: str, fork_cwd: str) -> str:
    """解析 part.data JSON、walk 树重写路径字段、re-serialize。

    ``origin_cwd`` / ``fork_cwd`` 都是调用方传入的字符串形式。``_rewrite_one`` 内部对
    origin_cwd 与 p 都做 resolve 做规范化比对（处理 ``..`` / symlink），用 fork_cwd
    字符串拼接结果。``_rewrite_content_string`` 用原始 origin_cwd 字符串做 pattern 匹配
    （opencode 写入的是原始形式）。调用方（traceback_runner）应传 resolve 后的
    fork_cwd，使 seeded session.directory / L2 路径与 opencode project worktree 一致。

    parse 失败（非 JSON / 空）→ 退化为 path-prefix-aware 字符串替换（best-effort）。
    """
    if not raw:
        return raw
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 非 JSON → 朴素 path-prefix-aware 替换（best-effort）
        return _rewrite_content_string(raw, origin_cwd, fork_cwd)
    _rewrite_paths(obj, origin_cwd, fork_cwd)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _rewrite_paths(obj: Any, origin_cwd: str, fork_cwd: str) -> None:
    """递归 walk JSON 树，按字段类别重写路径。

    - dict：路径字段（``_PATH_FIELDS``）→ ``_rewrite_one``（resolve + prefix-match）；
      其他 **所有** 字符串值 → ``_rewrite_content_string``（path-prefix-aware 替换，
      不限字段名——覆盖 ``prompt`` / ``description`` / ``text`` 等所有未知字段）；
      其余递归。
    - list：逐元素递归。
    - 标量：不动（路径重写只在字段名上下文里做）。
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in _PATH_FIELDS:
                obj[k] = _rewrite_one(v, origin_cwd, fork_cwd)
            elif isinstance(v, str):
                obj[k] = _rewrite_content_string(v, origin_cwd, fork_cwd)
            else:
                _rewrite_paths(v, origin_cwd, fork_cwd)
    elif isinstance(obj, list):
        for v in obj:
            _rewrite_paths(v, origin_cwd, fork_cwd)


def _rewrite_one(p: Any, origin_cwd: str, fork_cwd: str) -> Any:
    """路径字段重写：``Path.resolve`` + ``is_relative_to(origin_cwd_resolved)`` prefix 比对。

    对 ``p`` 与 ``origin_cwd`` 都做 resolve 做规范化比对（处理 ``..`` / symlink——
    macOS ``/var`` → ``/private/var`` 展开后两侧一致），命中则用 ``fork_cwd`` 字符串
    拼接 rel。``fork_cwd`` 应由调用方传 resolve 后的形式（与 opencode project
    worktree 保持一致），l2_reader 不额外 resolve 以保留调用方形式。
    消除 ``str.replace`` 的子串误伤（``/foo/bar`` 不再匹配 ``/foo/bar-baz``）。
    非 origin_cwd 绝对路径**显式不重写**（结合 fs 隔离，worker 即便读到旧路径也访问不到）。
    """
    if not isinstance(p, str) or not p:
        return p
    try:
        rp = Path(p).expanduser()
        if not rp.is_absolute():
            return p  # 相对路径不动，opencode 以 cwd 解析
        rp = rp.resolve(strict=False)
        origin_resolved = Path(origin_cwd).expanduser().resolve(strict=False)
        if rp.is_relative_to(origin_resolved):
            rel = rp.relative_to(origin_resolved)
            # 用原始 fork_cwd 字符串拼接（不 resolve），保留调用方 directory 形式
            return str(Path(fork_cwd) / rel)
    except (ValueError, OSError):
        return p
    return p


def _rewrite_content_string(s: str, origin_cwd: str, fork_cwd: str) -> str:
    """内容字段重写：path-prefix-aware 字符串替换。

    替换 ``origin_cwd`` 后跟**非路径段字符**（负向先行断言 ``(?![a-zA-Z0-9_.-])``）
    的 occurrence——即后跟 ``/`` / ``\\`` / 空格 / 中文标点 / 末尾等皆命中，而
    ``origin_cwd-foo`` / ``origin_cwd_extra`` 等合法文件名字符衔接不命中（消除
    ``/foo/bar-baz`` 子串误伤）。用**原始** origin_cwd 字符串做 pattern（opencode
    写入的是原始形式，含 symlink 未展开），用 fork_cwd 字符串替换（调用方应传
    resolve 后的 fork_cwd，与 opencode project worktree 一致）。
    """
    if not s or origin_cwd == fork_cwd:
        return s
    pattern = re.escape(origin_cwd) + r"(?![a-zA-Z0-9_.-])"
    return re.sub(pattern, fork_cwd, s)
