"""Request and response types for the local project library."""
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


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
