#!/usr/bin/env python3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile


DURABLE_TABLES = ("sdk_sessions", "observations", "session_summaries", "user_prompts")
SYNC_COLUMNS = {"synced_at", "origin_device_id", "origin_local_id", "sync_rev"}
STABLE_COLUMNS = {
    "observations": (
        "text", "type", "title", "subtitle", "facts", "narrative", "concepts",
        "files_read", "files_modified", "prompt_number", "discovery_tokens",
        "created_at", "created_at_epoch", "generated_by_model", "merged_into_project",
        "agent_type", "agent_id", "metadata",
    ),
    "session_summaries": (
        "request", "investigated", "learned", "completed", "next_steps", "files_read",
        "files_edited", "notes", "prompt_number", "discovery_tokens", "created_at",
        "created_at_epoch", "merged_into_project",
    ),
    "user_prompts": ("prompt_number", "prompt_text", "created_at", "created_at_epoch"),
}


class FleetMemoryDatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseReport:
    quick_check: str
    foreign_key_errors: tuple[tuple, ...]
    schema_version: int
    counts: dict[str, int]


@dataclass(frozen=True)
class MergeReport:
    inserted_sessions: int
    inserted_observations: int
    inserted_summaries: int
    inserted_prompts: int

    @property
    def total_inserted(self):
        return self.inserted_sessions + self.inserted_observations + self.inserted_summaries + self.inserted_prompts


def regular_file(path):
    path = Path(path)
    try:
        details = os.lstat(path)
    except FileNotFoundError as error:
        raise FleetMemoryDatabaseError(f"Database does not exist: {path}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise FleetMemoryDatabaseError(f"Database is not a safe regular file: {path}")
    return path


def readonly_connection(path):
    path = regular_file(path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def check_database(path):
    try:
        connection = readonly_connection(path)
        quick_rows = tuple(row[0] for row in connection.execute("PRAGMA quick_check"))
        if quick_rows != ("ok",):
            raise FleetMemoryDatabaseError(f"SQLite quick check failed: {quick_rows[0] if quick_rows else 'no result'}")
        foreign = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
        schema = connection.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
        if not isinstance(schema, int):
            raise FleetMemoryDatabaseError("Claude-mem schema version is missing.")
        counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in DURABLE_TABLES}
        counts["pending_messages"] = connection.execute("SELECT count(*) FROM pending_messages").fetchone()[0]
        counts["pending"] = connection.execute("SELECT count(*) FROM pending_messages WHERE status='pending'").fetchone()[0]
        counts["processing"] = connection.execute("SELECT count(*) FROM pending_messages WHERE status='processing'").fetchone()[0]
        connection.close()
        return DatabaseReport("ok", foreign, schema, counts)
    except FleetMemoryDatabaseError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise FleetMemoryDatabaseError(f"Could not verify SQLite database: {path}") from error


def fsync_path(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def snapshot_database(source, destination):
    source = regular_file(source)
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FleetMemoryDatabaseError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    check_database(source)
    try:
        source_connection = readonly_connection(source)
        target_connection = sqlite3.connect(destination)
        source_connection.backup(target_connection)
        target_connection.close()
        source_connection.close()
        os.chmod(destination, 0o600)
        fsync_path(destination)
        fsync_path(destination.parent)
        report = check_database(destination)
        return report
    except BaseException:
        if os.path.lexists(destination):
            os.unlink(destination)
        raise


def table_columns(connection, table):
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))


def row_values(row, columns):
    return tuple(row[column] for column in columns)


def insert_row(connection, table, row, *, omit=("id",)):
    columns = tuple(column for column in row.keys() if column not in omit)
    placeholders = ", ".join("?" for _ in columns)
    names = ", ".join(columns)
    cursor = connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", row_values(row, columns))
    return cursor.lastrowid


def identity(row):
    return (row["platform_source"] or "claude", row["content_session_id"])


def complete_status(left, right):
    rank = {"active": 0, "failed": 1, "completed": 2}
    return left if rank.get(left, -1) >= rank.get(right, -1) else right


def merge_session(connection, target, source):
    start_source = source["started_at_epoch"] < target["started_at_epoch"]
    target_end = target["completed_at_epoch"]
    source_end = source["completed_at_epoch"]
    end_source = source_end is not None and (target_end is None or source_end > target_end)
    connection.execute(
        """UPDATE sdk_sessions SET
        user_prompt=?, started_at=?, started_at_epoch=?, completed_at=?, completed_at_epoch=?,
        status=?, prompt_counter=?, custom_title=? WHERE id=?""",
        (
            target["user_prompt"] or source["user_prompt"],
            source["started_at"] if start_source else target["started_at"],
            source["started_at_epoch"] if start_source else target["started_at_epoch"],
            source["completed_at"] if end_source else target["completed_at"],
            source_end if end_source else target_end,
            complete_status(target["status"], source["status"]),
            max(target["prompt_counter"] or 0, source["prompt_counter"] or 0),
            target["custom_title"] or source["custom_title"],
            target["id"],
        ),
    )


def stable_fingerprint(table, session_key, row):
    values = [table, *session_key]
    values.extend(row[column] for column in STABLE_COLUMNS[table])
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def origin_key(row):
    device = row["origin_device_id"] if "origin_device_id" in row.keys() else None
    local = row["origin_local_id"] if "origin_local_id" in row.keys() else None
    return (device, local) if device is not None and local is not None else None


def copy_children(target, source, table, source_sessions, target_sessions):
    inserted = 0
    existing_origins = set()
    existing_hashes = set()
    existing_fingerprints = set()
    for row in target.execute(f"SELECT * FROM {table}"):
        if table == "user_prompts":
            session = target_sessions["by_id"].get(row["session_db_id"])
        else:
            session = target_sessions["by_memory"].get(row["memory_session_id"])
        if session is None:
            raise FleetMemoryDatabaseError(f"{table} has no target session.")
        session_key = identity(session)
        key = origin_key(row)
        if key is not None:
            existing_origins.add(key)
        if table == "observations" and row["content_hash"] is not None:
            existing_hashes.add((session["memory_session_id"], row["content_hash"]))
        existing_fingerprints.add(stable_fingerprint(table, session_key, row))

    for source_row in source.execute(f"SELECT * FROM {table} ORDER BY id"):
        row = dict(source_row)
        if table == "user_prompts":
            source_session = source_sessions["by_id"].get(source_row["session_db_id"])
        else:
            source_session = source_sessions["by_memory"].get(source_row["memory_session_id"])
        if source_session is None:
            raise FleetMemoryDatabaseError(f"{table} has no source session.")
        target_session = target_sessions["by_identity"][identity(source_session)]
        session_key = identity(target_session)
        if table == "user_prompts":
            row["session_db_id"] = target_session["id"]
            row["content_session_id"] = target_session["content_session_id"]
        else:
            row["memory_session_id"] = target_session["memory_session_id"]
            row["project"] = target_session["project"]
        key = origin_key(source_row)
        content_key = None
        if table == "observations" and source_row["content_hash"] is not None:
            content_key = (target_session["memory_session_id"], source_row["content_hash"])
        fingerprint = stable_fingerprint(table, session_key, row)
        if (key is not None and key in existing_origins) or (content_key is not None and content_key in existing_hashes) or fingerprint in existing_fingerprints:
            continue
        insert_row(target, table, row)
        inserted += 1
        if key is not None:
            existing_origins.add(key)
        if content_key is not None:
            existing_hashes.add(content_key)
        existing_fingerprints.add(fingerprint)
    return inserted


def session_maps(connection):
    rows = tuple(connection.execute("SELECT * FROM sdk_sessions"))
    return {
        "rows": rows,
        "by_identity": {identity(row): row for row in rows},
        "by_memory": {row["memory_session_id"]: row for row in rows},
        "by_id": {row["id"]: row for row in rows},
    }


def verify_compatible(target, source):
    for table in DURABLE_TABLES:
        if table_columns(target, table) != table_columns(source, table):
            raise FleetMemoryDatabaseError(f"Claude-mem table schema differs: {table}")


def recovered_memory_id(platform, content):
    digest = hashlib.sha256(f"{platform}\0{content}".encode()).hexdigest()
    return f"fleet-null-{digest}"


def epoch_text(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def repair_legacy_sessions(connection):
    inserted = 0
    for row in connection.execute(
        "SELECT id, platform_source, content_session_id FROM sdk_sessions WHERE memory_session_id IS NULL"
    ).fetchall():
        memory_id = recovered_memory_id(row["platform_source"] or "claude", row["content_session_id"])
        conflict = connection.execute(
            "SELECT id FROM sdk_sessions WHERE memory_session_id=? AND id<>?", (memory_id, row["id"])
        ).fetchone()
        if conflict is not None:
            raise FleetMemoryDatabaseError("Recovered memory session ID conflicts with an existing session.")
        connection.execute("UPDATE sdk_sessions SET memory_session_id=? WHERE id=?", (memory_id, row["id"]))

    orphan_rows = connection.execute(
        """WITH durable AS (
            SELECT memory_session_id, project, created_at_epoch, prompt_number FROM observations
            UNION ALL
            SELECT memory_session_id, project, created_at_epoch, prompt_number FROM session_summaries
        )
        SELECT d.memory_session_id, min(d.project) AS project,
               min(d.created_at_epoch) AS started_epoch,
               max(d.created_at_epoch) AS completed_epoch,
               max(coalesce(d.prompt_number, 0)) AS prompt_counter,
               count(DISTINCT d.project) AS project_count
        FROM durable d
        LEFT JOIN sdk_sessions s ON s.memory_session_id=d.memory_session_id
        WHERE s.id IS NULL
        GROUP BY d.memory_session_id
        ORDER BY d.memory_session_id"""
    ).fetchall()
    for row in orphan_rows:
        if row["memory_session_id"] is None or row["project_count"] != 1:
            raise FleetMemoryDatabaseError("Legacy memory cannot be assigned to one project.")
        content_id = f"fleet-orphan-{row['memory_session_id']}"
        connection.execute(
            """INSERT INTO sdk_sessions
            (content_session_id, memory_session_id, project, platform_source, user_prompt,
             started_at, started_at_epoch, completed_at, completed_at_epoch, status,
             worker_port, prompt_counter, custom_title)
            VALUES (?, ?, ?, 'claude', NULL, ?, ?, ?, ?, 'completed', NULL, ?, NULL)""",
            (
                content_id,
                row["memory_session_id"],
                row["project"],
                epoch_text(row["started_epoch"]),
                row["started_epoch"],
                epoch_text(row["completed_epoch"]),
                row["completed_epoch"],
                row["prompt_counter"],
            ),
        )
        inserted += 1
    remaining = tuple(connection.execute("PRAGMA foreign_key_check"))
    if remaining:
        raise FleetMemoryDatabaseError("Legacy parent repair left foreign key errors.")
    return inserted


def merge_databases(server, client, output):
    server_report = check_database(server)
    client_report = check_database(client)
    if server_report.schema_version != client_report.schema_version:
        raise FleetMemoryDatabaseError("Claude-mem schema versions differ.")
    output = Path(output)
    snapshot_database(server, output)
    descriptor, client_copy_name = tempfile.mkstemp(prefix=".fleet-memory-client-", suffix=".db", dir=output.parent)
    os.close(descriptor)
    os.unlink(client_copy_name)
    client_copy = Path(client_copy_name)
    snapshot_database(client, client_copy)
    target = None
    source = None
    try:
        target = sqlite3.connect(output)
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys=ON")
        repair_legacy_sessions(target)
        target.commit()
        normalized_client = sqlite3.connect(client_copy)
        normalized_client.row_factory = sqlite3.Row
        normalized_client.execute("PRAGMA foreign_keys=ON")
        repair_legacy_sessions(normalized_client)
        normalized_client.commit()
        normalized_client.close()
        source = readonly_connection(client_copy)
        verify_compatible(target, source)
        target_maps = session_maps(target)
        source_maps = session_maps(source)
        inserted_sessions = 0
        for source_session in source_maps["rows"]:
            source_memory = source_session["memory_session_id"]
            if source_memory is None:
                raise FleetMemoryDatabaseError("A source session has no memory session ID.")
            session_identity = identity(source_session)
            target_session = target_maps["by_identity"].get(session_identity)
            conflicting = target_maps["by_memory"].get(source_memory)
            if target_session is None and conflicting is not None and identity(conflicting) != session_identity:
                raise FleetMemoryDatabaseError("One memory session ID maps to two session identities.")
            if target_session is not None:
                merge_session(target, target_session, source_session)
                continue
            insert_row(target, "sdk_sessions", source_session)
            inserted_sessions += 1
            target_maps = session_maps(target)

        target_maps = session_maps(target)
        inserted_observations = copy_children(target, source, "observations", source_maps, target_maps)
        inserted_summaries = copy_children(target, source, "session_summaries", source_maps, target_maps)
        inserted_prompts = copy_children(target, source, "user_prompts", source_maps, target_maps)
        for table in ("observations", "session_summaries", "user_prompts"):
            target.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES('rebuild')")
        target.commit()
        source.close()
        target.close()
        os.chmod(output, 0o600)
        fsync_path(output)
        report = check_database(output)
        if report.foreign_key_errors:
            raise FleetMemoryDatabaseError("Merged database has foreign key errors.")
        return MergeReport(inserted_sessions, inserted_observations, inserted_summaries, inserted_prompts)
    except BaseException:
        if source is not None:
            source.close()
        if target is not None:
            target.close()
        if os.path.lexists(output):
            os.unlink(output)
        raise
    finally:
        if os.path.lexists(client_copy):
            os.unlink(client_copy)


def prune_daily_backups(directory, keep):
    directory = Path(directory)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise FleetMemoryDatabaseError("Backup retention must be positive.")
    daily = sorted(
        path for path in directory.iterdir()
        if path.is_file() and not path.is_symlink() and re.fullmatch(r"daily-[0-9]{8}T[0-9]{6}Z\.db", path.name)
    )
    removed = daily[:-keep]
    for path in removed:
        path.unlink()
    return removed
