import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.crawl_test import (
    export_inline_svgs,
    normalize_links,
    render_article_markdown,
    replace_math_with_tokens,
    select_article,
)


FIXTURE = r"""
<html>
  <body>
    <main>
      <div class="sidebar">noise noise noise noise noise noise noise</div>
      <div class="td-content">
        <h1>Instruction Set Architecture</h1>
        <p><a href="/quiz/1/">quiz</a></p>
        <div class="katex-display">
          <span class="katex">
            <span class="katex-mathml">
              <math display="block">
                <semantics>
                  <mrow><mi>EA</mi></mrow>
                  <annotation encoding="application/x-tex">EA=(EBX)+(ECX)\times 4+8</annotation>
                </semantics>
              </math>
            </span>
            <span class="katex-html">EA = (EBX) + (ECX) × 4 + 8</span>
          </span>
        </div>
        <svg viewBox="0 0 100 50">
          <title>ISA layers</title>
          <desc>Application, operating system and ISA layers</desc>
          <text x="10" y="20">ISA</text>
        </svg>
      </div>
    </main>
  </body>
</html>
"""


class CrawlPostprocessTests(unittest.TestCase):
    def article(self):
        soup = BeautifulSoup(FIXTURE, "html.parser")
        return select_article(soup)

    def test_article_selector_priority(self):
        article, selector = self.article()
        self.assertEqual(selector, ".td-content")
        self.assertIn("Instruction Set Architecture", article.get_text(" ", strip=True))
        self.assertNotIn("noise noise", article.get_text(" ", strip=True))

    def test_katex_is_recovered_once_and_restored_to_markdown(self):
        article, _ = self.article()
        formulas, replacements = replace_math_with_tokens(article)
        self.assertEqual(len(formulas), 1)
        self.assertTrue(formulas[0]["recovered"])
        self.assertTrue(formulas[0]["display"])
        self.assertEqual(formulas[0]["tex"], r"EA=(EBX)+(ECX)\times 4+8")

        markdown = render_article_markdown(article, replacements)
        self.assertIn("$$", markdown)
        self.assertIn(r"EA=(EBX)+(ECX)\times 4+8", markdown)
        self.assertNotIn("C4AMATHTOKEN", markdown)

    def test_inline_svg_is_exported_and_replaced_with_local_image(self):
        article, _ = self.article()
        with tempfile.TemporaryDirectory() as tmp:
            manifest = export_inline_svgs(article, Path(tmp))
            self.assertEqual(len(manifest), 1)
            svg_path = Path(tmp) / manifest[0]["local_path"]
            self.assertTrue(svg_path.exists())
            self.assertIn("ISA layers", svg_path.read_text(encoding="utf-8"))
            image = article.find("img")
            self.assertIsNotNone(image)
            self.assertEqual(image.get("src"), "assets/svg/figure-001.svg")

    def test_links_are_made_absolute(self):
        article, _ = self.article()
        normalize_links(article, "https://csgraduates.com/docs/page/")
        self.assertEqual(
            article.find("a").get("href"),
            "https://csgraduates.com/quiz/1/",
        )


if __name__ == "__main__":
    unittest.main()
