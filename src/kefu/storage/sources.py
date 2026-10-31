"""Revalidate each recorded answer's own sources before displaying or reusing it."""

import copy

import sqlalchemy as sa

from kefu.storage import schema as s

WITHDRAWN_NOTICE = "这条回复所依据的资料已撤回或失效，请重新核对当前资料。"


def source_key(source):
    return tuple(str(source.get(key) or "") for key in ("document_id", "version", "chunk_id"))


async def invalid_source_keys(conn, sources):
    keys = {source_key(source) for source in sources}
    if not keys:
        return set()
    document_ids = {document + "@" + version for document, version, _chunk in keys}
    chunk_ids = {document + "@" + version + ":" + chunk for document, version, chunk in keys}
    documents = {
        row["id"]: row["data"]
        for row in (
            await conn.execute(sa.select(s.documents).where(s.documents.c.id.in_(document_ids)))
        ).mappings()
    }
    chunks = {
        row["id"]: row["data"]
        for row in (
            await conn.execute(sa.select(s.chunks).where(s.chunks.c.id.in_(chunk_ids)))
        ).mappings()
    }
    invalid = set()
    for key in keys:
        document_id, version, chunk_id = key
        document_key = document_id + "@" + version
        document = documents.get(document_key, {})
        chunk = chunks.get(document_key + ":" + chunk_id)
        embedded = {item["id"]: item for item in document.get("chunks", [])}
        if (
            not all(key)
            or document.get("status") != "published"
            or document.get("version") != version
            or not chunk
            or chunk.get("status") in {"withdrawn", "deleted"}
            or chunk_id not in embedded
        ):
            invalid.add(key)
            continue
        # Parent safety sections are part of a chunk's provenance.
        current, seen = chunk_id, set()
        while current:
            parent = embedded.get(current)
            if current in seen or not parent or parent.get("status") in {"withdrawn", "deleted"}:
                invalid.add(key)
                break
            seen.add(current)
            current = parent.get("parent_id")
    return invalid


async def sanitize_messages(conn, events):
    """Keep durable history; withhold invalid source content in both UI and model views."""
    copied = copy.deepcopy([dict(event) for event in events])
    sources = [
        source
        for event in copied
        if event["payload"].get("role") == "assistant"
        for source in event["payload"].get("source_versions", [])
    ]
    invalid = await invalid_source_keys(conn, sources)
    for event in copied:
        payload = event["payload"]
        if payload.get("role") == "assistant" and (
            payload.get("source_invalidated")
            or any(source_key(source) in invalid for source in payload.get("source_versions", []))
        ):
            payload.update(text=WITHDRAWN_NOTICE, evidence_refs=[], source_invalidated=True)
    return copied
