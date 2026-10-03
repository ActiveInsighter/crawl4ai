import html
import hashlib
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.quiz_assets import serialize_svg
from scripts.quiz_mdx import convert_article, validate_article
from scripts.export_408_mdx import validate_package
from scripts import export_408_mdx


def complete_article():
    """An annual paper with the source site's question/answer containers."""
    questions = []
    for number in range(1, 48):
        questions.append(f'<h5 id="{number}">{number}</h5><p>题干 {number}</p>')
        if number <= 40:
            questions.append(
                '<div class="choice-container" data-answer="B">'
                '<div class="quiz-tag-container">知识点标签</div>'
                '<div class="choice-option">A. 选项一</div>'
                '<div class="choice-option">B. 选项二</div>'
                '<div class="choice-option">C. 选项三</div>'
                '<div class="choice-option">D. 选项四</div>'
                '<button class="show-answer">查看答案</button>'
                '<button class="bookmark">收藏</button>'
                '<div class="explanation" style="display:none">'
                '<strong>正确答案：<span class="correct-answer-text">B</span></strong>'
                f'<p>选择题解析 {number}</p></div></div>'
            )
        else:
            questions.append(
                '<div class="answer-container">'
                '<div class="quiz-tag-container">知识点标签</div>'
                '<button>查看答案</button><button>收藏</button></div>'
                '<div class="solution-detail" style="display:none">'
                f'<p>大题解析 {number}</p></div>'
            )
    return '<div class="td-content">' + "".join(questions) + "</div>"


class QuizMdxTests(unittest.TestCase):
    def test_accepts_all_47_questions_with_40_answers_and_7_solutions(self):
        validate_article(complete_article(), 2026)

    def test_rejects_missing_or_duplicate_question_numbers(self):
        for invalid in (
            complete_article().replace('<h5 id="16">16</h5>', ""),
            complete_article().replace('<h5 id="16">16</h5>', '<h5 id="15">15</h5>'),
        ):
            with self.subTest(html=invalid[:80]):
                with self.assertRaises(ValueError):
                    validate_article(invalid, 2026)

    def test_rejects_incomplete_answers_or_big_question_solutions(self):
        soup = BeautifulSoup(complete_article(), "html.parser")
        soup.select_one(".correct-answer-text").decompose()
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)
        soup = BeautifulSoup(complete_article(), "html.parser")
        soup.select_one(".solution-detail").decompose()
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)

    def test_rejects_a_question_with_one_missing_choice(self):
        soup = BeautifulSoup(complete_article(), "html.parser")
        soup.select_one(".choice-container .choice-option").decompose()
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)

    def test_rejects_answers_attached_to_the_wrong_question(self):
        soup = BeautifulSoup(complete_article(), "html.parser")
        containers = soup.select(".choice-container")
        answer = containers[0].select_one(".correct-answer-text").extract()
        containers[1].select_one(".explanation").append(answer)
        self.assertEqual(len(soup.select(".correct-answer-text")), 40)
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)

    def test_rejects_questions_with_all_stems_removed(self):
        soup = BeautifulSoup(complete_article(), "html.parser")
        for paragraph in soup.select(".td-content > p"):
            paragraph.decompose()
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)

    def test_rejects_choices_whose_content_is_empty(self):
        soup = BeautifulSoup(complete_article(), "html.parser")
        for option in soup.select(".choice-option"):
            option.clear()
        with self.assertRaises(ValueError):
            validate_article(str(soup), 2026)

    def test_rejects_year_discovery_missing_the_latest_known_year(self):
        with self.assertRaises(ValueError):
            export_408_mdx.validate_years(list(range(2009, 2026)))

    def test_package_rejects_mdx_body_loss_even_when_all_headings_remain(self):
        source = complete_article()
        full_mdx = convert_article(source, 2026, {})
        truncated_mdx = "\n\n".join(f"## 第 {question} 题" for question in range(1, 48)) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "sources" / "2026.html").write_text(source, encoding="utf-8")
            (root / "2026.mdx").write_text(truncated_mdx, encoding="utf-8")
            record = {
                "year": 2026, "diagram_count": 0, "formula_count": 0, "image_count": 0,
                "assets": [], "mdx_sha256": hashlib.sha256(full_mdx.encode()).hexdigest(),
            }
            (root / "sources" / "2026.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_package(root, [2026])

    def test_package_rejects_malformed_svg_even_with_a_matching_hash(self):
        source = complete_article().replace(
            '<h5 id="1">1</h5>',
            '<h5 id="1">1</h5><img src="./assets/figures/broken.svg" alt="图示" />',
        )
        malformed = b'<svg xmlns="http://www.w3.org/2000/svg"><path></svg>'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "assets" / "figures").mkdir(parents=True)
            (root / "sources" / "2026.html").write_text(source, encoding="utf-8")
            (root / "2026.mdx").write_text(convert_article(source, 2026, {}), encoding="utf-8")
            (root / "assets" / "figures" / "broken.svg").write_bytes(malformed)
            record = {
                "year": 2026, "diagram_count": 1, "formula_count": 0, "image_count": 0,
                "mdx_sha256": hashlib.sha256(convert_article(source, 2026, {}).encode()).hexdigest(),
                "assets": [{"path": "assets/figures/broken.svg",
                            "sha256": hashlib.sha256(malformed).hexdigest()}],
            }
            (root / "sources" / "2026.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(ET.ParseError):
                validate_package(root, [2026])

    def test_preserves_options_and_hidden_answers_without_site_controls(self):
        output = convert_article(complete_article(), 2026, {})
        for expected in (
            "题干 1", "A. 选项一", "B. 选项二", "C. 选项三", "D. 选项四",
            "正确答案", "选择题解析 40", "大题解析 41", "大题解析 47",
        ):
            self.assertIn(expected, output)
        for control in ("查看答案", "收藏", "知识点标签", "display:none"):
            self.assertNotIn(control, output)

    def test_literal_braces_and_comparisons_are_safe_in_mdx_prose(self):
        source = '<div class="td-content"><p>集合 {a,b}，条件 x &lt; y。</p></div>'
        output = convert_article(source, 2026, {})
        # Literal JSX/JS expression delimiters must be escaped or put in code.
        prose = re.sub(r"```[\s\S]*?```|`[^`]*`", "", output)
        self.assertNotRegex(prose, r"(?<!\\)[{}]")
        self.assertNotIn("x < y", prose)
        decoded = html.unescape(output).replace("\\{", "{").replace("\\}", "}")
        decoded = decoded.replace("\\<", "<")
        self.assertIn("{a,b}", decoded)
        self.assertIn("x < y", decoded)

    def test_c_code_remains_verbatim_inside_a_code_fence(self):
        program = "int main(void) {\n  for (int i = 0; i < 4; i++) {\n    a[i]++;\n  }\n}"
        source = (
            '<div class="td-content"><pre><code class="language-c">'
            + html.escape(program) + "</code></pre></div>"
        )
        output = convert_article(source, 2026, {})
        fences = re.findall(r"```[^\n]*\n([\s\S]*?)\n```", output)
        self.assertTrue(any(block.rstrip() == program for block in fences), output)

    def test_svg_and_bitmap_references_are_local_self_closing_images(self):
        source = '''<div class="td-content">
          <p>图示：</p><img src="https://source.test/figure.svg" alt="树 &amp; 图" />
          <p>公式：</p><img src="formula-001.svg" alt="2(2k−1)/(k+1)" />
          <img src="/scan.png" alt="扫描图" />
        </div>'''
        output = convert_article(source, 2026, {
            "https://source.test/figure.svg": "./assets/2026/figure-001.svg",
            "formula-001.svg": "./assets/2026/formula-001.svg",
            "/scan.png": "./assets/2026/image-001.png",
        })
        images = BeautifulSoup(output, "html.parser").find_all("img")
        self.assertEqual([image["src"] for image in images], [
            "./assets/2026/figure-001.svg", "./assets/2026/formula-001.svg",
            "./assets/2026/image-001.png",
        ])
        self.assertEqual([image["alt"] for image in images], [
            "树 & 图", "2(2k−1)/(k+1)", "扫描图",
        ])
        self.assertEqual(len(re.findall(r"<img\b[^>]*?/\s*>", output)), 3)
        self.assertNotIn("https://source.test", output)

    def test_svg_xml_removes_html_event_attributes_and_preserves_drawing(self):
        source = '''<svg viewBox="0 0 100 40" @click="toggle()" :class="active"
          xmlns="http://www.w3.org/2000/svg">
          <defs><marker id="arrow" markerHeight="5" refX="4"><path d="M0 0L4 2" /></marker></defs>
          <style>.edge{stroke:#333}</style>
          <path class="edge" d="M0 0L100 40" marker-end="url(#arrow)" />
          <svg x="2" y="2" width="10" height="10"><circle r="2" /></svg>
          <foreignObject x="0" y="0" width="80" height="20">
            <div xmlns="http://www.w3.org/1999/xhtml">CPU</div>
          </foreignObject>
        </svg>'''
        output = serialize_svg(source)
        root = ET.fromstring(output)
        self.assertEqual(root.attrib["viewBox"], "0 0 100 40")
        self.assertNotIn("@click", output)
        self.assertNotIn(":class", output)
        self.assertIn(".edge{stroke:#333}", output)
        self.assertEqual(len(list(root.iter("{http://www.w3.org/2000/svg}svg"))), 2)
        self.assertIsNotNone(root.find(".//{http://www.w3.org/2000/svg}foreignObject"))
        marker = root.find(".//{http://www.w3.org/2000/svg}marker")
        self.assertEqual(marker.attrib["markerHeight"], "5")
        self.assertIn('marker-end="url(#arrow)"', output)


if __name__ == "__main__":
    unittest.main()
