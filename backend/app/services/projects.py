"""SQLite operations for the project library."""
from .. import database
from . import media


def check_database() -> None:
    with database.connect() as connection:
        connection.execute("SELECT id FROM projects LIMIT 1").fetchone()


def list_projects() -> list[dict]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, name, created_at FROM projects ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def create_project(name: str) -> dict:
    with database.connect() as connection:
        cursor = connection.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        row = connection.execute(
            "SELECT id, name, created_at FROM projects WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


def delete_project(project_id: int) -> bool:
    with database.connect() as connection:
        # Lock writes while checking active jobs and removing their files.
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            return False
        active = connection.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('pending','uploading','retrieving metadata','downloading','validating')", (project_id,)).fetchone()
        if active:
            raise ValueError('Cancel active imports before deleting this project.')
        media.remove_project_files(project_id)
        connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return True
