"""Conservative collector for public Anker support-page metadata.

The collector discovers an explicit allow-list through official sitemaps, checks
robots.txt, and stores only audit metadata.  Page bodies are never persisted;
maintainers write and review short paraphrases before knowledge publication.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx

ALLOWED_HOSTS = {"service.anker.com", "service.soundcore.com", "service.eufy.com"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _checked_url(value: str, *, hosts: set[str] = ALLOWED_HOSTS) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Official collection only accepts allow-listed HTTPS URLs")
    return value


def _bounded_content(response: httpx.Response) -> bytes:
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Official source response is too large")
    return body


def parse_sitemap(body: bytes) -> set[str]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Official sitemap is too large")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ValueError("Official sitemap is not valid XML") from error
    urls = set()
    for node in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
        location = node.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if location is not None and location.text:
            urls.add(location.text.strip())
    return urls


def extract_title(body: bytes, fallback: str) -> str:
    text = body.decode("utf-8", errors="replace")
    generic = {"anker", "soundcore", "eufy support | troubleshooting & customer service"}
    for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = re.sub(r"<[^>]+>", " ", match.group(1))
            title = re.sub(r"\s+", " ", html.unescape(title)).strip()
            if title and title.casefold() not in generic:
                return title[:500]
    return fallback[:500]


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    response = await client.get(_checked_url(url))
    if response.status_code != 200:
        raise ValueError(f"Official source returned HTTP {response.status_code}")
    final_url = str(response.url)
    if urlsplit(final_url).hostname != urlsplit(url).hostname:
        raise ValueError("Official source redirected to a different host")
    _bounded_content(response)
    return response


async def collect_official_sources(
    config_path: str | Path,
    output_path: str | Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int | str]:
    config_path, output_path = Path(config_path), Path(output_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    groups = config.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Official collection config needs non-empty groups")
    user_agent = str(config.get("user_agent") or "KefuAgentResearchBot/0.1")
    delay = float(config.get("delay_seconds", 1.0))
    if not 0 <= delay <= 10:
        raise ValueError("Collection delay must be between 0 and 10 seconds")

    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": user_agent}, follow_redirects=True, timeout=30
        )
    records: list[dict] = []
    discovered = 0
    try:
        for group in groups:
            brand = str(group.get("brand") or "").strip()
            sitemap_url = _checked_url(str(group.get("sitemap_url") or ""))
            host = urlsplit(sitemap_url).hostname or ""
            robots_url = f"https://{host}/robots.txt"
            robots_response = await _get(client, robots_url)
            robots = RobotFileParser(robots_url)
            robots.parse(robots_response.text.splitlines())
            sitemap_response = await _get(client, sitemap_url)
            sitemap_urls = parse_sitemap(_bounded_content(sitemap_response))
            discovered += len(sitemap_urls)
            for source in group.get("sources", []):
                url = _checked_url(str(source.get("url") or ""), hosts={host})
                if url not in sitemap_urls:
                    raise ValueError(f"Selected source is not present in official sitemap: {url}")
                if not robots.can_fetch(user_agent, url):
                    raise ValueError(f"robots.txt does not allow collection: {url}")
                response = await _get(client, url)
                body = _bounded_content(response)
                fallback = str(source.get("title") or source.get("source_id") or url)
                records.append(
                    {
                        "record_type": "collection_audit",
                        "source_id": str(source.get("source_id") or ""),
                        "brand": brand,
                        "url": url,
                        "title": extract_title(body, fallback),
                        "accessed_at": datetime.now(UTC).date().isoformat(),
                        "robots_allowed": True,
                        "sitemap_listed": True,
                        "http_status": response.status_code,
                        "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                        "content_bytes": len(body),
                        "content_sha256": hashlib.sha256(body).hexdigest(),
                        "body_persisted": False,
                    }
                )
                if delay:
                    await asyncio.sleep(delay)
    finally:
        if owned_client:
            await client.aclose()
    if not records:
        raise ValueError("Official collection selected no sources")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    output_path.write_text(payload, encoding="utf-8")
    return {
        "selected_sources": len(records),
        "sitemap_urls_seen": discovered,
        "output": str(output_path),
    }
