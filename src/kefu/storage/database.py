from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from kefu.settings import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engines = {
            name: create_async_engine(dsn.get_secret_value(), pool_pre_ping=True, pool_size=5)
            for name, dsn in (
                ("exec", settings.database_url),
                ("read", settings.read_database_url),
                ("work", settings.work_database_url),
            )
        }

    @asynccontextmanager
    async def transaction(self, principal_id: str = "", role: str = "exec"):
        async with self.engines[role].begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.principal_id', :id, true)"), {"id": principal_id}
            )
            yield connection

    async def close(self):
        for engine in self.engines.values():
            await engine.dispose()
