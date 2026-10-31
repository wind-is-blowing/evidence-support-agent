import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()


def table(name, *cols, **kw):
    return sa.Table(name, metadata, *cols, **kw)


def idcol(name="id", **kw):
    return sa.Column(name, sa.String(128), **kw)


def jsoncol(name="data"):
    return sa.Column(name, JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


def timestamp(name="created_at"):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


principals = table(
    "principals",
    idcol(primary_key=True),
    sa.Column("username", sa.Text, unique=True),
    sa.Column("password_hash", sa.Text),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    timestamp(),
    sa.CheckConstraint("role IN ('guest','consumer','staff')"),
)
sessions = table(
    "sessions",
    idcol(primary_key=True),
    idcol("principal_id", nullable=False),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    sa.Column("csrf_token", sa.Text, nullable=False),
    timestamp("expires_at"),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    timestamp(),
    sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
)
products = table("products", idcol(primary_key=True), jsoncol())
devices = table(
    "devices",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    idcol("product_id", nullable=False),
    jsoncol(),
)
purchases = table(
    "purchases",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    idcol("product_id", nullable=False),
    jsoncol(),
)
dealers = table("dealers", idcol(primary_key=True), jsoncol())
tasks = table(
    "tasks",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    idcol("assigned_staff_id"),
    sa.Column("create_key", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False, server_default="READY"),
    sa.Column("wait_reason", sa.Text),
    sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    sa.Column("input_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_epoch", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_owner", sa.Text),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("token_limit", sa.BigInteger, nullable=False, server_default="1000000"),
    sa.Column("tokens_used", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("tokens_reserved", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("last_event_seq", sa.BigInteger, nullable=False, server_default="0"),
    timestamp(),
    timestamp("updated_at"),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("owner_id", "create_key"),
    sa.ForeignKeyConstraint(["owner_id"], ["principals.id"]),
    sa.ForeignKeyConstraint(["assigned_staff_id"], ["principals.id"]),
    sa.CheckConstraint("state IN ('READY','RUNNING','WAITING','PAUSED','COMPLETED','CANCELLED')"),
)
task_work = table(
    "task_work",
    idcol("task_id", primary_key=True),
    jsoncol(),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
)
events = table(
    "events",
    idcol(primary_key=True),
    idcol("task_id", nullable=False),
    sa.Column("seq", sa.BigInteger, nullable=False),
    idcol("actor_id", nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    jsoncol("payload"),
    sa.Column("visibility", sa.Text, nullable=False, server_default="public"),
    timestamp(),
    sa.UniqueConstraint("task_id", "seq"),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
requests = table(
    "requests",
    idcol("principal_id", primary_key=True),
    idcol("task_id", primary_key=True),
    sa.Column("client_event_id", sa.String(128), primary_key=True),
    sa.Column("body_hash", sa.Text, nullable=False),
    jsoncol("result"),
)
actions = table(
    "actions",
    idcol(primary_key=True),
    idcol("task_id", nullable=False),
    idcol("draft_id", nullable=False),
    sa.Column("draft_version", sa.Integer, nullable=False),
    idcol("operation_id", nullable=False, unique=True),
    idcol("confirmation_event_id", nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    jsoncol("draft"),
    jsoncol("receipt"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    timestamp(),
    sa.UniqueConstraint("task_id", "draft_id", "draft_version"),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
jobs = table(
    "jobs",
    idcol(primary_key=True),
    idcol("task_id", nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("input_version", sa.Integer, nullable=False),
    sa.Column("target_id", sa.String(128), nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="queued"),
    timestamp("available_at"),
    sa.Column("lease_owner", sa.Text),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    jsoncol(),
    timestamp(),
    sa.UniqueConstraint("kind", "target_id", "input_version"),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
model_calls = table(
    "model_calls",
    idcol(primary_key=True),
    idcol("task_id", nullable=False),
    sa.Column("day", sa.Text, nullable=False),
    sa.Column("reserved", sa.BigInteger, nullable=False),
    sa.Column("used", sa.BigInteger),
    sa.Column("status", sa.Text, nullable=False),
    jsoncol(),
    timestamp(),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
budget_buckets = table(
    "budget_buckets",
    sa.Column("day", sa.Text, primary_key=True),
    sa.Column("used", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reserved", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("limit", sa.BigInteger, nullable=False),
)
artifacts = table(
    "artifacts",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    idcol("task_id", nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    jsoncol(),
    timestamp(),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
documents = table(
    "documents", idcol(primary_key=True), sa.Column("version", sa.Text, nullable=False), jsoncol()
)
document_scopes = table(
    "document_scopes",
    idcol(primary_key=True),
    idcol("document_id", nullable=False),
    idcol("product_id", nullable=False),
    jsoncol(),
)
chunks = table(
    "chunks",
    idcol(primary_key=True),
    idcol("document_id", nullable=False),
    sa.Column("embedding", Vector(1024)),
    sa.Column("embedding_version", sa.Text),
    jsoncol(),
)
approved_steps = table(
    "approved_steps", idcol(primary_key=True), idcol("document_id", nullable=False), jsoncol()
)
memories = table(
    "memories",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    idcol("device_id"),
    sa.Column("version", sa.Integer, nullable=False),
    jsoncol(),
)
memory_exclusions = table(
    "memory_exclusions",
    idcol(primary_key=True),
    idcol("owner_id", nullable=False),
    jsoncol(),
    timestamp(),
)
source_states = table(
    "source_states",
    idcol("owner_id", primary_key=True),
    idcol("source_id", primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("excluded", sa.Boolean, nullable=False, server_default=sa.false()),
)
tickets = table(
    "tickets",
    idcol(primary_key=True),
    idcol("task_id", nullable=False),
    idcol("operation_id", nullable=False, unique=True),
    jsoncol(),
    timestamp(),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
)
