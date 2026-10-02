#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from modules.jobs.models.job import JobStatus, JobType


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    type: JobType
    status: JobStatus
    server_id: UUID | None
    access_grant_id: UUID | None = None
    admin_id: UUID | None = None
    progress: int
    message: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
