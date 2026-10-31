import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine

from kefu.plugins.knowledge import _without_generated_embeddings
from kefu.storage import schema as s
from kefu.storage.repository import DomainError


class PluginRepository:
    """Knowledge and context adapters; import credentials never enter runtime tools."""

    def __init__(self, db, import_url=None):
        self.db = db
        self.import_engine = create_async_engine(import_url) if import_url else None

    async def list_products(self):
        async with self.db.transaction(role="read") as conn:
            return list((await conn.execute(sa.select(s.products.c.data))).scalars())

    async def list_documents(self, product_id=None, kind=None):
        query = sa.select(s.documents.c.data)
        if product_id:
            query = query.where(s.documents.c.data["product_ids"].contains([product_id]))
        if kind:
            query = query.where(s.documents.c.data["kind"].astext == kind)
        async with self.db.transaction(role="read") as conn:
            return list((await conn.execute(query)).scalars())

    async def semantic_search(self, vector, candidate_refs, model_id, limit):
        """Exact pgvector nearest neighbors within server-validated product/scope IDs."""
        import math

        if len(vector) != 1024 or not all(math.isfinite(value) for value in vector):
            raise ValueError("pgvector requires a finite 1024-dimensional embedding")
        if not 1 <= limit <= 50:
            raise ValueError("Semantic retrieval limit must be 1 through 50")
        keys = [
            f"{ref['document_id']}@{ref['version']}:{ref['chunk_id']}" for ref in candidate_refs
        ]
        if not keys:
            return []
        distance = s.chunks.c.embedding.cosine_distance(vector).label("distance")
        query = (
            sa.select(
                s.chunks.c.data.label("chunk"), s.documents.c.data.label("document"), distance
            )
            .join(s.documents, s.documents.c.id == s.chunks.c.document_id)
            .where(
                s.chunks.c.id.in_(keys),
                s.chunks.c.embedding_version == model_id,
                s.chunks.c.embedding.is_not(None),
                s.documents.c.data["status"].astext == "published",
            )
            .order_by(distance, s.chunks.c.id)
            .limit(limit)
        )
        async with self.db.transaction(role="read") as conn:
            rows = (await conn.execute(query)).mappings().all()
            return [
                {
                    "document_id": row["document"]["id"],
                    "version": row["document"]["version"],
                    "chunk_id": row["chunk"]["id"],
                    "distance": float(row["distance"]),
                }
                for row in rows
            ]

    async def publish(self, products, documents):
        if not self.import_engine:
            raise PermissionError("knowledge publication requires offline import credentials")
        async with self.import_engine.begin() as conn:
            for product in products:
                await conn.execute(
                    insert(s.products)
                    .values(id=product["id"], data=product)
                    .on_conflict_do_update(index_elements=["id"], set_={"data": product})
                )
            for document in documents:
                key = document["id"] + "@" + document["version"]
                stored_document = _without_generated_embeddings(document)
                old = (
                    await conn.execute(sa.select(s.documents.c.data).where(s.documents.c.id == key))
                ).scalar_one_or_none()
                if old:
                    if old != stored_document:
                        raise ValueError("published document version is immutable")
                    for chunk in document["chunks"]:
                        if "embedding" not in chunk:
                            continue
                        chunk_key = f"{key}:{chunk['id']}"
                        updated = await conn.execute(
                            s.chunks.update()
                            .where(
                                s.chunks.c.id == chunk_key,
                                s.chunks.c.document_id == key,
                            )
                            .values(
                                embedding=chunk["embedding"],
                                embedding_version=chunk.get("embedding_model"),
                            )
                        )
                        if updated.rowcount != 1:
                            raise ValueError("published document chunks are immutable")
                    continue
                await conn.execute(
                    s.documents.insert().values(
                        id=key, version=document["version"], data=stored_document
                    )
                )
                for index, product_id in enumerate(document["product_ids"]):
                    await conn.execute(
                        s.document_scopes.insert().values(
                            id=f"{key}:{index}",
                            document_id=key,
                            product_id=product_id,
                            data={
                                k: document.get(k)
                                for k in (
                                    "regions",
                                    "channels",
                                    "effective_from",
                                    "effective_until",
                                )
                            },
                        )
                    )
                for chunk in document["chunks"]:
                    chunk_key = f"{key}:{chunk['id']}"
                    stored_chunk = {
                        field: value
                        for field, value in chunk.items()
                        if field not in {"embedding", "embedding_model"}
                    }
                    await conn.execute(
                        s.chunks.insert().values(
                            id=chunk_key,
                            document_id=key,
                            data=stored_chunk,
                            embedding=chunk.get("embedding"),
                            embedding_version=chunk.get("embedding_model"),
                        )
                    )
                    for step in chunk.get("steps", []):
                        await conn.execute(
                            s.approved_steps.insert().values(
                                id=f"{key}:{step['step_id']}", document_id=key, data=step
                            )
                        )

    async def withdraw(self, document_id, version):
        if not self.import_engine:
            raise PermissionError("withdrawal requires import credentials")
        async with self.import_engine.begin() as conn:
            key = f"{document_id}@{version}"
            record = (
                await conn.execute(
                    sa.select(s.documents.c.data).where(s.documents.c.id == key).with_for_update()
                )
            ).scalar_one()
            record = {**record, "status": "withdrawn"}
            await conn.execute(
                s.documents.update().where(s.documents.c.id == key).values(data=record)
            )

    async def get_artifact(self, task_id, path, user_id):
        async with self.db.transaction(user_id, "read") as conn:
            return (
                await conn.execute(
                    sa.select(s.artifacts.c.data).where(
                        s.artifacts.c.task_id == task_id,
                        s.artifacts.c.owner_id == user_id,
                        s.artifacts.c.kind == "context",
                        s.artifacts.c.data["path"].astext == path,
                    )
                )
            ).scalar_one_or_none()

    async def put_artifact(self, record):
        async with self.db.transaction(record["user_id"]) as conn:
            task = (
                (
                    await conn.execute(
                        sa.select(s.tasks)
                        .where(s.tasks.c.id == record["task_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                not task
                or task["owner_id"] != record["user_id"]
                or task["input_version"] != record["task_version"]
                or task["state"] in ("COMPLETED", "CANCELLED")
            ):
                raise DomainError("STALE_CONTEXT", "任务已变化")
            await conn.execute(
                insert(s.artifacts)
                .values(
                    id=record["task_id"] + ":" + record["id"],
                    owner_id=record["user_id"],
                    task_id=record["task_id"],
                    kind="context",
                    data=record,
                )
                .on_conflict_do_nothing()
            )

    async def list_artifacts(self, task_id, user_id):
        async with self.db.transaction(user_id, "read") as conn:
            return list(
                (
                    await conn.execute(
                        sa.select(s.artifacts.c.data).where(
                            s.artifacts.c.task_id == task_id,
                            s.artifacts.c.owner_id == user_id,
                            s.artifacts.c.kind == "context",
                        )
                    )
                ).scalars()
            )

    async def invalidate_artifacts(self, task_id, user_id):
        async with self.db.transaction(user_id) as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(s.artifacts)
                        .where(
                            s.artifacts.c.task_id == task_id,
                            s.artifacts.c.owner_id == user_id,
                            s.artifacts.c.kind == "context",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await conn.execute(
                    s.artifacts.update()
                    .where(s.artifacts.c.id == row["id"])
                    .values(data={**row["data"], "status": "deleted", "preview": ""})
                )

    async def begin_cleanup(self, task_id, user_id, expected_task_status):
        """Fence terminal cleanup against reopening, publishing and unresolved effects."""
        async with self.db.transaction(user_id) as conn:
            task = (
                (
                    await conn.execute(
                        sa.select(s.tasks)
                        .where(s.tasks.c.id == task_id, s.tasks.c.owner_id == user_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                not task
                or task["state"] not in {"COMPLETED", "CANCELLED"}
                or task["state"] != expected_task_status
            ):
                return None
            unresolved = (
                await conn.execute(
                    sa.select(s.actions.c.id)
                    .where(
                        s.actions.c.task_id == task_id,
                        s.actions.c.status.in_(["confirmed", "submitting", "unknown"]),
                    )
                    .limit(1)
                )
            ).first()
            reserved_call = (
                await conn.execute(
                    sa.select(s.model_calls.c.id)
                    .where(s.model_calls.c.task_id == task_id, s.model_calls.c.status == "reserved")
                    .limit(1)
                )
            ).first()
            if unresolved or reserved_call:
                return None
            rows = (
                (
                    await conn.execute(
                        sa.select(s.artifacts)
                        .where(
                            s.artifacts.c.task_id == task_id,
                            s.artifacts.c.owner_id == user_id,
                            s.artifacts.c.kind == "context",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await conn.execute(
                    s.artifacts.update()
                    .where(s.artifacts.c.id == row["id"])
                    .values(data={**row["data"], "status": "deleted", "preview": ""})
                )
            return [dict(row["data"]) for row in rows]

    async def close(self):
        if self.import_engine:
            await self.import_engine.dispose()
