import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from crawl_site_static import _extract_links_and_media, _prepare_browser_html, _title_from_html


class StaticArchiveHelpersTest(unittest.TestCase):
    def test_prepare_browser_html_injects_single_base(self):
        html = "<html><head><base href='https://old.example/'></head><body><img src='/x.png'></body></html>"
        prepared = _prepare_browser_html(html, "https://csgraduates.com/a/b/")
        self.assertEqual(prepared.count("<base"), 1)
        self.assertIn('href="https://csgraduates.com/a/b/"', prepared)

    def test_title_prefers_document_title(self):
        html = "<html><head><title>测试页面 | 计算机考研杂货铺</title></head><body><h1>标题</h1></body></html>"
        self.assertEqual(_title_from_html(html, "fallback"), "测试页面 | 计算机考研杂货铺")

    def test_extract_links_and_media_resolves_relative_urls(self):
        html = """
        <html><body>
          <a href='/data_structure/'>数据结构</a>
          <a href='https://example.com/x'>外部</a>
          <img src='/images/a.png' alt='A'>
        </body></html>
        """
        links, media = _extract_links_and_media(html, "https://csgraduates.com/a/")
        self.assertEqual(links["internal"][0]["href"], "https://csgraduates.com/data_structure/")
        self.assertEqual(links["external"][0]["href"], "https://example.com/x")
        self.assertEqual(media["images"][0]["src"], "https://csgraduates.com/images/a.png")


if __name__ == "__main__":
    unittest.main()
