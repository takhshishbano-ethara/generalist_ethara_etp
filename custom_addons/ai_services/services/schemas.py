from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProjectCategory(str, Enum):
    STEM = "STEM"
    NON_STEM = "Non-STEM"
    TECHNICAL = "Technical"


class ProjectType(str, Enum):
    SINGLE_TURN = "Single Turn"
    MULTI_TURN = "Multi Turn"


class ProjectDetails(BaseModel):
    client_project_name: str = Field(..., min_length=1)
    internal_project_name: str = Field(..., min_length=1)
    client_name: str = Field(..., min_length=1)
    internal_client_name: str = Field(..., min_length=1)
    project_category: ProjectCategory
    project_type: ProjectType
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    sample_task_number: int = Field(..., gt=0)
    description: str = Field(..., min_length=1)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_date_string(cls, v):
        if v is None or v == "null" or v == "":
            return None
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            return date.fromisoformat(v)
        return v

    @field_validator("sample_task_number", mode="before")
    @classmethod
    def coerce_to_int(cls, v):
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return v


class ExtractionResponse(BaseModel):
    success: bool = True
    filename: str
    model_name: str = "Kimi K2.5"
    project_details: ProjectDetails
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
