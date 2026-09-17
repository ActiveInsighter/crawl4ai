#!/usr/bin/env python3
"""Fast, auditable URL discovery helpers for documentation-site archiving."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def canonical_host(url: str) -> str:
    """Treat www.example.com and example.com as the same site boundary."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def parse_sitemap(payload: bytes) -> tuple[str, list[str]]:
    root = ET.fromstring(payload)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    locations = [
        (node.text or "").strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and (node.text or "").strip()
    ]
    return kind, locations


async def discover_from_sitemaps(
    base_url: str,
    *,
    fetch_bytes: Callable[..., tuple[bytes, str, str]],
    fetch_text: Callable[..., tuple[str, str]],
    normalize_url: Callable[[str, str], str | None],
    candidates: tuple[str, ...],
) -> dict[str, Any]:
    """Expand robots-declared/common sitemaps and record diagnostics for each one."""
    base_url = base_url.rstrip("/") + "/"
    robots_url = urljoin(base_url, "robots.txt")
    sitemap_queue: deque[str] = deque()
    sitemap_seen: set[str] = set()
    urls: set[str] = set()
    errors: list[dict[str, str]] = []
    reports: list[dict[str, Any]] = []
    robots_text = ""

    try:
        robots_text, _ = await asyncio.to_thread(fetch_text, robots_url, "text/plain,*/*")
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                location = line.split(":", 1)[1].strip()
                if location:
                    sitemap_queue.append(urljoin(base_url, location))
    except Exception as exc:
        errors.append({"url": robots_url, "error": str(exc)})

    for name in candidates:
        sitemap_queue.append(urljoin(base_url, name))

    while sitemap_queue and len(sitemap_seen) < 100:
        sitemap_url = sitemap_queue.popleft()
        if sitemap_url in sitemap_seen:
            continue
        sitemap_seen.add(sitemap_url)
        try:
            payload, resolved, content_type = await asyncio.to_thread(
                fetch_bytes,
                sitemap_url,
                "application/xml,text/xml,*/*",
            )
            kind, locations = parse_sitemap(payload)
            accepted_before = len(urls)

            if kind == "sitemapindex":
                for location in locations:
                    if canonical_host(location) == canonical_host(base_url):
                        sitemap_queue.append(location)
            elif kind == "urlset":
                for location in locations:
                    # normalize_url remains the final same-origin/document-type gate.
                    # Try a www/non-www canonical rewrite if the sitemap uses the alias.
                    candidate = location
                    if canonical_host(candidate) == canonical_host(base_url):
                        parsed_candidate = urlsplit(candidate)
                        parsed_base = urlsplit(base_url)
                        if (parsed_candidate.hostname or "").lower() != (parsed_base.hostname or "").lower():
                            candidate = parsed_candidate._replace(netloc=parsed_base.netloc).geturl()
                    normalized = normalize_url(candidate, base_url)
                    if normalized:
                        urls.add(normalized)
            else:
                errors.append({"url": resolved, "error": f"unsupported sitemap root: {kind}"})

            reports.append(
                {
                    "requested_url": sitemap_url,
                    "resolved_url": resolved,
                    "content_type": content_type,
                    "root": kind,
                    "locations": len(locations),
                    "accepted_documents": len(urls) - accepted_before,
                    "sample_locations": locations[:5],
                }
            )
        except Exception as exc:
            errors.append({"url": sitemap_url, "error": str(exc)})

    result = {
        "urls": sorted(urls),
        "robots_url": robots_url,
        "robots_text": robots_text,
        "sitemaps": sorted(sitemap_seen),
        "reports": reports,
        "errors": errors,
    }
    print("SITEMAP_DISCOVERY=" + json.dumps({k: v for k, v in result.items() if k != "robots_text"}, ensure_ascii=False))
    return result


async def discover_by_links(
    base_url: str,
    *,
    max_urls: int,
    concurrency: int,
    fetch_text: Callable[..., tuple[str, str]],
    normalize_url: Callable[[str, str], str | None],
) -> list[str]:
    """Concurrent same-origin BFS fallback for sites without a useful sitemap."""
    base = normalize_url(base_url, base_url) or base_url
    queue: deque[str] = deque([base])
    queued: set[str] = {base}
    fetched: set[str] = set()
    documents: set[str] = set()
    concurrency = max(1, concurrency)

    async def fetch_one(url: str) -> tuple[str, str, str | None]:
        try:
            html, resolved = await asyncio.to_thread(fetch_text, url)
            return html, resolved, None
        except Exception as exc:
            return "", url, str(exc)

    while queue and len(fetched) < max_urls:
        batch: list[str] = []
        while queue and len(batch) < concurrency and len(fetched) + len(batch) < max_urls:
            candidate = queue.popleft()
            if candidate not in fetched:
                batch.append(candidate)
        if not batch:
            continue

        results = await asyncio.gather(*(fetch_one(url) for url in batch))
        for requested, (html, resolved, error) in zip(batch, results):
            fetched.add(requested)
            if error:
                continue
            normalized_resolved = normalize_url(resolved, base_url)
            if normalized_resolved:
                documents.add(normalized_resolved)
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                candidate = normalize_url(str(anchor.get("href") or ""), base_url)
                if not candidate or candidate in fetched or candidate in queued:
                    continue
                queued.add(candidate)
                queue.append(candidate)

    print(
        "LINK_BFS_DISCOVERY="
        + json.dumps(
            {
                "fetched": len(fetched),
                "documents": len(documents),
                "queued_remaining": len(queue),
                "max_urls": max_urls,
                "concurrency": concurrency,
            },
            ensure_ascii=False,
        )
    )
    return sorted(documents)
