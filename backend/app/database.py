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
        video_columns = {row['name'] for row in connection.execute('PRAGMA table_info(videos)')}
        if 'preflight_json' not in video_columns:
            connection.execute('ALTER TABLE videos ADD COLUMN preflight_json TEXT')

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
        clip_columns = {row['name'] for row in connection.execute('PRAGMA table_info(candidate_clips)')}
        for name in ('original_start','original_end'):
            if name not in clip_columns: connection.execute(f'ALTER TABLE candidate_clips ADD COLUMN {name} REAL')
        connection.execute("""
            CREATE TABLE IF NOT EXISTS clip_metadata (
                clip_id TEXT PRIMARY KEY REFERENCES candidate_clips(id) ON DELETE CASCADE,
                titles_json TEXT NOT NULL, description TEXT NOT NULL,
                hashtags_json TEXT NOT NULL, provider TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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

        if 'selection_mode' not in columns:
            connection.execute("ALTER TABLE analysis_jobs ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'local'")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS renders (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                clip_id TEXT NOT NULL, clip_title TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                layout TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                settings_json TEXT NOT NULL, tracking_status TEXT,
                status TEXT NOT NULL, stage TEXT NOT NULL, progress REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
                path TEXT, thumbnail_path TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                completed_at TEXT
            )
        """)
        render_columns = {row['name'] for row in connection.execute('PRAGMA table_info(renders)')}
        if 'info_json' not in render_columns:
            connection.execute('ALTER TABLE renders ADD COLUMN info_json TEXT')
        if 'caption_words_json' not in render_columns:
            connection.execute('ALTER TABLE renders ADD COLUMN caption_words_json TEXT')
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_render_per_clip ON renders(clip_id) WHERE status IN ('pending','running')")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS face_track_cache (
                key TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
