"""Local-only project and health endpoints."""
from fastapi import APIRouter, HTTPException

from ..models import Project, ProjectCreate
from ..services import projects

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    projects.check_database()
    return {"status": "ok", "database": "connected"}


@router.get("/projects", response_model=list[Project])
def list_projects():
    return projects.list_projects()


@router.post("/projects", response_model=Project, status_code=201)
def create_project(project: ProjectCreate):
    return projects.create_project(project.name)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int):
    if not projects.delete_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
