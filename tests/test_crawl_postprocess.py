import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.crawl_test import (
    export_inline_svgs,
    extract_source_formulas,
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
        <svg aria-hidden="true" viewBox="0 0 20 20"><path d="M0 0h1v1z"/></svg>
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

SOURCE_FIXTURE = r"""
<html>
  <body>
    <main>
      <div class="td-content">
        <p>Address:</p>
        <div>$$EA=(\mathrm{EBX})+(\mathrm{ECX})\times 4+8$$</div>
        <p>Inline: \(x+y\)</p>
        <pre><code>$$not_math$$</code></pre>
      </div>
    </main>
  </body>
</html>
"""

RENDERED_NO_TEX = r"""
<div class="td-content">
  <p>Address:</p>
  <div class="katex-display">
    <span class="katex">
      <span class="katex-html" aria-hidden="true">EA = ( EBX ) + ( ECX ) × 4 + 8</span>
    </span>
  </div>
</div>
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
        self.assertEqual(formulas[0]["tex_source"], "rendered-dom")

        markdown = render_article_markdown(article, replacements)
        self.assertIn("$$", markdown)
        self.assertIn(r"EA=(EBX)+(ECX)\times 4+8", markdown)
        self.assertNotIn("C4AMATHTOKEN", markdown)

    def test_source_delimiters_recover_tex_after_katex_render(self):
        source = extract_source_formulas(SOURCE_FIXTURE)
        self.assertEqual(len(source), 2)
        self.assertEqual(
            source[0]["tex"], r"EA=(\mathrm{EBX})+(\mathrm{ECX})\times 4+8"
        )
        self.assertTrue(source[0]["display"])
        self.assertFalse(source[1]["display"])

        soup = BeautifulSoup(RENDERED_NO_TEX, "html.parser")
        article, _ = select_article(soup)
        formulas, replacements = replace_math_with_tokens(article, source)
        self.assertEqual(len(formulas), 1)
        self.assertTrue(formulas[0]["recovered"])
        self.assertEqual(formulas[0]["tex_source"], "pre-render-source")
        self.assertEqual(formulas[0]["tex"], source[0]["tex"])

        markdown = render_article_markdown(article, replacements)
        self.assertIn(source[0]["tex"], markdown)

    def test_inline_svg_is_exported_and_decorative_svg_is_skipped(self):
        article, _ = self.article()
        with tempfile.TemporaryDirectory() as tmp:
            manifest, skipped = export_inline_svgs(article, Path(tmp))
            self.assertEqual(skipped, 1)
            self.assertEqual(len(manifest), 1)
            svg_path = Path(tmp) / manifest[0]["local_path"]
            self.assertTrue(svg_path.exists())
            self.assertIn("ISA layers", svg_path.read_text(encoding="utf-8"))
            images = article.find_all("img")
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].get("src"), "assets/svg/figure-001.svg")

    def test_links_are_made_absolute(self):
        article, _ = self.article()
        normalize_links(article, "https://csgraduates.com/docs/page/")
        self.assertEqual(
            article.find("a").get("href"),
            "https://csgraduates.com/quiz/1/",
        )


if __name__ == "__main__":
    unittest.main()
