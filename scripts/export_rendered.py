#!/usr/bin/env python3
"""Export Crawl4AI rendered HTML as a browser-replayable HTML file and print PDF."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


def prepare_standalone_html(rendered_html: str, base_url: str) -> str:
    """Add an explicit base URL so relative CSS/images still resolve when replayed."""
    soup = BeautifulSoup(rendered_html, "html.parser")

    if soup.html is None:
        wrapper = BeautifulSoup("<!doctype html><html><head></head><body></body></html>", "html.parser")
        wrapper.body.append(BeautifulSoup(rendered_html, "html.parser"))
        soup = wrapper

    if soup.head is None:
        head = soup.new_tag("head")
        soup.html.insert(0, head)
    else:
        head = soup.head

    for old_base in head.find_all("base"):
        old_base.decompose()

    base = soup.new_tag("base", href=base_url)
    head.insert(0, base)

    if head.find("meta", attrs={"charset": True}) is None:
        meta = soup.new_tag("meta", charset="utf-8")
        head.insert(0, meta)

    return "<!doctype html>\n" + str(soup)


async def export_pdf(html: str, output_pdf: Path) -> dict[str, int]:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.set_content(html, wait_until="networkidle", timeout=120_000)

        # The page already contains KaTeX-rendered DOM. We only need its stylesheet
        # and other resources to finish loading before printing.
        katex_count = await page.locator(".katex").count()
        svg_count = await page.locator("svg").count()
        image_count = await page.locator("img").count()

        await page.emulate_media(media="print")
        await page.pdf(
            path=str(output_pdf),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "12mm", "right": "12mm", "bottom": "12mm", "left": "12mm"},
        )
        await browser.close()

    return {
        "katex_nodes": katex_count,
        "svg_nodes": svg_count,
        "image_nodes": image_count,
        "pdf_bytes": output_pdf.stat().st_size,
    }


async def main_async(args: argparse.Namespace) -> None:
    rendered_html = args.input.read_text(encoding="utf-8")
    standalone_html = prepare_standalone_html(rendered_html, args.base_url)

    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(standalone_html, encoding="utf-8")

    stats = await export_pdf(standalone_html, args.output_pdf)
    stats.update(
        {
            "source_html_chars": len(rendered_html),
            "standalone_html_chars": len(standalone_html),
            "base_url": args.base_url,
        }
    )

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Crawl4AI rendered HTML")
    parser.add_argument("--base-url", required=True, help="Original page URL")
    parser.add_argument("--output-html", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
