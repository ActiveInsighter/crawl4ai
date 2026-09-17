#!/usr/bin/env python3
"""Preserve rendered browser HTML and print the live page to PDF."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


def prepare_standalone_html(rendered_html: str, base_url: str) -> str:
    """Add an explicit base URL so a saved Crawl4AI DOM can be replayed."""
    soup = BeautifulSoup(rendered_html, "html.parser")

    if soup.html is None:
        wrapper = BeautifulSoup(
            "<!doctype html><html><head></head><body></body></html>",
            "html.parser",
        )
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


async def export_live_page(
    url: str,
    output_html: Path,
    output_pdf: Path,
) -> dict[str, Any]:
    """Navigate like Chrome, wait for rendering, save page.content(), then print it."""
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        response = await page.goto(url, wait_until="networkidle", timeout=120_000)

        # The target uses KaTeX client-side rendering. If this page contains KaTeX,
        # give it a chance to finish before serializing/printing.
        try:
            await page.wait_for_selector(".katex", state="attached", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(500)

        katex = page.locator(".katex")
        katex_count = await katex.count()
        katex_texts = [
            (await katex.nth(i).inner_text()).strip()
            for i in range(min(katex_count, 20))
        ]
        svg_count = await page.locator("svg").count()
        image_count = await page.locator("img").count()

        browser_rendered_html = await page.content()
        output_html.write_text(browser_rendered_html, encoding="utf-8")

        # This is the important part: print the same live page context that Chrome
        # has already rendered. No HTML->Markdown->HTML round trip is involved.
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(output_pdf),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top": "12mm",
                "right": "12mm",
                "bottom": "12mm",
                "left": "12mm",
            },
        )
        status = response.status if response is not None else None
        final_url = page.url
        await browser.close()

    return {
        "status_code": status,
        "final_url": final_url,
        "katex_nodes": katex_count,
        "katex_texts": katex_texts,
        "svg_nodes": svg_count,
        "image_nodes": image_count,
        "browser_rendered_html_chars": len(browser_rendered_html),
        "pdf_bytes": output_pdf.stat().st_size,
    }


async def main_async(args: argparse.Namespace) -> None:
    crawl4ai_rendered_html = args.input.read_text(encoding="utf-8")
    standalone_html = prepare_standalone_html(
        crawl4ai_rendered_html,
        args.base_url,
    )

    args.output_replay_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_replay_html.write_text(standalone_html, encoding="utf-8")

    stats = await export_live_page(
        args.base_url,
        args.output_live_html,
        args.output_pdf,
    )
    stats.update(
        {
            "crawl4ai_rendered_html_chars": len(crawl4ai_rendered_html),
            "replay_html_chars": len(standalone_html),
            "base_url": args.base_url,
            "pdf_source": "live-browser-page",
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
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Crawl4AI result.html saved as rendered.html",
    )
    parser.add_argument("--base-url", required=True, help="Original page URL")
    parser.add_argument("--output-replay-html", type=Path, required=True)
    parser.add_argument("--output-live-html", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
