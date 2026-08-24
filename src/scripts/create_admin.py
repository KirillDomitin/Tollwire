#!/usr/bin/python
# -*- coding: utf-8 -*-
import asyncio

from core.database.postgres import session_factory
from core.security import hash_password
from modules.auth.models.admin import Admin

admin = Admin(email="a@a.a", password_hash=hash_password("1234"))


async def main() -> None:
    async with session_factory() as session:
        session.add(admin)
        await session.commit()


asyncio.run(main())
