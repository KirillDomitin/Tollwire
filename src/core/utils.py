#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

from core.config import settings


def get_access_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_access_expire)


def get_refresh_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_refresh_expire)
