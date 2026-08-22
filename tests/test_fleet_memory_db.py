#!/usr/bin/env python3
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1] if len(sys.argv) < 2 else Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("fleet_memory_db", ROOT / "lib" / "fleet_memory_db.py")
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT);
INSERT INTO schema_versions VALUES (49, '2026-08-22');
CREATE TABLE sdk_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_session_id TEXT NOT NULL,
    memory_session_id TEXT UNIQUE,
    project TEXT NOT NULL,
    platform_source TEXT NOT NULL DEFAULT 'claude',
    user_prompt TEXT,
    started_at TEXT NOT NULL,
    started_at_epoch INTEGER NOT NULL,
    completed_at TEXT,
    completed_at_epoch INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    worker_port INTEGER,
    prompt_counter INTEGER DEFAULT 0,
    custom_title TEXT
);
CREATE UNIQUE INDEX ux_sdk_sessions_platform_content ON sdk_sessions(platform_source, content_session_id);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    text TEXT,
    type TEXT NOT NULL,
    title TEXT,
    subtitle TEXT,
    facts TEXT,
    narrative TEXT,
    concepts TEXT,
    files_read TEXT,
    files_modified TEXT,
    prompt_number INTEGER,
    discovery_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    content_hash TEXT,
    generated_by_model TEXT,
    relevance_count INTEGER DEFAULT 0,
    merged_into_project TEXT,
    agent_type TEXT,
    agent_id TEXT,
    metadata TEXT,
    synced_at INTEGER,
    origin_device_id TEXT,
    origin_local_id TEXT,
    sync_rev TEXT NOT NULL DEFAULT '1',
    FOREIGN KEY(memory_session_id) REFERENCES sdk_sessions(memory_session_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE UNIQUE INDEX ux_observations_session_hash ON observations(memory_session_id, content_hash);
CREATE UNIQUE INDEX ux_observations_origin ON observations(origin_device_id, origin_local_id) WHERE origin_device_id IS NOT NULL;
CREATE VIRTUAL TABLE observations_fts USING fts5(title, subtitle, narrative, text, facts, concepts, content='observations', content_rowid='id');
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    request TEXT,
    investigated TEXT,
    learned TEXT,
    completed TEXT,
    next_steps TEXT,
    files_read TEXT,
    files_edited TEXT,
    notes TEXT,
    prompt_number INTEGER,
    discovery_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    merged_into_project TEXT,
    synced_at INTEGER,
    origin_device_id TEXT,
    origin_local_id TEXT,
    sync_rev TEXT NOT NULL DEFAULT '1',
    FOREIGN KEY(memory_session_id) REFERENCES sdk_sessions(memory_session_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE UNIQUE INDEX ux_session_summaries_origin ON session_summaries(origin_device_id, origin_local_id) WHERE origin_device_id IS NOT NULL;
CREATE VIRTUAL TABLE session_summaries_fts USING fts5(request, investigated, learned, completed, next_steps, notes, content='session_summaries', content_rowid='id');
CREATE TABLE user_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_db_id INTEGER,
    content_session_id TEXT NOT NULL,
    prompt_number INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    synced_at INTEGER,
    origin_device_id TEXT,
    origin_local_id TEXT,
    sync_rev TEXT NOT NULL DEFAULT '1',
    FOREIGN KEY(session_db_id) REFERENCES sdk_sessions(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_user_prompts_origin ON user_prompts(origin_device_id, origin_local_id) WHERE origin_device_id IS NOT NULL;
CREATE VIRTUAL TABLE user_prompts_fts USING fts5(prompt_text, content='user_prompts', content_rowid='id');
CREATE TABLE pending_messages (
    id INTEGER PRIMARY KEY,
    session_db_id INTEGER NOT NULL,
    content_session_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    FOREIGN KEY(session_db_id) REFERENCES sdk_sessions(id) ON DELETE CASCADE
);
"""


def make_database(path, sessions, observations=(), summaries=(), prompts=()):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.executemany(
        """INSERT INTO sdk_sessions
        (content_session_id, memory_session_id, project, platform_source, user_prompt,
         started_at, started_at_epoch, completed_at, completed_at_epoch, status,
         worker_port, prompt_counter, custom_title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        sessions,
    )
    ids = dict(connection.execute("SELECT content_session_id, id FROM sdk_sessions"))
    connection.executemany(
        """INSERT INTO observations
        (memory_session_id, project, text, type, title, narrative, prompt_number,
         created_at, created_at_epoch, content_hash, origin_device_id, origin_local_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        observations,
    )
    connection.executemany(
        """INSERT INTO session_summaries
        (memory_session_id, project, request, learned, prompt_number, created_at,
         created_at_epoch, origin_device_id, origin_local_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        summaries,
    )
    connection.executemany(
        """INSERT INTO user_prompts
        (session_db_id, content_session_id, prompt_number, prompt_text, created_at,
         created_at_epoch, origin_device_id, origin_local_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(ids[content], content, number, text, created, epoch, device, local) for content, number, text, created, epoch, device, local in prompts],
    )
    for table in ("observations", "session_summaries", "user_prompts"):
        connection.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES('rebuild')")
    connection.commit()
    connection.close()


def session(content, memory, *, status="completed", start=100, end=200, prompt="prompt", title="title"):
    return (
        content, memory, "project-one", "claude", prompt, "start", start,
        "end" if end else None, end, status, 37702, 2, title,
    )


class FleetMemoryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_merge_is_a_deduplicated_union_and_is_idempotent(self):
        server = self.root / "server.db"
        client = self.root / "client.db"
        merged = self.root / "merged.db"
        merged_again = self.root / "merged-again.db"
        make_database(
            server,
            [session("shared-content", "server-memory")],
            [("server-memory", "project-one", "same", "discovery", "same", "same", 1, "time", 110, "same-hash", None, None)],
        )
        make_database(
            client,
            [
                session("shared-content", "client-memory", start=90, end=220, prompt="", title="client title"),
                session("client-only", "client-only-memory"),
            ],
            [
                ("client-memory", "project-one", "same", "discovery", "same", "same", 1, "time", 110, "same-hash", None, None),
                ("client-memory", "project-one", "unique", "decision", "unique", "mac-only-marker", 2, "later", 120, None, None, None),
            ],
            [("client-memory", "project-one", "request", "learned-marker", 2, "later", 121, None, None)],
            [("shared-content", 2, "prompt-marker", "later", 122, None, None)],
        )

        report = db.merge_databases(server, client, merged)

        self.assertEqual(report.inserted_sessions, 1)
        self.assertEqual(report.inserted_observations, 1)
        self.assertEqual(report.inserted_summaries, 1)
        self.assertEqual(report.inserted_prompts, 1)
        checked = db.check_database(merged)
        self.assertEqual(checked.quick_check, "ok")
        self.assertEqual(checked.foreign_key_errors, ())
        connection = sqlite3.connect(merged)
        self.assertEqual(connection.execute("SELECT count(*) FROM observations_fts WHERE observations_fts MATCH 'mac' ").fetchone()[0], 1)
        shared = connection.execute(
            "SELECT memory_session_id, started_at_epoch, completed_at_epoch FROM sdk_sessions WHERE content_session_id='shared-content'"
        ).fetchone()
        connection.close()
        self.assertEqual(shared, ("server-memory", 90, 220))

        second = db.merge_databases(merged, client, merged_again)
        self.assertEqual(second.total_inserted, 0)

    def test_snapshot_is_private_verified_and_never_overwrites(self):
        source = self.root / "source.db"
        destination = self.root / "backup.db"
        make_database(source, [session("one", "memory-one")])

        report = db.snapshot_database(source, destination)

        self.assertEqual(report.quick_check, "ok")
        self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(db.FleetMemoryDatabaseError, "exists"):
            db.snapshot_database(source, destination)

    def test_check_database_rejects_corruption_and_foreign_key_errors(self):
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(db.FleetMemoryDatabaseError):
            db.check_database(corrupt)

        broken = self.root / "broken.db"
        make_database(broken, [session("one", "memory-one")])
        connection = sqlite3.connect(broken)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO observations(memory_session_id, project, type, created_at, created_at_epoch) VALUES ('missing', 'p', 'x', 'now', 1)"
        )
        connection.commit()
        connection.close()

        report = db.check_database(broken)
        self.assertNotEqual(report.foreign_key_errors, ())

    def test_merge_rejects_one_memory_id_for_two_session_identities(self):
        server = self.root / "server.db"
        client = self.root / "client.db"
        make_database(server, [session("server-content", "same-memory")])
        make_database(client, [session("client-content", "same-memory")])

        with self.assertRaisesRegex(db.FleetMemoryDatabaseError, "memory session"):
            db.merge_databases(server, client, self.root / "merged.db")

    def test_backup_retention_removes_only_old_daily_files(self):
        backups = self.root / "backups"
        backups.mkdir()
        paths = [backups / f"daily-202608{day:02d}T000000Z.db" for day in (20, 21, 22)]
        for path in paths:
            path.write_bytes(b"daily")
        archive = backups / "pre-migration.db"
        archive.write_bytes(b"archive")

        removed = db.prune_daily_backups(backups, 2)

        self.assertEqual(removed, [paths[0]])
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertTrue(paths[2].exists())
        self.assertTrue(archive.exists())

    def test_merge_repairs_legacy_orphans_and_null_memory_sessions_on_copies(self):
        server = self.root / "server.db"
        client = self.root / "client.db"
        merged = self.root / "merged.db"
        make_database(server, [session("server", "server-memory")])
        make_database(client, [session("null-session", None)])
        connection = sqlite3.connect(client)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO observations
            (memory_session_id, project, text, type, title, narrative, created_at, created_at_epoch)
            VALUES ('orphan-memory', 'legacy-project', 'legacy', 'discovery', 'legacy',
                    'legacy-marker', 'legacy-time', 1777020030646)"""
        )
        connection.execute(
            """INSERT INTO session_summaries
            (memory_session_id, project, request, learned, created_at, created_at_epoch)
            VALUES ('summary-only-memory', 'legacy-project', 'old request', 'old lesson',
                    'legacy-time', 1777020030647)"""
        )
        null_id = connection.execute(
            "SELECT id FROM sdk_sessions WHERE content_session_id='null-session'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO user_prompts
            (session_db_id, content_session_id, prompt_number, prompt_text, created_at, created_at_epoch)
            VALUES (?, 'null-session', 1, 'null-session-prompt', 'legacy-time', 1777020030648)""",
            (null_id,),
        )
        connection.commit()
        connection.close()

        report = db.merge_databases(server, client, merged)

        self.assertGreaterEqual(report.inserted_sessions, 3)
        checked = db.check_database(merged)
        self.assertEqual(checked.foreign_key_errors, ())
        connection = sqlite3.connect(merged)
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM observations WHERE memory_session_id='orphan-memory' AND narrative='legacy-marker'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM sdk_sessions WHERE memory_session_id IN ('orphan-memory', 'summary-only-memory')"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM sdk_sessions WHERE content_session_id='null-session' AND memory_session_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_snapshot_preserves_a_legacy_database_for_recovery(self):
        source = self.root / "legacy.db"
        destination = self.root / "legacy-backup.db"
        make_database(source, [session("one", "memory-one")])
        connection = sqlite3.connect(source)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO observations(memory_session_id, project, type, created_at, created_at_epoch) VALUES ('orphan', 'p', 'x', 'now', 1)"
        )
        connection.commit()
        connection.close()

        report = db.snapshot_database(source, destination)

        self.assertEqual(len(report.foreign_key_errors), 1)
        self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
