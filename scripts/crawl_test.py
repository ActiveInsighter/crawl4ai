#!/usr/bin/env python3
"""Crawl one page with Crawl4AI and export a high-fidelity, AI-friendly bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

DEFAULT_URL = "https://csgraduates.com/constitution_principle/instruction/concepts/"
ARTICLE_SELECTORS = (
    ".td-content",
    "main article",
    "article",
    "main[role='main']",
    "[role='main']",
    "main",
)
MATH_ATTRS = ("data-tex", "data-latex", "data-math", "alttext", "aria-label")
SAFE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
}


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def markdown_parts(markdown: Any) -> tuple[str, str]:
    """Support both string markdown and MarkdownGenerationResult objects."""
    if markdown is None:
        return "", ""
    if isinstance(markdown, str):
        return markdown, ""
    raw = text_value(getattr(markdown, "raw_markdown", ""))
    fit = text_value(getattr(markdown, "fit_markdown", ""))
    if not raw:
        raw = text_value(markdown)
    return raw, fit


def select_article(
    soup: BeautifulSoup, preferred_selector: str | None = None
) -> tuple[Tag, str]:
    """Pick the largest likely article container, preferring known documentation selectors."""
    selectors = ((preferred_selector,) if preferred_selector else ()) + ARTICLE_SELECTORS
    seen: set[int] = set()
    candidates: list[tuple[int, int, str, Tag]] = []

    for priority, selector in enumerate(selectors):
        if not selector:
            continue
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes:
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            text_len = len(node.get_text(" ", strip=True))
            if text_len:
                candidates.append((text_len, -priority, selector, node))

    if candidates:
        _, _, selector, node = max(candidates, key=lambda item: (item[0], item[1]))
        return node, selector

    if soup.body:
        return soup.body, "body"
    return soup, "document"


def clean_article(article: Tag) -> None:
    """Remove non-content elements without flattening educational structure."""
    for node in article.select(
        "script, style, noscript, template, nav, footer, "
        ".td-page-meta, .td-toc, .feedback--answer, .taxonomy-terms-cloud"
    ):
        node.decompose()


def normalize_links(article: Tag, base_url: str) -> None:
    for anchor in article.find_all("a", href=True):
        href = text_value(anchor.get("href")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        anchor["href"] = urljoin(base_url, href)


def extract_tex(node: Tag) -> str:
    annotation = node.select_one('annotation[encoding="application/x-tex"]')
    if annotation:
        tex = annotation.get_text("", strip=True)
        if tex:
            return tex

    for attr in MATH_ATTRS:
        value = node.get(attr)
        if value:
            candidate = text_value(value).strip()
            if candidate:
                return candidate

    script = node.find("script", attrs={"type": re.compile(r"^math/tex")})
    if script:
        tex = script.get_text("", strip=True)
        if tex:
            return tex

    return ""


def is_display_math(node: Tag) -> bool:
    classes = set(node.get("class") or [])
    if "katex-display" in classes:
        return True
    parent = node.find_parent(class_="katex-display")
    if parent is not None:
        return True
    display = text_value(node.get("display")).lower()
    return display in {"block", "true"}


def replace_math_with_tokens(article: Tag) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Replace recoverable KaTeX/MathJax/MathML with tokens before Markdown conversion."""
    formulas: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    processed: set[int] = set()
    candidates = list(article.select(".katex, mjx-container, math"))

    for node in candidates:
        if not isinstance(node, Tag) or node.parent is None:
            continue

        container = node
        if "katex" in (node.get("class") or []):
            display_parent = node.find_parent(class_="katex-display")
            if display_parent is not None:
                container = display_parent

        marker = id(container)
        if marker in processed or container.parent is None:
            continue
        processed.add(marker)

        tex = extract_tex(node) or extract_tex(container)
        rendered_text = node.get_text(" ", strip=True)
        display = is_display_math(container)

        entry: dict[str, Any] = {
            "index": len(formulas) + 1,
            "display": display,
            "tex": tex,
            "rendered_text": rendered_text,
            "recovered": bool(tex),
        }

        if tex:
            token = f"C4AMATHTOKEN{len(formulas) + 1:04d}END"
            replacement = f"$$\n{tex}\n$$" if display else f"${tex}$"
            replacements[token] = replacement
            container.replace_with(NavigableString(token))
            entry["token"] = token

        formulas.append(entry)

    return formulas, replacements


def _safe_label(value: str, default: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:240] if value else default


def export_inline_svgs(article: Tag, output_dir: Path) -> list[dict[str, Any]]:
    svg_dir = output_dir / "assets" / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    for index, svg in enumerate(list(article.find_all("svg")), start=1):
        if svg.parent is None:
            continue
        if not svg.get("xmlns"):
            svg["xmlns"] = "http://www.w3.org/2000/svg"

        title = svg.find("title")
        desc = svg.find("desc")
        title_text = title.get_text(" ", strip=True) if title else ""
        desc_text = desc.get_text(" ", strip=True) if desc else ""
        alt = _safe_label(title_text or desc_text, f"Inline SVG figure {index}")

        filename = f"figure-{index:03d}.svg"
        path = svg_dir / filename
        path.write_text(str(svg), encoding="utf-8")

        relative = PurePosixPath("assets", "svg", filename).as_posix()
        image = article.new_tag("img", src=relative, alt=alt)
        svg.replace_with(image)

        manifest.append(
            {
                "index": index,
                "local_path": relative,
                "title": title_text,
                "description": desc_text,
                "chars": path.stat().st_size,
            }
        )

    return manifest


def _source_from_image(img: Tag) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = text_value(img.get(attr)).strip()
        if value:
            return value

    srcset = text_value(img.get("srcset")).strip()
    if srcset:
        first = srcset.split(",", 1)[0].strip()
        if first:
            return first.split()[0]
    return ""


def _filename_for_url(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    raw_name = unquote(Path(parsed.path).name) or "image"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in SAFE_EXTENSIONS:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
        suffix = guessed if guessed in SAFE_EXTENSIONS else ".bin"

    stem = Path(raw_name).stem or "image"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "image"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{stem[:80]}-{digest}{suffix}"


def _download_binary(url: str, referer: str) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0 Safari/537.36"
            ),
            "Referer": referer,
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
        content_type = response.headers.get_content_type() or ""
    return payload, content_type


async def localize_images(
    article: Tag, base_url: str, output_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    image_dir = output_dir / "assets" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    cache: dict[str, str] = {}

    for img in article.find_all("img"):
        source = _source_from_image(img)
        if not source:
            continue
        if source.startswith(("data:", "blob:", "assets/")):
            continue

        absolute = urljoin(base_url, source)
        if not absolute.startswith(("http://", "https://")):
            continue

        if absolute in cache:
            img["src"] = cache[absolute]
            img.attrs.pop("srcset", None)
            continue

        try:
            payload, content_type = await asyncio.to_thread(
                _download_binary, absolute, base_url
            )
            filename = _filename_for_url(absolute, content_type)
            path = image_dir / filename
            path.write_bytes(payload)
            relative = PurePosixPath("assets", "images", filename).as_posix()
            cache[absolute] = relative
            img["src"] = relative
            img.attrs.pop("srcset", None)
            for attr in ("data-src", "data-original", "data-lazy-src"):
                img.attrs.pop(attr, None)

            manifest.append(
                {
                    "source_url": absolute,
                    "local_path": relative,
                    "alt": text_value(img.get("alt")),
                    "content_type": content_type,
                    "bytes": len(payload),
                }
            )
        except Exception as exc:
            img["src"] = absolute
            failures.append({"source_url": absolute, "error": str(exc)})

    return manifest, failures


def render_article_markdown(article: Tag, replacements: dict[str, str]) -> str:
    generator = DefaultMarkdownGenerator(options={"ignore_links": False})
    result = generator.generate_markdown(
        input_html=str(article),
        base_url="",
        citations=False,
    )
    markdown = text_value(getattr(result, "raw_markdown", result))
    for token, latex in replacements.items():
        markdown = markdown.replace(token, latex)
    return markdown.strip() + "\n"


async def build_high_fidelity_bundle(
    source_html: str,
    base_url: str,
    output_dir: Path,
    preferred_selector: str | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(source_html, "html.parser")
    article, selector = select_article(soup, preferred_selector)
    clean_article(article)
    normalize_links(article, base_url)

    formulas, replacements = replace_math_with_tokens(article)
    svgs = export_inline_svgs(article, output_dir)
    images, image_failures = await localize_images(article, base_url, output_dir)
    markdown = render_article_markdown(article, replacements)

    (output_dir / "article.html").write_text(str(article), encoding="utf-8")
    (output_dir / "content.md").write_text(markdown, encoding="utf-8")
    (output_dir / "formulas.json").write_text(
        json.dumps(formulas, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "assets.json").write_text(
        json.dumps(
            {
                "images": images,
                "image_failures": image_failures,
                "inline_svgs": svgs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "article_selector": selector,
        "article_html_chars": len(str(article)),
        "content_markdown_chars": len(markdown),
        "formulas_total": len(formulas),
        "formulas_recovered": sum(1 for item in formulas if item["recovered"]),
        "inline_svgs": len(svgs),
        "downloaded_images": len(images),
        "image_download_failures": len(image_failures),
    }


async def crawl(
    url: str,
    output_dir: Path,
    article_selector: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=120_000,
        delay_before_return_html=0.5,
        remove_overlay_elements=True,
        excluded_tags=["nav", "footer"],
        markdown_generator=DefaultMarkdownGenerator(
            content_source="cleaned_html",
            options={"ignore_links": False},
        ),
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    crawl4ai_markdown, fit_markdown = markdown_parts(
        getattr(result, "markdown", None)
    )
    source_html = text_value(getattr(result, "html", ""))
    cleaned_html = text_value(getattr(result, "cleaned_html", ""))
    links = getattr(result, "links", {}) or {}
    media = getattr(result, "media", {}) or {}
    metadata = getattr(result, "metadata", {}) or {}
    resolved_url = text_value(getattr(result, "url", url)) or url

    (output_dir / "raw.html").write_text(source_html, encoding="utf-8")
    (output_dir / "cleaned.html").write_text(cleaned_html, encoding="utf-8")
    (output_dir / "crawl4ai-raw.md").write_text(
        crawl4ai_markdown, encoding="utf-8"
    )
    if fit_markdown:
        (output_dir / "fit-content.md").write_text(
            fit_markdown, encoding="utf-8"
        )
    (output_dir / "links.json").write_text(
        json.dumps(links, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "media.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    fidelity: dict[str, Any] = {}
    if bool(getattr(result, "success", False)) and source_html:
        fidelity = await build_high_fidelity_bundle(
            source_html=source_html,
            base_url=resolved_url,
            output_dir=output_dir,
            preferred_selector=article_selector,
        )

    summary = {
        "requested_url": url,
        "resolved_url": resolved_url,
        "success": bool(getattr(result, "success", False)),
        "status_code": getattr(result, "status_code", None),
        "error_message": text_value(getattr(result, "error_message", "")),
        "title": metadata.get("title") if isinstance(metadata, dict) else None,
        "source_html_chars": len(source_html),
        "cleaned_html_chars": len(cleaned_html),
        "crawl4ai_raw_markdown_chars": len(crawl4ai_markdown),
        "internal_links": (
            len(links.get("internal", [])) if isinstance(links, dict) else 0
        ),
        "external_links": (
            len(links.get("external", [])) if isinstance(links, dict) else 0
        ),
        "crawl4ai_images": (
            len(media.get("images", [])) if isinstance(media, dict) else 0
        ),
        **fidelity,
    }

    (output_dir / "metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if not summary["success"]:
        raise RuntimeError(
            summary["error_message"]
            or "Crawl4AI reported an unsuccessful crawl"
        )
    if not crawl4ai_markdown.strip():
        raise RuntimeError("Crawl succeeded but produced empty Crawl4AI Markdown")
    content_path = output_dir / "content.md"
    if not content_path.exists() or not content_path.read_text(
        encoding="utf-8"
    ).strip():
        raise RuntimeError("High-fidelity post-processing produced empty Markdown")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Page URL to crawl")
    parser.add_argument(
        "--output-dir",
        default="artifacts/crawl",
        type=Path,
        help="Output directory",
    )
    parser.add_argument(
        "--article-selector",
        default="",
        help="Optional CSS selector for the article root; auto-detected by default",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        crawl(
            args.url,
            args.output_dir,
            article_selector=args.article_selector or None,
        )
    )


if __name__ == "__main__":
    main()
