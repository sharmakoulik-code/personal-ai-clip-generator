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
        connection.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                model TEXT NOT NULL, language TEXT, duration REAL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS transcript_segments (
                video_id TEXT NOT NULL REFERENCES transcripts(video_id) ON DELETE CASCADE,
                position INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                text TEXT NOT NULL, words_json TEXT NOT NULL,
                PRIMARY KEY (video_id, position)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS candidate_clips (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start REAL NOT NULL, end REAL NOT NULL, title TEXT NOT NULL, hook TEXT NOT NULL,
                transcript TEXT NOT NULL, reason TEXT NOT NULL, score INTEGER NOT NULL,
                scores_json TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                model TEXT NOT NULL, min_duration INTEGER NOT NULL, max_duration INTEGER NOT NULL,
                force_transcribe INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, stage TEXT NOT NULL, progress REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                completed_at TEXT
            )
        """)
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_analysis_per_video
            ON analysis_jobs(video_id) WHERE status IN ('pending','running')
        """)

        # Existing Phase 3 databases predate optional Groq enhancements.
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(analysis_jobs)')}
        for name in ('groq_titles', 'gemini_titles', 'cloud_titles'):
            if name not in columns:
                connection.execute(f'ALTER TABLE analysis_jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
