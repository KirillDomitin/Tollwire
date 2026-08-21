#!/usr/bin/python
# -*- coding: utf-8 -*-

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings

engine: AsyncEngine = create_async_engine(settings.database_url)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with session_factory() as session:
        yield session
