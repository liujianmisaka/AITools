from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from multi_agent_v2.packages.persistence.schema import (
    CURRENT_SCHEMA_REVISION,
    DatabaseSchemaError,
)


class DatabaseManager:
    def __init__(self, database_url: SecretStr) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url.get_secret_value(),
            pool_pre_ping=True,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            try:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            except SQLAlchemyError as exc:
                raise DatabaseSchemaError("Control DB schema is not initialized") from exc

            revisions = tuple(str(value) for value in result.scalars())
            if revisions != (CURRENT_SCHEMA_REVISION,):
                raise DatabaseSchemaError("Control DB schema revision does not match application")

    async def close(self) -> None:
        await self._engine.dispose()


class DatabaseProbe:
    name = "postgresql"

    def __init__(self, database: DatabaseManager) -> None:
        self._database = database

    async def check(self) -> None:
        await self._database.check()
