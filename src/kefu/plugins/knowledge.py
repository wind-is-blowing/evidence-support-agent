"""Versioned, scope-constrained evidence retrieval; never an authority for execution."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class KnowledgeRepository(Protocol):
    async def list_products(self) -> list[dict[str, Any]]: ...

    async def list_documents(
        self, product_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def publish(
        self, products: list[dict[str, Any]], documents: list[dict[str, Any]]
    ) -> None:
        """Atomically publish an already validated import; same id/version is immutable."""
        ...

    async def withdraw(self, document_id: str, version: str) -> None: ...


class EmbeddingProvider(Protocol):
    model_id: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class SemanticKnowledgeRepository(Protocol):
    async def semantic_search(
        self, vector: list[float], candidate_refs: list[dict[str, str]], model_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Exact pgvector distance on already scoped candidate chunk IDs, rechecking publication."""
        ...


def result(
    status: str, data: dict[str, Any], refs: list[str] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {"status": status, "data": data, "evidence_refs": refs or [], "error": error}


def _product_id(product: str | dict[str, Any] | None) -> str | None:
    if isinstance(product, dict):
        return product.get("product_id") or product.get("id")
    return product


def _date(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _tokens(value: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", value.casefold()))
    for group in re.findall(r"[\u4e00-\u9fff]+", value):
        words.update(group[i : i + 2] for i in range(max(1, len(group) - 1)))
    return words


_ENGLISH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "device",
    "do",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "the",
    "to",
    "what",
    "why",
    "with",
}

_CHINESE_RETRIEVAL_STOPWORDS = {
    "一下",
    "不能",
    "什么",
    "可以",
    "帮我",
    "怎么",
    "情况",
    "故障",
    "是否",
    "能不",
    "设备",
    "请问",
    "这个",
    "那个",
    "问题",
}

_NEGATION_MARKERS = ("不是", "并非", "无关", "排除")
_CHINESE_WEAK_BIGRAM_CHARS = set("的了吗呢啊呀吧嘛我你他她它这那把给和或与及")

_QUERY_EXPANSIONS = {
    "图咋": "地图不显示 地图丢失",
    "图没": "地图不显示 地图丢失",
    "图不见": "地图不显示 地图丢失",
    "路线全忘": "地图丢失 不显示地图",
    "忘了路线": "地图丢失 不显示地图",
    "路痴": "地图丢失 路径 地图",
    "卷毛": "滚刷缠毛 毛发缠绕",
    "卡毛": "滚刷缠毛 毛发缠绕",
    "配不上": "配网失败 无法联网 Wi-Fi",
    "连不上网": "配网失败 无法联网 Wi-Fi",
    "自己连": "自动重连 自动连接",
    "连回手机": "自动重连 自动连接",
    "吸不动": "吸力减弱 不吸",
    "吸不出来": "吸力 漏气 不吸",
    "c 口": "USB-C 端口",
    "c口": "USB-C 端口",
    "多少瓦": "最大输出 功率 W",
}


def _retrieval_tokens(value: str) -> set[str]:
    tokens = _tokens(value) - _ENGLISH_STOPWORDS - _CHINESE_RETRIEVAL_STOPWORDS
    return {
        token
        for token in tokens
        if not (
            len(token) == 2
            and all("\u4e00" <= char <= "\u9fff" for char in token)
            and any(char in _CHINESE_WEAK_BIGRAM_CHARS for char in token)
        )
    }


def _phrase_is_negated(value: str, phrase: str) -> bool:
    """Return true only when a phrase is locally preceded by an explicit negation marker."""
    phrase_normal = _normal(phrase)
    if not phrase_normal:
        return False
    for clause in re.split(r"[，。；;,.!?！？]", value.casefold()):
        clause_normal = _normal(clause)
        start = clause_normal.find(phrase_normal)
        while start >= 0:
            prefix = clause_normal[max(0, start - 8) : start]
            if any(_normal(marker) in prefix for marker in _NEGATION_MARKERS):
                return True
            start = clause_normal.find(phrase_normal, start + 1)
    return False


def _negated_query_tokens(value: str) -> set[str]:
    """Remove explicitly rejected concepts without treating symptom words like '没有' as negation."""
    negated: set[str] = set()
    for clause in re.split(r"[，。；;,.!?！？]", value.casefold()):
        for marker in _NEGATION_MARKERS:
            if marker not in clause:
                continue
            tail = clause.split(marker, 1)[1]
            tail = re.split(r"而是|而非", tail, maxsplit=1)[0]
            negated.update(_retrieval_tokens(tail))
    return negated


def _semantic_query(value: str) -> str:
    """Keep the requested concept, not explicitly rejected concepts, for dense retrieval."""
    kept: list[str] = []
    for clause in re.split(r"[，。；;,.!?！？]", value):
        if not any(marker in clause.casefold() for marker in _NEGATION_MARKERS):
            kept.append(clause)
            continue
        for pivot in ("我问", "想问", "而是", "但要问", "只问"):
            if pivot in clause:
                requested = clause.split(pivot, 1)[1].strip()
                if requested:
                    kept.append(requested)
                break
    cleaned = " ".join(part.strip() for part in kept if part.strip())
    return cleaned or value


def _expanded_query(value: str) -> str:
    lowered = value.casefold()
    additions = [
        expanded
        for phrase, expanded in _QUERY_EXPANSIONS.items()
        if phrase in lowered and not _phrase_is_negated(lowered, phrase)
    ]
    return value if not additions else value + "\n" + " ".join(additions)


def _rank_key(document: dict[str, Any], chunk: dict[str, Any]) -> tuple[str, str, str]:
    return document["id"], document["version"], chunk["id"]


RankedRow = tuple[float, dict[str, Any], dict[str, Any], list[str]]


def _legacy_rank(
    query: str,
    eligible: list[tuple[dict[str, Any], dict[str, Any], list[str]]],
) -> list[RankedRow]:
    """Frozen pre-RRF ranking used only for same-corpus evaluation."""
    qtokens = _tokens(query)
    ranked = []
    for document, chunk, gaps in eligible:
        searchable = " ".join([chunk.get("title", ""), chunk["text"], *chunk.get("keywords", [])])
        score = float(len(qtokens & _tokens(searchable)))
        if any(code.casefold() in query.casefold() for code in chunk.get("error_codes", [])):
            score += 1000
        if score:
            ranked.append((score, document, chunk, gaps))
    return sorted(ranked, key=lambda row: (-row[0], _rank_key(row[1], row[2])))


def _rrf_rank(
    channels: list[tuple[str, list[RankedRow]]],
    *,
    rank_constant: int = 60,
) -> tuple[list[RankedRow], dict[str, int]]:
    """Fuse independently ranked channels by chunk identity; scores are not cross-calibrated."""
    scores: dict[tuple[str, str, str], float] = {}
    rows: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any], list[str]]] = {}
    counts: dict[str, int] = {}
    for name, channel in channels:
        ordered = sorted(channel, key=lambda row: (-row[0], _rank_key(row[1], row[2])))
        counts[name] = len(ordered)
        for rank, (_, document, chunk, gaps) in enumerate(ordered, start=1):
            key = _rank_key(document, chunk)
            rows[key] = (document, chunk, gaps)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
    ranked = [
        (score, *rows[key])
        for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]
    return ranked, counts


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise ValueError("Firmware version must be a dot-separated numeric version")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * max(0, 8 - len(parts))


def scope_check(record: dict[str, Any], scope: dict[str, Any]) -> tuple[bool, list[str]]:
    """Unknown restrictive conditions are missing evidence, never assumed satisfied."""
    missing: list[str] = []
    product_id = scope.get("product_id")
    if product_id not in record.get("product_ids", []):
        return False, []
    for plural, singular in (("regions", "region"), ("channels", "channel")):
        allowed = record.get(plural, [])
        if allowed:
            actual = scope.get(singular)
            if actual is None:
                missing.append(singular)
            elif actual not in allowed:
                return False, []
    firmware = scope.get("firmware")
    if record.get("firmware_min") or record.get("firmware_max"):
        if firmware is None:
            missing.append("firmware")
        else:
            try:
                current = _version(firmware)
                if record.get("firmware_min") and current < _version(record["firmware_min"]):
                    return False, []
                if record.get("firmware_max") and current > _version(record["firmware_max"]):
                    return False, []
            except ValueError:
                missing.append("firmware")
    event_at = _date(scope.get("at"))
    if record.get("effective_from") or record.get("effective_until"):
        if event_at is None:
            missing.append("event_or_purchase_date")
        else:
            effective_from = _date(record.get("effective_from"))
            effective_until = _date(record.get("effective_until"))
            if effective_from is not None and event_at < effective_from:
                return False, []
            if effective_until is not None and event_at >= effective_until:
                return False, []
    known = scope.get("conditions") or {}
    for key, expected in record.get("conditions", {}).items():
        if key not in known:
            missing.append(key)
        elif known[key] != expected:
            return False, []
    return True, missing


def section_scope(
    document: dict[str, Any], chunk: dict[str, Any], scope: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Every ancestor must be applicable before its text or derived step can be used."""
    applicable, missing = scope_check(document, scope)
    if not applicable:
        return False, missing
    lookup = {item["id"]: item for item in document["chunks"]}
    visited = set()
    current = chunk
    while current:
        if current["id"] in visited or current.get("status") in {"withdrawn", "invalidated"}:
            return False, ["invalid_parent_section"]
        visited.add(current["id"])
        valid, gaps = scope_check(
            {
                **document,
                **{
                    key: value
                    for key, value in current.items()
                    if key
                    in {
                        "product_ids",
                        "regions",
                        "channels",
                        "firmware_min",
                        "firmware_max",
                        "conditions",
                        "effective_from",
                        "effective_until",
                    }
                },
            },
            scope,
        )
        if not valid:
            return False, gaps
        missing.extend(gaps)
        parent_id = current.get("parent_id")
        if not parent_id:
            break
        if parent_id not in lookup:
            return False, ["parent_section"]
        current = lookup[parent_id]
    return True, sorted(set(missing))


def step_scope(
    document: dict[str, Any],
    chunk: dict[str, Any],
    step: dict[str, Any],
    scope: dict[str, Any],
) -> tuple[bool, list[str]]:
    valid, missing = section_scope(document, chunk, scope)
    if not valid:
        return False, missing
    fields = {
        "product_ids",
        "regions",
        "channels",
        "firmware_min",
        "firmware_max",
        "effective_from",
        "effective_until",
        "conditions",
    }
    narrowed = {
        **document,
        **{key: value for key, value in chunk.items() if key in fields},
        **{key: value for key, value in step.items() if key in fields},
    }
    for record in (
        narrowed,
        {"product_ids": narrowed["product_ids"], "conditions": step.get("preconditions", {})},
    ):
        applicable, gaps = scope_check(record, scope)
        if not applicable:
            return False, gaps
        missing.extend(gaps)
    return True, sorted(set(missing))


class InMemoryKnowledgeRepository:
    """Deterministic test repository; applications inject their durable repository."""

    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}

    async def list_products(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.products.values()))

    async def list_documents(
        self, product_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(
            [
                d
                for d in self.documents.values()
                if (product_id is None or product_id in d["product_ids"])
                and (kind is None or kind == d["kind"])
            ]
        )

    async def publish(
        self, products: list[dict[str, Any]], documents: list[dict[str, Any]]
    ) -> None:
        pending = copy.deepcopy(self.documents)
        for document in documents:
            key = (document["id"], document["version"])
            if key in pending and _without_generated_embeddings(pending[key]) != (
                _without_generated_embeddings(document)
            ):
                raise ValueError("Published document versions are immutable")
            pending[key] = copy.deepcopy(document)
        self.documents = pending
        self.products.update({p["id"]: copy.deepcopy(p) for p in products})

    async def withdraw(self, document_id: str, version: str) -> None:
        self.documents[(document_id, version)]["status"] = "withdrawn"


def _without_generated_embeddings(document: dict[str, Any]) -> dict[str, Any]:
    """Return immutable reviewed content without reproducible vector artifacts."""
    clean = copy.deepcopy(document)
    for chunk in clean.get("chunks", []):
        chunk.pop("embedding", None)
        chunk.pop("embedding_model", None)
    return clean


class SentenceTransformersEmbedding:
    """Optional local Qwen embedding. Install the retrieval extra before use.

    No synthetic vector fallback: load/inference failures are surfaced to the caller.
    """

    def __init__(
        self,
        model_path: str | Path,
        revision: str,
        model_id: str = "Qwen/Qwen3-Embedding-0.6B",
        device: str = "cpu",
    ) -> None:
        self.model_path = Path(model_path).resolve()
        if not self.model_path.is_dir() or not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
            raise ValueError(
                "Embedding requires a local model directory and pinned commit/hash revision"
            )
        self.model_id = f"{model_id}@{revision}"
        self.revision = revision
        self.device = device
        self._model: Any = None

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                str(self.model_path),
                device=self.device,
                trust_remote_code=False,
                local_files_only=True,
            )
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, texts)


class OllamaEmbedding:
    """Pinned, loopback-only Ollama embedding provider for the local 0.6B model."""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        revision: str,
        *,
        manifest_path: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama embedding endpoint must be a loopback HTTP URL")
        if parsed.path.rstrip("/") in {"/v1", "/api"}:
            raise ValueError("EMBEDDING_BASE_URL must be the Ollama root URL, without /v1 or /api")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", revision):
            raise ValueError("Ollama embedding requires the pinned 64-character manifest digest")
        if not model_name.strip():
            raise ValueError("Ollama embedding model name is required")
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name.strip()
        self.revision = revision.casefold()
        self.model_id = f"ollama/{self.model_name}@{self.revision}"
        self.transport = transport
        self.manifest_path = Path(manifest_path).resolve() if manifest_path else None
        if self.manifest_path and not self.manifest_path.is_file():
            raise ValueError("Configured Ollama manifest path does not exist")
        if (
            self.manifest_path
            and hashlib.sha256(self.manifest_path.read_bytes()).hexdigest() != self.revision
        ):
            raise ValueError("Configured Ollama manifest file does not match the pinned digest")
        self._identity_verified = False
        self._verified_manifest_stamp: tuple[int, int] | None = None
        self._identity_lock = asyncio.Lock()

    def _manifest_stamp(self) -> tuple[int, int] | None:
        if not self.manifest_path:
            return None
        stat = self.manifest_path.stat()
        return stat.st_mtime_ns, stat.st_size

    async def _assert_pinned_identity(self, client: httpx.AsyncClient) -> None:
        current_stamp = self._manifest_stamp()
        if self._identity_verified and current_stamp == self._verified_manifest_stamp:
            return
        async with self._identity_lock:
            current_stamp = self._manifest_stamp()
            if self._identity_verified and current_stamp == self._verified_manifest_stamp:
                return
            if (
                self.manifest_path
                and hashlib.sha256(self.manifest_path.read_bytes()).hexdigest() != self.revision
            ):
                self._identity_verified = False
                raise RuntimeError("Ollama manifest changed and no longer matches the pin")
            response = await client.get("/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
            match = next(
                (
                    item
                    for item in models
                    if item.get("name") == self.model_name or item.get("model") == self.model_name
                ),
                None,
            )
            if not match:
                raise RuntimeError(f"Pinned Ollama model is not installed: {self.model_name}")
            if str(match.get("digest", "")).casefold() != self.revision:
                raise RuntimeError("Installed Ollama embedding model digest does not match the pin")
            self._identity_verified = True
            self._verified_manifest_stamp = current_stamp

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=120,
            transport=self.transport,
        ) as client:
            await self._assert_pinned_identity(client)
            response = await client.post(
                "/api/embed", json={"model": self.model_name, "input": texts}
            )
            response.raise_for_status()
            vectors = response.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Ollama embedding count mismatch")
        if any(
            not isinstance(vector, list)
            or len(vector) != 1024
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise ValueError("Ollama must return finite 1024-dimensional embeddings")
        return vectors


def configured_embedding(settings: Any) -> EmbeddingProvider | None:
    """Build only an explicitly configured provider; never auto-download a model."""
    if settings.embedding_provider == "ollama":
        return OllamaEmbedding(
            settings.embedding_base_url,
            settings.embedding_model_name,
            settings.embedding_revision,
            manifest_path=settings.embedding_manifest_path,
        )
    if settings.embedding_provider == "sentence_transformers" or settings.embedding_model_path:
        if not settings.embedding_model_path:
            raise ValueError("sentence_transformers embedding requires EMBEDDING_MODEL_PATH")
        return SentenceTransformersEmbedding(
            str(settings.embedding_model_path), revision=settings.embedding_revision
        )
    return None


class KnowledgePlugin:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: EmbeddingProvider | None = None,
        allow_synthetic: bool = False,
        max_results: int = 6,
        dense_only_min_similarity: float = 0.48,
    ) -> None:
        self.repository = repository
        self.embedder = embedder
        self.allow_synthetic = allow_synthetic
        self.max_results = max_results
        self.dense_only_min_similarity = dense_only_min_similarity

    async def resolve_product(
        self, query: str, evidence_refs: list[str] | None = None
    ) -> dict[str, Any]:
        needle = _normal(query)
        if not needle:
            return result("needs_info", {"candidates": [], "missing_conditions": ["product_model"]})
        candidates = []
        for product in await self.repository.list_products():
            if product.get("synthetic", False) and not self.allow_synthetic:
                continue
            names = [
                product["id"],
                product["name"],
                product.get("model", ""),
                *product.get("aliases", []),
            ]
            matches = [
                n for n in names if _normal(n) and (_normal(n) in needle or needle in _normal(n))
            ]
            if matches:
                candidate = copy.deepcopy(product)
                candidate["matched_names"] = matches
                candidate["confirmation_required"] = True
                candidates.append(candidate)
        return result(
            "ok" if candidates else "not_found",
            {
                "candidates": candidates[:20],
                "confirmation_required": True,
                "coverage": "Only the imported catalogue is searched; absence does not imply unsupported product.",
            },
            evidence_refs,
        )

    async def import_manifest(self, manifest: str | Path) -> dict[str, int]:
        """Import local reviewed records; never follows URLs or executes document contents."""
        path = Path(manifest)
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        products = [r["product"] for r in records if r.get("record_type") == "product"]
        documents = [
            copy.deepcopy(r["document"]) for r in records if r.get("record_type") == "document"
        ]
        product_ids = {p["id"] for p in products} | {
            p["id"] for p in await self.repository.list_products()
        }
        for document in documents:
            self._validate_document(document, product_ids)
        if self.embedder and documents:
            chunks = [chunk for d in documents for chunk in d["chunks"]]
            vectors = await self.embedder.embed(
                [c.get("title", "") + "\n" + c["text"] for c in chunks]
            )
            if len(vectors) != len(chunks):
                raise ValueError("Embedding count mismatch")
            for chunk, vector in zip(chunks, vectors, strict=True):
                if len(vector) != 1024 or not all(math.isfinite(v) for v in vector):
                    raise ValueError("Invalid embedding vector")
                chunk["embedding"] = vector
                chunk["embedding_model"] = self.embedder.model_id
        await self.repository.publish(products, documents)
        return {
            "products": len(products),
            "documents": len(documents),
            "chunks": sum(len(d["chunks"]) for d in documents),
        }

    def _validate_document(self, document: dict[str, Any], product_ids: set[str]) -> None:
        required = {
            "id",
            "version",
            "source_url",
            "product_ids",
            "kind",
            "status",
            "reviewed_by",
            "license",
            "chunks",
        }
        if required - document.keys():
            raise ValueError(
                "Missing document metadata: " + ", ".join(sorted(required - document.keys()))
            )
        if not document["product_ids"] or not set(document["product_ids"]).issubset(product_ids):
            raise ValueError("Document has unknown or missing product scope")
        if document["status"] not in {"published", "candidate", "withdrawn"}:
            raise ValueError("Invalid publication state")
        if document["status"] == "published" and not document["reviewed_by"]:
            raise ValueError("Published knowledge needs an identified reviewer")
        if document["kind"] == "policy" and (
            not document.get("regions") or not document.get("effective_from")
        ):
            raise ValueError("Policies need region and independently verified effective date")
        if document["kind"] not in {"technical", "policy", "communication"}:
            raise ValueError("Unsupported knowledge kind")
        if document["kind"] == "communication" and (
            document.get("non_authoritative") is not True
            or document.get("allowed_use") != "tone_and_structure_only"
        ):
            raise ValueError("Communication guidance must be non-authoritative and tone-only")
        if document.get("synthetic") and not self.allow_synthetic:
            raise ValueError("Synthetic knowledge is disabled")
        chunk_ids = {c["id"] for c in document["chunks"]}
        if len(chunk_ids) != len(document["chunks"]):
            raise ValueError("Duplicate chunk identifiers")
        for chunk in document["chunks"]:
            if not chunk.get("text") or not chunk.get("location"):
                raise ValueError("A chunk needs original location and text")
            if document["kind"] == "communication":
                if chunk.get("steps") or chunk.get("claim_key") or chunk.get("claim_value"):
                    raise ValueError("Communication guidance cannot contain facts or steps")
                if not chunk.get("intents") or not chunk.get("emotions"):
                    raise ValueError("Communication guidance needs intent and emotion labels")
                forbidden_promises = re.compile(
                    r"(?:(?:我们|本店|客服)(?:将|会|可以)?(?:保证|承诺)|(?:一定|立刻|无条件))"
                    r"(?:退款|退货|保修|换新|赔偿|修好)"
                )
                if forbidden_promises.search(chunk["text"]):
                    raise ValueError("Communication guidance cannot promise an outcome")
            if chunk.get("product_ids") and not set(chunk["product_ids"]).issubset(
                document["product_ids"]
            ):
                raise ValueError("A chunk cannot broaden document product scope")
            if chunk.get("parent_id") and chunk["parent_id"] not in chunk_ids:
                raise ValueError("Missing parent section")
            chunk["content_hash"] = hashlib.sha256(chunk["text"].encode()).hexdigest()
        canonical = json.dumps(
            document["chunks"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        actual = hashlib.sha256(canonical.encode()).hexdigest()
        if document.get("content_hash") and document["content_hash"] != actual:
            raise ValueError("Document content hash mismatch")
        document["content_hash"] = actual

    async def search(
        self,
        query: str,
        kind: str = "technical",
        product: str | dict[str, Any] | None = None,
        region: str | None = None,
        channel: str | None = None,
        firmware: str | None = None,
        at: str | None = None,
        conditions: dict[str, Any] | None = None,
        retrieval_strategy: str = "rrf",
    ) -> dict[str, Any]:
        product_id = _product_id(product)
        if not product_id:
            return result(
                "needs_info",
                {"evidence": [], "steps": [], "missing_conditions": ["confirmed_product"]},
            )
        scope = {
            "product_id": product_id,
            "region": region,
            "channel": channel,
            "firmware": firmware,
            "at": at,
            "conditions": conditions,
        }
        if retrieval_strategy not in {"rrf", "legacy"}:
            raise ValueError("retrieval_strategy must be rrf or legacy")
        products = await self.repository.list_products()
        product_record = next((item for item in products if item.get("id") == product_id), {})
        eligible: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
        missing: set[str] = set()
        for document in await self.repository.list_documents(product_id, kind):
            if document["status"] != "published" or (
                document.get("synthetic") and not self.allow_synthetic
            ):
                continue
            matches, gaps = scope_check(document, scope)
            if not matches:
                continue
            for chunk in document["chunks"]:
                narrowed = {
                    **document,
                    **{
                        k: v
                        for k, v in chunk.items()
                        if k
                        in (
                            "product_ids",
                            "regions",
                            "channels",
                            "firmware_min",
                            "firmware_max",
                            "conditions",
                        )
                    },
                }
                valid, chunk_gaps = scope_check(narrowed, scope)
                ancestor_valid, ancestor_gaps = section_scope(document, chunk, scope)
                valid = valid and ancestor_valid
                chunk_gaps.extend(ancestor_gaps)
                if valid:
                    eligible.append((document, chunk, sorted(set(gaps + chunk_gaps))))
        query_codes = set(
            re.findall(r"(?<![A-Z0-9])E(?:B)?[0-9][0-9A-Z](?![A-Z0-9])", query.upper())
        )
        degraded = None
        channel_counts: dict[str, int] = {}
        if retrieval_strategy == "legacy":
            ranked = _legacy_rank(query, eligible)
            retrieval_mode = "legacy_exact_keywords"
        else:
            expanded = _expanded_query(query)
            product_names = [
                product_record.get("id", ""),
                product_record.get("name", ""),
                product_record.get("model", ""),
                *product_record.get("aliases", []),
            ]
            product_tokens = set().union(*(_retrieval_tokens(name) for name in product_names))
            product_norms = {_normal(name) for name in product_names if _normal(name)}
            qtokens = _retrieval_tokens(expanded) - product_tokens - _negated_query_tokens(query)
            query_normal = _normal(expanded)
            exact: list[tuple[float, dict[str, Any], dict[str, Any], list[str]]] = []
            fields: list[tuple[float, dict[str, Any], dict[str, Any], list[str]]] = []
            bodies: list[tuple[float, dict[str, Any], dict[str, Any], list[str]]] = []
            for document, chunk, gaps in eligible:
                exact_score = 0.0
                chunk_codes = {code.upper() for code in chunk.get("error_codes", [])}
                exact_score += 1000.0 * len(query_codes & chunk_codes)
                for keyword in chunk.get("keywords", []):
                    normal_keyword = _normal(keyword)
                    keyword_tokens = _retrieval_tokens(keyword)
                    if (
                        len(normal_keyword) >= 2
                        and normal_keyword not in product_norms
                        and not (keyword_tokens and keyword_tokens <= product_tokens)
                        and normal_keyword in query_normal
                        and not _phrase_is_negated(query, keyword)
                    ):
                        exact_score += min(20.0, float(len(normal_keyword)))
                if exact_score:
                    exact.append((exact_score, document, chunk, gaps))

                field_text = " ".join([chunk.get("title", ""), *chunk.get("keywords", [])])
                field_score = float(len(qtokens & _retrieval_tokens(field_text)))
                if field_score:
                    fields.append((field_score, document, chunk, gaps))
                body_score = float(len(qtokens & _retrieval_tokens(chunk["text"])))
                # A single body-only bigram is too weak for a support answer. A candidate
                # already supported by an exact or metadata-field channel may retain it.
                if body_score >= 2 or (body_score and (exact_score or field_score)):
                    bodies.append((body_score, document, chunk, gaps))

            channels = [("exact", exact), ("title_keywords", fields), ("body", bodies)]
            retrieval_mode = "lexical_rrf"
            try:
                if self.embedder and eligible:
                    query_vectors = await self.embedder.embed([_semantic_query(query)])
                    vector = query_vectors[0]
                    if len(vector) != 1024 or not all(math.isfinite(value) for value in vector):
                        raise ValueError("Embedding query must be a finite 1024-dimensional vector")
                    semantic = []
                    lexical_keys = {
                        _rank_key(document, chunk)
                        for channel in (exact, fields, bodies)
                        for _, document, chunk, _ in channel
                    }
                    database_search = getattr(self.repository, "semantic_search", None)
                    vector_rows = (
                        eligible
                        if database_search is not None
                        else [
                            (document, chunk, gaps)
                            for document, chunk, gaps in eligible
                            if chunk.get("embedding")
                            and len(chunk["embedding"]) == len(vector)
                            and chunk.get("embedding_model") == self.embedder.model_id
                        ]
                    )
                    if database_search is not None and vector_rows:
                        allowed = {
                            _rank_key(document, chunk): (document, chunk, gaps)
                            for document, chunk, gaps in vector_rows
                        }
                        refs = [
                            {
                                "document_id": document["id"],
                                "version": document["version"],
                                "chunk_id": chunk["id"],
                            }
                            for document, chunk, _ in vector_rows
                        ]
                        nearest = await database_search(
                            vector, refs, self.embedder.model_id, min(50, len(refs))
                        )
                        for match in nearest:
                            key = (
                                match["document_id"],
                                match["version"],
                                match["chunk_id"],
                            )
                            similarity = 1.0 - match["distance"]
                            threshold = (
                                0.35 if key in lexical_keys else self.dense_only_min_similarity
                            )
                            if key in allowed and similarity >= threshold:
                                document, chunk, gaps = allowed[key]
                                semantic.append((similarity, document, chunk, gaps))
                    else:
                        for document, chunk, gaps in vector_rows:
                            other = chunk["embedding"]
                            cosine = sum(a * b for a, b in zip(vector, other, strict=True)) / max(
                                1e-12,
                                math.sqrt(sum(v * v for v in vector) * sum(v * v for v in other)),
                            )
                            key = _rank_key(document, chunk)
                            threshold = (
                                0.35 if key in lexical_keys else self.dense_only_min_similarity
                            )
                            if cosine >= threshold:
                                semantic.append((cosine, document, chunk, gaps))
                    channels.append(("dense", semantic))
                    retrieval_mode = "hybrid_rrf"
            except Exception as exc:  # noqa: BLE001 - optional provider failures retain precise retrieval
                degraded = (
                    "Embedding unavailable; lexical RRF remains available: " + type(exc).__name__
                )
            ranked, channel_counts = _rrf_rank(channels)
        current_versions = {
            (d["id"], d["version"]): d
            for d in await self.repository.list_documents(product_id, kind)
            if d["status"] == "published"
        }
        ranked = [
            row
            for row in ranked
            if (row[1]["id"], row[1]["version"]) in current_versions
            and current_versions[(row[1]["id"], row[1]["version"])].get("content_hash")
            == row[1].get("content_hash")
        ]
        evidence, steps = [], []
        seen_refs: set[str] = set()
        for _, document, chunk, gaps in ranked[: self.max_results]:
            parent = next(
                (c for c in document["chunks"] if c["id"] == chunk.get("parent_id")), None
            )
            if chunk.get("parent_id") and parent is None:
                gaps = [*gaps, "parent_section"]
            ref = f"knowledge:{document['id']}:{document['version']}:{chunk['id']}"
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            missing.update(gaps)
            entry = {
                "id": ref,
                "chunk_id": chunk["id"],
                "document_id": document["id"],
                "version": document["version"],
                "source_url": document["source_url"],
                "location": chunk["location"],
                "text": chunk["text"],
                "title": chunk.get("title", ""),
                "product_ids": document["product_ids"],
                "conditions": document.get("conditions", {}),
                "missing_conditions": gaps,
                "step_ids": [s["step_id"] for s in chunk.get("steps", [])],
                "synthetic": bool(document.get("synthetic")),
                "parent_context": parent["text"] if parent and not gaps else None,
                "warnings": chunk.get("warnings", []),
                "content_hash": chunk.get("content_hash"),
                "claim_key": chunk.get("claim_key"),
                "claim_value": chunk.get("claim_value"),
            }
            evidence.append(entry)
            exact_step_scope = not query_codes or bool(
                query_codes & set(chunk.get("error_codes", []))
            )
            query_terms = chunk.get("step_query_terms", [])
            matches_step_query = not query_terms or any(
                term.casefold() in query.casefold() for term in query_terms
            )
            if not gaps and exact_step_scope and matches_step_query:
                steps.extend(self._steps(document, chunk, scope, ref))
        conflicts = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in evidence:
            if entry.get("claim_key"):
                grouped.setdefault(entry["claim_key"], []).append(entry)
        for claim_key, entries in grouped.items():
            if len({json.dumps(e.get("claim_value"), sort_keys=True) for e in entries}) > 1:
                conflicts.append(
                    {"claim_key": claim_key, "evidence_ids": [e["id"] for e in entries]}
                )
        if conflicts:
            steps = []
        status = (
            "conflict"
            if conflicts
            else "ok"
            if evidence and not missing
            else "needs_info"
            if missing
            else "not_found"
        )
        return result(
            status,
            {
                "evidence": evidence,
                "steps": steps,
                "missing_conditions": sorted(missing),
                "conflicts": conflicts,
                "retrieval_mode": retrieval_mode,
                "retrieval_trace": {
                    "strategy": retrieval_strategy,
                    "channels": channel_counts,
                    "rrf_rank_constant": 60 if retrieval_strategy == "rrf" else None,
                    "dense_provider": self.embedder.model_id if self.embedder else None,
                    "dense_only_min_similarity": self.dense_only_min_similarity
                    if self.embedder
                    else None,
                },
                "degraded": degraded,
                "knowledge_gaps": [] if evidence else ["No applicable published evidence found."],
            },
            list(seen_refs),
        )

    async def search_response_guidance(
        self,
        *,
        intent: str,
        emotion: str,
        priority: str = "normal",
        product: str | dict[str, Any] | None = None,
        limit: int = 2,
    ) -> dict[str, Any]:
        """Return tone/structure guidance separately from evidence and executable steps."""
        product_id = _product_id(product)
        if not product_id:
            return result(
                "needs_info", {"guidance": [], "missing_conditions": ["confirmed_product"]}
            )
        if not 1 <= limit <= 5:
            raise ValueError("Response guidance limit must be 1 through 5")
        ranked = []
        for document in await self.repository.list_documents(product_id, "communication"):
            if (
                document.get("status") != "published"
                or document.get("non_authoritative") is not True
                or document.get("allowed_use") != "tone_and_structure_only"
            ):
                continue
            for chunk in document.get("chunks", []):
                if intent not in chunk.get("intents", []):
                    continue
                priorities = chunk.get("priorities", ["normal"])
                if priority not in priorities:
                    continue
                emotions = chunk.get("emotions", [])
                if emotion not in emotions and "neutral" not in emotions:
                    continue
                score = 10 + (3 if emotion in emotions else 0) + (4 if priority == "safety" else 0)
                ranked.append((score, document, chunk))
        ranked.sort(key=lambda row: (-row[0], row[2]["id"]))
        guidance = [
            {
                "id": chunk["id"],
                "title": chunk.get("title", ""),
                "text": chunk["text"],
                "intent": intent,
                "emotion": emotion,
                "priority": priority,
                "authority": "project_authored_non_official",
                "allowed_use": "tone_and_structure_only",
                "forbidden_uses": document.get("forbidden_uses", []),
            }
            for _, document, chunk in ranked[:limit]
        ]
        return result(
            "ok" if guidance else "not_found",
            {
                "guidance": guidance,
                "separation_notice": (
                    "Communication guidance is not evidence and cannot support product facts, "
                    "policy eligibility, promises, or safety operations."
                ),
            },
        )

    def _steps(
        self, document: dict[str, Any], chunk: dict[str, Any], scope: dict[str, Any], ref: str
    ) -> list[dict[str, Any]]:
        steps = []
        for step in chunk.get("steps", []):
            if (
                step.get("reviewed") is not True
                or step.get("consumer_safe") is not True
                or not step.get("reviewed_by")
                or step.get("status") in {"withdrawn", "invalidated", "candidate", "revoked"}
            ):
                continue
            valid, gaps = step_scope(document, chunk, step, scope)
            if valid and not gaps:
                steps.append(
                    {
                        **copy.deepcopy(step),
                        "product_ids": step.get(
                            "product_ids", chunk.get("product_ids", document["product_ids"])
                        ),
                        "document_id": document["id"],
                        "version": document["version"],
                        "evidence_refs": [ref],
                        "source_url": document["source_url"],
                        "synthetic": bool(document.get("synthetic")),
                    }
                )
        return steps

    async def get_step(
        self,
        step_id: str,
        product: str | dict[str, Any] | None = None,
        region: str | None = None,
        channel: str | None = None,
        firmware: str | None = None,
        conditions: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        product_id = _product_id(product)
        if not product_id:
            return result("needs_info", {"step": None, "missing_conditions": ["confirmed_product"]})
        scope = {
            "product_id": product_id,
            "region": region,
            "channel": channel,
            "firmware": firmware,
            "conditions": conditions,
            "at": at,
        }
        found: list[dict[str, Any]] = []
        missing: set[str] = set()
        claims: dict[str, dict[str, list[str]]] = {}
        for document in await self.repository.list_documents(product_id, "technical"):
            if document["status"] != "published" or (
                document.get("synthetic") and not self.allow_synthetic
            ):
                continue
            for chunk in document["chunks"]:
                chunk_valid, chunk_gaps = section_scope(document, chunk, scope)
                if not chunk_valid:
                    continue
                ref = f"knowledge:{document['id']}:{document['version']}:{chunk['id']}"
                if chunk.get("claim_key") and not chunk_gaps:
                    value = json.dumps(chunk.get("claim_value"), sort_keys=True, ensure_ascii=False)
                    claims.setdefault(chunk["claim_key"], {}).setdefault(value, []).append(ref)
                for step in chunk.get("steps", []):
                    if step["step_id"] != step_id:
                        continue
                    applicable, gaps = step_scope(document, chunk, step, scope)
                    if not applicable:
                        continue
                    missing.update(gaps)
                    if gaps:
                        continue
                    found.extend(
                        s
                        for s in self._steps(document, chunk, scope, ref)
                        if s["step_id"] == step_id
                    )
        conflicts = [
            {
                "claim_key": key,
                "evidence_ids": sorted(ref for refs in values.values() for ref in refs),
            }
            for key, values in claims.items()
            if len(values) > 1
        ]
        if found and (len(found) > 1 or conflicts):
            return result(
                "conflict",
                {
                    "step": None,
                    "missing_conditions": [],
                    "conflicts": conflicts,
                    "reason": "Current applicable sources or step versions conflict",
                },
            )
        if not found:
            return result(
                "needs_info" if missing else "not_found",
                {"step": None, "missing_conditions": sorted(missing)},
            )
        return result("ok", {"step": found[0]}, found[0]["evidence_refs"])
