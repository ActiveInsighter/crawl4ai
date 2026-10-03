"""Content validation and portable MDX conversion for 408 annual papers."""

from __future__ import annotations

import html
import re
from collections import Counter
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify


def validate_article(source: str, year: int) -> dict:
    soup = BeautifulSoup(source, "html.parser")
    article = soup.select_one(".td-content")
    if article is None:
        raise ValueError(f"{year}: article missing")
    numbers = [int(h.get_text(strip=True)) for h in article.select("h5")
               if h.get_text(strip=True).isdigit()]
    if numbers != list(range(1, 48)):
        raise ValueError(f"{year}: expected ordered questions 1..47, got {numbers}")
    for heading in article.select("h5"):
        if not heading.get_text(strip=True).isdigit():
            continue
        stem = []
        for node in heading.next_siblings:
            if isinstance(node, Tag) and (node.name == "h5" or set(node.get("class", [])) & {
                    "choice-container", "answer-container", "solution-detail"}):
                break
            stem.append(str(node))
        content = BeautifulSoup("".join(stem), "html.parser")
        text = re.sub(r"[（(]本题满分\s*\d+\s*分[）)]", "", content.get_text(strip=True))
        if not text and not content.select("img, svg, .katex, math"):
            raise ValueError(f"{year}: question {heading.get_text(strip=True)} has no stem")
    for selector, count in ((".correct-answer-text", 40), (".explanation", 40),
                            (".solution-detail", 7)):
        nodes = article.select(selector)
        if len(nodes) != count or any(not n.get_text(strip=True) for n in nodes):
            raise ValueError(f"{year}: incomplete {selector}; expected {count}")
        assignments = Counter(int(node.find_previous("h5").get_text(strip=True)) for node in nodes)
        expected = range(41, 48) if selector == ".solution-detail" else range(1, 41)
        if assignments != Counter(expected):
            raise ValueError(f"{year}: answers/solutions assigned to wrong questions")
    options = article.select(".choice-option, .choice-option-inline")
    assignments = Counter(int(node.find_previous("h5").get_text(strip=True)) for node in options)
    if assignments != Counter({q: 4 for q in range(1, 41)}):
        raise ValueError(f"{year}: each choice question must have four options")
    for option in options:
        text = option.get_text(strip=True)
        if not text and not option.select("img, svg, .katex, math"):
            raise ValueError(f"{year}: empty option")
        choice_text = option.select_one(".choice-text")
        if choice_text is not None and not choice_text.get_text(strip=True) and not choice_text.select("img, svg, .katex, math"):
            raise ValueError(f"{year}: empty option content")
    return {"questions": 47, "choice_answers": 40, "choice_explanations": 40,
            "solutions": 7}


def escape_mdx_prose(markdown: str) -> str:
    # Code fences and inline code must retain their literal source characters.
    pieces = re.split(r"(```[\s\S]*?```|`+[^`\n]*?`+)", markdown)
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(r"(?<!\\)([{}<])", r"\\\1", pieces[index])
        pieces[index] = re.sub(r"(?m)^(import|export)(?=\s)", r"\\\1", pieces[index])
    return "".join(pieces)


def convert_article(source: str, year: int, asset_sources: dict[str, str]) -> str:
    soup = BeautifulSoup(source, "html.parser")
    article = soup.select_one(".td-content")
    if article is None:
        raise ValueError("article missing")
    for node in article.select("script, style, noscript, template, nav, footer, "
                               ".td-page-meta, .quiz-tag-container, .quiz-actions, "
                               "button, input, .feedback-area"):
        node.decompose()
    for heading in article.select("h5"):
        if heading.get_text(strip=True).isdigit():
            heading.name = "h2"
            heading.string = f"第 {heading.get_text(strip=True)} 题"
    for option in article.select(".choice-option, .choice-option-inline"):
        option.name = "p"
    # markdownify's code_language_callback recognizes highlighted C snippets.
    def code_language(el):
        code = el.find("code")
        if code:
            return code.get("data-lang") or next(
                (c.removeprefix("language-") for c in code.get("class", [])
                 if c.startswith("language-")), "")
        return ""

    image_tokens = {}
    for index, image in enumerate(article.select("img")):
        src = str(image.get("src", ""))
        local = asset_sources.get(src, src)
        if not local.startswith(("./assets/", "assets/")) or ".." in local.split("/"):
            raise ValueError(f"unlocalized image: {src}")
        alt = html.escape(str(image.get("alt", "图示")), quote=True)
        alt = alt.replace("{", "&#123;").replace("}", "&#125;")
        token = f"QUIZMDXIMAGE{index:05d}END"
        dimensions = ""
        if image.get("data-formula"):
            dimensions = ' style={{ display: "inline-block", verticalAlign: "middle", maxWidth: "100%" }}'
        image_tokens[token] = f'<img src="{html.escape(local, quote=True)}" alt="{alt}"{dimensions} />'
        image.replace_with(NavigableString(token))
    if article.select("svg, .katex, math, mjx-container"):
        raise ValueError("formula/diagram DOM must be exported before MDX conversion")
    markdown = markdownify(str(article), heading_style="ATX", bullets="-",
                          code_language_callback=code_language).strip()
    markdown = escape_mdx_prose(markdown)
    for token, markup in image_tokens.items():
        markdown = markdown.replace(token, markup)
    frontmatter = (f'---\ntitle: "{year} 年 408 真题"\nyear: {year}\n'
                   f'source: "https://csgraduates.com/study_methods/408quiz/{year}/"\n---\n\n')
    return frontmatter + markdown + "\n"
