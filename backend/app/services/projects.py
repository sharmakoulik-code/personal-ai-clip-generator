"""SQLite operations for the project library."""
from .. import database


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
        cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0
