import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from kefu import configure_asyncio
from kefu.storage.schema import metadata

configure_asyncio()


def migrate(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    url = os.environ["MIGRATION_DATABASE_URL"]
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


asyncio.run(main())
