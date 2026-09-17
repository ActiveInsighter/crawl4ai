import unittest

from bs4 import BeautifulSoup

from scripts.export_rendered import prepare_standalone_html


class RenderExportTests(unittest.TestCase):
    def test_adds_base_and_preserves_katex_dom(self):
        rendered = """
        <html>
          <head><link rel="stylesheet" href="/css/site.css"></head>
          <body>
            <div class="katex-display"><span class="katex"><span class="katex-html">EA = EBX + 4</span></span></div>
          </body>
        </html>
        """
        output = prepare_standalone_html(
            rendered,
            "https://csgraduates.com/constitution_principle/instruction/concepts/",
        )
        soup = BeautifulSoup(output, "html.parser")
        self.assertEqual(
            soup.find("base").get("href"),
            "https://csgraduates.com/constitution_principle/instruction/concepts/",
        )
        self.assertIsNotNone(soup.select_one(".katex"))
        self.assertEqual(soup.find("link").get("href"), "/css/site.css")


if __name__ == "__main__":
    unittest.main()
