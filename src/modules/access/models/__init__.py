#!/usr/bin/python
# -*- coding: utf-8 -*-
import enum


class AccessGrantStatus(enum.Enum):
    ACTIVE = "active"
    PENDING = "pending"
    EXPIRED = "expired"
    REVOKED = "revoked"
