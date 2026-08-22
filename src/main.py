#!/usr/bin/python
# -*- coding: utf-8 -*-

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database.postgres import get_session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    print("Start!")
    yield
    print("Finish!")


app = FastAPI(title=settings.project_name, docs_url="/api/openapi", openapi_url="/api/openapi.json", lifespan=lifespan)


@app.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
