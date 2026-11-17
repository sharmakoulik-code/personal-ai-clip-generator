from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import database


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="Personal Clip Generator API", version="0.1.0", lifespan=lifespan)


class ProjectCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class Project(BaseModel):
    id: int
    name: str
    created_at: str


@app.get("/api/health")
def health():
    # Check the database, not just the web server.
    with database.connect() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": "connected"}


@app.get("/api/projects", response_model=list[Project])
def list_projects():
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, name, created_at FROM projects ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/projects", response_model=Project, status_code=201)
def create_project(project: ProjectCreate):
    with database.connect() as connection:
        cursor = connection.execute("INSERT INTO projects (name) VALUES (?)", (project.name,))
        row = connection.execute(
            "SELECT id, name, created_at FROM projects WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: int):
    with database.connect() as connection:
        cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Project not found")
