from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from crawl_site import (  # noqa: E402
    build_tree,
    normalize_url,
    output_dir_for_url,
    parse_sitemap_xml,
    safe_segment,
    write_catalog,
)


class SiteCrawlerTests(unittest.TestCase):
    def test_parse_urlset_and_filter_assets(self) -> None:
        payload = b'''<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://csgraduates.com/a/b/</loc></url>
          <url><loc>https://csgraduates.com/images/a.png</loc></url>
        </urlset>'''
        kind, values = parse_sitemap_xml(payload)
        self.assertEqual(kind, "urlset")
        self.assertEqual(len(values), 2)
        self.assertEqual(
            normalize_url(values[0], "https://csgraduates.com/"),
            "https://csgraduates.com/a/b/",
        )
        self.assertIsNone(normalize_url(values[1], "https://csgraduates.com/"))

    def test_parse_sitemap_index(self) -> None:
        payload = b'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://csgraduates.com/sitemap-1.xml</loc></sitemap>
        </sitemapindex>'''
        kind, values = parse_sitemap_xml(payload)
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(values, ["https://csgraduates.com/sitemap-1.xml"])

    def test_normalize_url_rejects_cross_origin_and_taxonomy(self) -> None:
        base = "https://csgraduates.com/"
        self.assertIsNone(normalize_url("https://example.com/a/", base))
        self.assertIsNone(normalize_url("/tags/os/", base))
        self.assertEqual(
            normalize_url("/a/b/?utm_source=x#part", base),
            "https://csgraduates.com/a/b/",
        )

    def test_windows_safe_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = output_dir_for_url(
                "https://csgraduates.com/a/%E6%B5%8B%E8%AF%95:1/con.html",
                root,
            )
            self.assertEqual(path.relative_to(root).parts[0], "pages")
            self.assertNotIn(":", str(path))
            self.assertTrue(str(path).endswith("_con"))
            self.assertEqual(safe_segment("CON"), "_CON")

    def test_tree_and_catalog(self) -> None:
        records = [
            {
                "url": "https://csgraduates.com/a/b/",
                "title": "B",
                "success": True,
                "output_dir": "pages/a/b",
            },
            {
                "url": "https://csgraduates.com/a/c/",
                "title": "C",
                "success": False,
                "output_dir": "pages/a/c",
            },
        ]
        tree = build_tree(records)
        self.assertEqual(tree["type"], "root")
        self.assertTrue(tree["children"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / "pages" / "a" / "b"
            page.mkdir(parents=True)
            (page / "content.md").write_text("hello\n", encoding="utf-8")
            write_catalog(records, root)
            catalog = (root / "catalog.md").read_text(encoding="utf-8")
            self.assertIn("B", catalog)
            self.assertNotIn("[C]", catalog)
            jsonl = (root / "site-content.jsonl").read_text(encoding="utf-8")
            parsed = json.loads(jsonl.strip())
            self.assertEqual(parsed["markdown"], "hello\n")


if __name__ == "__main__":
    unittest.main()
