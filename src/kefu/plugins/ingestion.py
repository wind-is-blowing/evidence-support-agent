"""Offline Docling/HybridChunker preparation. Extraction never approves an operation.

Only locally supplied files and locally staged model/tokenizer assets are opened.
The resulting manifest stays candidate until a maintainer reviews the actual text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def _offline_environment():
    keys = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _local_components(source: Path, parser: dict[str, Any]):
    tokenizer_path = Path(parser.get("tokenizer_path", ""))
    revision = parser.get("tokenizer_revision", "")
    if (
        not tokenizer_path.is_absolute()
        or not tokenizer_path.is_dir()
        or not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision)
    ):
        raise ValueError("Provide an absolute local tokenizer_path and pinned tokenizer_revision")
    from docling.chunking import HybridChunker
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    format_options = {}
    if source.suffix.lower() == ".pdf":
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        artifacts_path = Path(parser.get("artifacts_path", ""))
        if not artifacts_path.is_absolute() or not artifacts_path.is_dir():
            raise ValueError("PDF parsing requires explicitly staged local Docling artifacts_path")
        format_options[InputFormat.PDF] = PdfFormatOption(
            pipeline_options=PdfPipelineOptions(
                artifacts_path=artifacts_path,
                do_ocr=False,
                do_table_structure=True,
                enable_remote_services=False,
            )
        )
    tokenizer = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(
            str(tokenizer_path), revision=revision, local_files_only=True, trust_remote_code=False
        ),
        max_tokens=int(parser.get("max_tokens", 512)),
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.MD, InputFormat.HTML],
        format_options=format_options,
    )
    return converter, HybridChunker(tokenizer=tokenizer, merge_peers=False)


def _body_html(source: Path, selector: str) -> str:
    if not selector or selector in {"body", "html", "*"}:
        raise ValueError("HTML requires a reviewed article_selector, not the whole page")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    selected = soup.select(selector)
    if len(selected) != 1:
        raise ValueError("article_selector must select exactly one article body")
    article = selected[0]
    for element in article.select(
        "script, style, nav, footer, header, aside, iframe, object, embed"
    ):
        element.decompose()
    # Preserve visible labels; do not let conversion fetch remote attachments.
    for element in article.find_all(True):
        for attr in ("src", "srcset", "poster", "background", "data"):
            if attr in element.attrs:
                element.attrs.pop(attr)
    return str(article)


def prepare_knowledge(
    source: str | Path,
    metadata: str | Path | dict[str, Any],
    output: str | Path,
    *,
    converter: Any = None,
    chunker: Any = None,
) -> dict[str, Any]:
    source_path, output_path = Path(source).resolve(), Path(output).resolve()
    if not source_path.is_file() or source_path.suffix.lower() not in {
        ".pdf",
        ".html",
        ".htm",
        ".md",
    }:
        raise ValueError("Source must be a local PDF, HTML, or Markdown file")
    if source_path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Source exceeds the 20 MiB offline parsing limit")
    info = (
        copy.deepcopy(metadata)
        if isinstance(metadata, dict)
        else json.loads(Path(metadata).read_text(encoding="utf-8"))
    )
    document = copy.deepcopy(info.get("document", {}))
    required = {"id", "version", "source_url", "product_ids", "kind", "license", "reviewed_by"}
    if (
        required - document.keys()
        or not document.get("product_ids")
        or not document.get("reviewed_by")
    ):
        raise ValueError("Reviewed source, scope, license and version metadata are required")
    if any(
        not isinstance(document[field], str) or not document[field].strip()
        for field in ("id", "version", "source_url", "license")
    ):
        raise ValueError("Document identity and provenance must be nonempty strings")
    parser = info.get("parser", {})
    raw_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if document.get("source_sha256") and document["source_sha256"] != raw_hash:
        raise ValueError("Source changed after metadata review")
    prepared_path = source_path
    with tempfile.TemporaryDirectory(prefix="kefu-ingest-") as temporary, _offline_environment():
        if source_path.suffix.lower() in {".html", ".htm"}:
            prepared_path = Path(temporary) / "article.html"
            prepared_path.write_text(
                _body_html(source_path, parser.get("article_selector", "")), encoding="utf-8"
            )
        if converter is None or chunker is None:
            if converter is not None or chunker is not None:
                raise ValueError("Supply both parser test doubles or neither")
            converter, chunker = _local_components(source_path, parser)
        conversion = converter.convert(
            source=prepared_path, max_num_pages=100, max_file_size=20 * 1024 * 1024
        )
        status = getattr(conversion.status, "value", conversion.status)
        if str(status).lower() != "success":
            raise ValueError(
                "Document conversion was not complete; source kept, no manifest published"
            )
        doc = conversion.document
        chunks = []
        sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for index, extracted in enumerate(chunker.chunk(dl_doc=doc)):
            if not extracted.text.strip():
                continue
            headings = list(getattr(extracted.meta, "headings", None) or [])
            section = " / ".join(headings) or "Document body"
            items = list(getattr(extracted.meta, "doc_items", None) or [])
            pages = sorted(
                {
                    provenance.page_no
                    for item in items
                    for provenance in (getattr(item, "prov", None) or [])
                }
            )
            refs = [item.self_ref for item in items if getattr(item, "self_ref", None)]
            scope_fields = {
                "product_ids",
                "regions",
                "channels",
                "firmware_min",
                "firmware_max",
                "conditions",
            }
            scope = {
                key: copy.deepcopy(value)
                for key, value in info.get("section_scopes", {}).get(section, {}).items()
                if key in scope_fields
            }
            if scope.get("product_ids") and not set(scope["product_ids"]).issubset(
                document["product_ids"]
            ):
                raise ValueError("A section cannot broaden the reviewed document product scope")
            chunk = {
                "id": f"chunk-{index:05d}",
                "text": extracted.text,
                "title": section,
                "location": {"pages": pages, "doc_item_refs": refs, "heading_path": headings},
                "source_item_metadata": [
                    item.model_dump(mode="json") for item in items if hasattr(item, "model_dump")
                ],
                "steps": [],
                "review_status": "candidate",
                **scope,
            }
            # Scope metadata is not permitted to inject generated executable instructions.
            chunk["steps"] = []
            chunk["review_status"] = "candidate"
            chunks.append(chunk)
            sections[section].append(chunk)
        if not chunks:
            raise ValueError("No readable chunks found; OCR or manual extraction is required")
        parents = []
        for section, children in sections.items():
            if len(children) < 2:
                continue
            parent_id = "section-" + hashlib.sha256(section.encode()).hexdigest()[:16]
            parents.append(
                {
                    "id": parent_id,
                    "title": section,
                    "text": "\n\n".join(c["text"] for c in children),
                    "location": {"heading_path": section, "child_ids": [c["id"] for c in children]},
                    "is_parent": True,
                    "steps": [],
                    "review_status": "candidate",
                    **{
                        key: copy.deepcopy(value)
                        for key, value in info.get("section_scopes", {}).get(section, {}).items()
                        if key in scope_fields
                    },
                }
            )
            for child in children:
                child["parent_id"] = parent_id
        document.update(
            status="candidate",
            chunks=parents + chunks,
            source_sha256=raw_hash,
            parser="Docling/HybridChunker",
            extraction_review_required=True,
            extraction_note="Automatic parsing preserves source text; warnings, models, tables and numerical values require human review.",
        )
        document.pop("content_hash", None)
        records = [
            {"record_type": "product", "product": product} for product in info.get("products", [])
        ]
        records.append({"record_type": "document", "document": document})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve a structured source copy for locating tables and image references;
        # its redistribution follows the input source's license, not project licensing.
        structured = output_path.with_suffix(output_path.suffix + ".docling.json")
        doc.save_as_json(structured)
        payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        temporary_output = output_path.with_name(output_path.name + ".tmp")
        try:
            temporary_output.write_text(payload, encoding="utf-8")
            os.replace(temporary_output, output_path)
        finally:
            temporary_output.unlink(missing_ok=True)
    return {
        "manifest": str(output_path),
        "structured_source": str(structured),
        "source_sha256": raw_hash,
        "chunks": len(chunks),
        "parent_sections": len(parents),
        "status": "candidate",
        "approved_steps": 0,
    }
