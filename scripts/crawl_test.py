#!/usr/bin/env python3
"""Crawl one page with Crawl4AI and persist inspectable artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

DEFAULT_URL = "https://csgraduates.com/constitution_principle/instruction/concepts/"


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


async def crawl(url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=120_000,
        remove_overlay_elements=True,
        excluded_tags=["nav", "footer"],
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    raw_markdown, fit_markdown = markdown_parts(getattr(result, "markdown", None))
    cleaned_html = text_value(getattr(result, "cleaned_html", ""))
    links = getattr(result, "links", {}) or {}
    media = getattr(result, "media", {}) or {}
    metadata = getattr(result, "metadata", {}) or {}

    summary = {
        "requested_url": url,
        "resolved_url": text_value(getattr(result, "url", url)),
        "success": bool(getattr(result, "success", False)),
        "status_code": getattr(result, "status_code", None),
        "error_message": text_value(getattr(result, "error_message", "")),
        "title": metadata.get("title") if isinstance(metadata, dict) else None,
        "markdown_chars": len(raw_markdown),
        "fit_markdown_chars": len(fit_markdown),
        "cleaned_html_chars": len(cleaned_html),
        "internal_links": len(links.get("internal", [])) if isinstance(links, dict) else 0,
        "external_links": len(links.get("external", [])) if isinstance(links, dict) else 0,
        "images": len(media.get("images", [])) if isinstance(media, dict) else 0,
    }

    (output_dir / "content.md").write_text(raw_markdown, encoding="utf-8")
    if fit_markdown:
        (output_dir / "fit-content.md").write_text(fit_markdown, encoding="utf-8")
    (output_dir / "cleaned.html").write_text(cleaned_html, encoding="utf-8")
    (output_dir / "links.json").write_text(
        json.dumps(links, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "media.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if not summary["success"]:
        raise RuntimeError(summary["error_message"] or "Crawl4AI reported an unsuccessful crawl")
    if not raw_markdown.strip():
        raise RuntimeError("Crawl succeeded but produced empty Markdown")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Page URL to crawl")
    parser.add_argument(
        "--output-dir", default="artifacts/crawl", type=Path, help="Output directory"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(crawl(args.url, args.output_dir))


if __name__ == "__main__":
    main()
