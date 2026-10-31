"""Initial schema, fixed role grants and row-level isolation."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

from kefu.storage.schema import metadata

revision = "0001"
down_revision = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata.create_all(op.get_bind())
    sql = (Path(__file__).parents[1] / "security.sql").read_text(encoding="utf-8")
    # psycopg supports multiple statements via the driver's simple query path.
    op.get_bind().execute(text(sql))


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup instead")
