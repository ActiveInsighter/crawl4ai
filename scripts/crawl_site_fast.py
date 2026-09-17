#!/usr/bin/env python3
"""Run the site archiver with optimized sitemap/BFS discovery."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

import crawl_site as core
from site_discovery import discover_by_links as fast_link_bfs
from site_discovery import discover_from_sitemaps as audited_sitemaps


def canonicalize_to_base_origin(url: str, base_url: str) -> str | None:
    """Rewrite sitemap http/www aliases to the exact scheme+host of base_url."""
    parsed = urlsplit(url)
    base = urlsplit(base_url)
    candidate = urlunsplit((base.scheme, base.netloc, parsed.path, "", ""))
    return core.normalize_url(candidate, base_url)


async def discover_from_sitemaps(base_url: str):
    result = await audited_sitemaps(
        base_url,
        fetch_bytes=core._fetch_bytes,
        fetch_text=core._fetch_text,
        normalize_url=core.normalize_url,
        candidates=core.SITEMAP_CANDIDATES,
    )
    result["urls"] = sorted(
        {
            canonical
            for url in result["urls"]
            if (canonical := canonicalize_to_base_origin(url, base_url)) is not None
        }
    )
    return result


async def discover_by_links(base_url: str, max_urls: int = 5000):
    return await fast_link_bfs(
        base_url,
        max_urls=max_urls,
        concurrency=10,
        fetch_text=core._fetch_text,
        normalize_url=core.normalize_url,
    )


def main() -> None:
    # archive_site resolves these functions from crawl_site's module globals.
    core.discover_from_sitemaps = discover_from_sitemaps
    core.discover_by_links = discover_by_links
    asyncio.run(core.archive_site(core.parse_args()))


if __name__ == "__main__":
    main()
