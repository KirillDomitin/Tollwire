#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from modules.jobs.models.job import JobStatus, JobType


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    type: JobType
    status: JobStatus
    server_id: UUID | None
    progress: int
    message: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
