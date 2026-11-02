import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.ai_archive_core import clean_article, export_inline_svgs, restore_svg_case


SVG_FIXTURE = """
<div class="td-content">
  <style>.page-only { color: red; }</style>
  <svg viewBox="0 0 100 50">
    <style>.label{font-weight:700}.edge{stroke:#333}</style>
    <defs>
      <marker id="arrow" markerHeight="6" markerWidth="6" refX="9" refY="5">
        <path d="M0 0 10 5 0 10z" />
      </marker>
    </defs>
    <foreignObject requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility"
                   x="0" y="0" width="100" height="30">
      <div xmlns="http://www.w3.org/1999/xhtml" class="label">CPU</div>
    </foreignObject>
  </svg>
</div>
"""


class AiArchiveTests(unittest.TestCase):
    def test_clean_article_preserves_embedded_svg_style(self):
        soup = BeautifulSoup(SVG_FIXTURE, "html.parser")
        article = soup.select_one(".td-content")
        self.assertIsNotNone(article)
        clean_article(article)
        styles = article.find_all("style")
        self.assertEqual(len(styles), 1)
        self.assertIsNotNone(styles[0].find_parent("svg"))
        self.assertIn("font-weight", styles[0].get_text())

    def test_svg_case_is_restored_for_standalone_xml(self):
        soup = BeautifulSoup(SVG_FIXTURE, "html.parser")
        svg = soup.find("svg")
        self.assertEqual(svg.name, "svg")
        self.assertIn("viewbox", svg.attrs)
        restore_svg_case(svg)
        text = str(svg)
        self.assertIn('viewBox="0 0 100 50"', text)
        self.assertIn("<foreignObject", text)
        self.assertIn("requiredFeatures=", text)
        self.assertIn("markerHeight=", text)
        self.assertIn("markerWidth=", text)
        self.assertIn("refX=", text)
        self.assertIn("refY=", text)

    def test_exported_svg_keeps_style_and_case(self):
        soup = BeautifulSoup(SVG_FIXTURE, "html.parser")
        article = soup.select_one(".td-content")
        clean_article(article)
        with tempfile.TemporaryDirectory() as tmp:
            manifest, skipped = export_inline_svgs(article, Path(tmp))
            self.assertEqual(skipped, 0)
            self.assertEqual(len(manifest), 1)
            svg_text = (Path(tmp) / manifest[0]["path"]).read_text(encoding="utf-8")
            self.assertIn("<style>", svg_text)
            self.assertIn(".label{font-weight:700}", svg_text)
            self.assertIn('viewBox="0 0 100 50"', svg_text)
            self.assertIn("<foreignObject", svg_text)
            self.assertIn("markerHeight=", svg_text)
            self.assertIn("requiredFeatures=", svg_text)
            image = article.find("img")
            self.assertIsNotNone(image)
            self.assertEqual(image.get("src"), "assets/svg/figure-001.svg")

    def test_browser_svg_assets_are_not_overwritten_by_secondary_export(self):
        from xml.etree import ElementTree as ET
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "assets" / "svg"
            folder.mkdir(parents=True)
            browser_xml = '<svg xmlns="http://www.w3.org/2000/svg"><text>source vector</text></svg>'
            (folder / "figure-001.svg").write_text(browser_xml, encoding="utf-8")
            soup = BeautifulSoup(
                '<div class="td-content">'
                '<img src="assets/svg/figure-001.svg" alt="browser diagram"/>'
                '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
                '</div>', "html.parser"
            )
            exported, skipped = export_inline_svgs(soup.select_one(".td-content"), root)
            self.assertEqual(skipped, 0)
            self.assertEqual(exported[0]["path"], "assets/svg/figure-002.svg")
            self.assertEqual((folder / "figure-001.svg").read_text(encoding="utf-8"), browser_xml)
            ET.parse(folder / "figure-002.svg")

    def test_formula_source_order_is_never_assumed(self):
        from scripts.ai_archive_core import replace_math_with_tokens
        soup = BeautifulSoup('<div class="td-content"><span class="katex"><span class="katex-html">unknown</span></span></div>', "html.parser")
        counts = {}
        changes = replace_math_with_tokens(soup.select_one(".td-content"), [{"tex": "WRONG"}], stats=counts)
        self.assertEqual(changes, {})
        self.assertEqual(counts["unresolved"], 1)
        self.assertIn("unknown", soup.get_text())

    def test_annotated_formula_and_screenshot_fallback(self):
        from scripts.ai_archive_core import replace_math_with_tokens
        soup = BeautifulSoup("""
          <div class="td-content">
            <span class="katex"><math><annotation encoding="application/x-tex">x^2</annotation></math></span>
            <span class="katex" data-archive-formula-image="assets/formulas/formula-0001.png">
              <span class="katex-html">visual</span></span>
          </div>""", "html.parser")
        stats = {}
        changes = replace_math_with_tokens(soup.select_one(".td-content"), stats=stats)
        self.assertEqual(stats, {"total": 2, "tex": 1, "image": 1, "unresolved": 0})
        self.assertIn("x^2", "".join(changes.values()))
        self.assertIn("formula-0001.png", "".join(changes.values()))

    def test_lazy_source_is_not_gif_placeholder(self):
        from scripts.ai_archive_core import _image_source
        image = BeautifulSoup('<img src="data:image/gif;base64,AAAA" data-src="/figures/real.png">', "html.parser").img
        self.assertEqual(_image_source(image), "/figures/real.png")

    def test_svg_embeds_external_image_and_remains_valid_xml(self):
        import asyncio
        import xml.etree.ElementTree as ET
        from unittest.mock import patch
        from scripts.ai_archive_core import inline_svg_images
        soup = BeautifulSoup('<div class="td-content"><svg viewBox="0 0 10 10"><image href="/cache.png"/></svg></div>', "html.parser")
        article = soup.select_one(".td-content")
        with patch("scripts.ai_archive_core.fetch_binary", return_value=(b"\x89PNG\r\n\x1a\nfake", "image/png")):
            failures = asyncio.run(inline_svg_images(article, "https://csgraduates.com/"))
        self.assertEqual(failures, [])
        self.assertTrue(article.image["href"].startswith("data:image/png;base64,"))
        with tempfile.TemporaryDirectory() as temp:
            svgs, skipped = export_inline_svgs(article, Path(temp))
            self.assertEqual((len(svgs), skipped), (1, 0))
            ET.parse(Path(temp) / svgs[0]["path"])

    def test_quality_report_detects_missing_assets(self):
        from scripts.merge_ai_shards import verify_page_integrity, build_quality_report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / "pages" / "operating_system" / "process" / "sync"
            page.mkdir(parents=True)
            (page / "content.md").write_text("![cache](assets/images/missing.png)")
            (page / "content.html").write_text("<p>sync</p>")
            record = {"success": True, "output_dir": "pages/operating_system/process/sync",
                      "url": "https://example.test/", "browser_rendered": False}
            issues = verify_page_integrity(record, root)
            self.assertIn("missing_local_asset", issues)
            record["quality_issues"] = issues
            record["quality_pass"] = False
            report = build_quality_report([record])
            self.assertEqual(report["subjects"]["操作系统"]["review"], 1)



if __name__ == "__main__":
    unittest.main()
