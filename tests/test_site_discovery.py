from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from site_discovery import (  # noqa: E402
    canonical_host,
    discover_by_links,
    discover_from_sitemaps,
    parse_sitemap,
)


class SiteDiscoveryTests(unittest.TestCase):
    def test_canonical_host_treats_www_as_alias(self) -> None:
        self.assertEqual(canonical_host("https://www.csgraduates.com/a"), "csgraduates.com")
        self.assertEqual(canonical_host("https://csgraduates.com/a"), "csgraduates.com")

    def test_parse_sitemap(self) -> None:
        kind, locations = parse_sitemap(
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b'<url><loc>https://csgraduates.com/a/</loc></url></urlset>'
        )
        self.assertEqual(kind, "urlset")
        self.assertEqual(locations, ["https://csgraduates.com/a/"])

    def test_www_sitemap_locations_are_rewritten_to_base_host(self) -> None:
        def fetch_text(url: str, accept: str = ""):
            if url.endswith("robots.txt"):
                return "Sitemap: https://www.csgraduates.com/sitemap.xml\n", url
            raise RuntimeError(url)

        def fetch_bytes(url: str, accept: str = ""):
            if url.endswith("sitemap.xml"):
                payload = (
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    '<url><loc>https://www.csgraduates.com/a/</loc></url>'
                    '</urlset>'
                ).encode()
                return payload, url, "application/xml"
            raise RuntimeError(url)

        def normalize(url: str, base: str):
            return url if url.startswith("https://csgraduates.com/") else None

        result = asyncio.run(
            discover_from_sitemaps(
                "https://csgraduates.com/",
                fetch_bytes=fetch_bytes,
                fetch_text=fetch_text,
                normalize_url=normalize,
                candidates=("sitemap.xml",),
            )
        )
        self.assertEqual(result["urls"], ["https://csgraduates.com/a/"])
        self.assertEqual(result["reports"][0]["accepted_documents"], 1)

    def test_concurrent_bfs_discovers_linked_pages(self) -> None:
        pages = {
            "https://csgraduates.com/": '<a href="/a/">A</a><a href="/b/">B</a>',
            "https://csgraduates.com/a/": '<a href="/c/">C</a>',
            "https://csgraduates.com/b/": "B",
            "https://csgraduates.com/c/": "C",
        }

        def fetch_text(url: str, accept: str = ""):
            return pages[url], url

        def normalize(url: str, base: str):
            from urllib.parse import urljoin, urlsplit, urlunsplit

            resolved = urljoin(base, url)
            parsed = urlsplit(resolved)
            if parsed.hostname != "csgraduates.com":
                return None
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

        result = asyncio.run(
            discover_by_links(
                "https://csgraduates.com/",
                max_urls=10,
                concurrency=3,
                fetch_text=fetch_text,
                normalize_url=normalize,
            )
        )
        self.assertEqual(
            result,
            [
                "https://csgraduates.com/",
                "https://csgraduates.com/a/",
                "https://csgraduates.com/b/",
                "https://csgraduates.com/c/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
