#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from modules.vpn_users.models.user import VPNUserStatus


class VPNUserCreate(BaseModel):
    username: str
    email: str
    description: str | None = None


class VPNUserUpdate(BaseModel):
    username: str | None = None
    email: str | None = None
    description: str | None = None


class VPNUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    email: str
    status: VPNUserStatus
    description: str | None = None
    created_at: datetime
