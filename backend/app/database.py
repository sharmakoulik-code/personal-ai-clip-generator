"""Small, local SQLite persistence layer for the Phase 1 project library."""
import os
from contextlib import contextmanager
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "clips.sqlite3"


def database_path() -> Path:
    return Path(os.environ.get("CLIP_DB_PATH", str(DEFAULT_DB))).expanduser().resolve()


@contextmanager
def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize() -> None:
    with connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL, source_id TEXT, title TEXT NOT NULL,
                path TEXT NOT NULL, thumbnail_path TEXT,
                duration REAL, width INTEGER, height INTEGER, frame_rate TEXT,
                video_codec TEXT, audio_codec TEXT, has_audio INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, source TEXT NOT NULL, title TEXT,
                quality TEXT, status TEXT NOT NULL, progress REAL, speed REAL,
                error TEXT, temp_path TEXT, video_id TEXT REFERENCES videos(id) ON DELETE SET NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                completed_at TEXT
            )
        """)
