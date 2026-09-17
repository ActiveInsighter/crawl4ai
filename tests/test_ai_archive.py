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


if __name__ == "__main__":
    unittest.main()
