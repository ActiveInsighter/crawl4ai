#!/usr/bin/env python3
"""Fast HTTP-first archive pipeline for mostly-static documentation sites.

The target site already serves article content, KaTeX DOM and inline SVG in the
HTTP response. Avoid launching Chromium in Crawl4AI for every page. Fetch HTML
concurrently, reuse the existing high-fidelity Crawl4AI markdown postprocessor,
and use one shared Chromium only to replay the fetched HTML and print PDFs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import Browser, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

import crawl_site as core
from crawl_site_fast import discover_by_links, discover_from_sitemaps
from crawl_test import build_high_fidelity_bundle, extract_source_formulas, text_value


BLOCKED_RESOURCE_TYPES = {"script", "media", "websocket", "eventsource"}


def _title_from_html(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        if title:
            return title
    heading = soup.select_one(".td-content h1, main h1, article h1, h1")
    if heading:
        title = heading.get_text(" ", strip=True)
        if title:
            return title
    return fallback


def _prepare_browser_html(html: str, page_url: str) -> str:
    """Inject a base URL so relative CSS/image/font references still resolve."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.html is None:
        wrapper = BeautifulSoup("<html><head></head><body></body></html>", "html.parser")
        wrapper.body.append(soup)
        soup = wrapper
    if soup.head is None:
        head = soup.new_tag("head")
        soup.html.insert(0, head)
    else:
        head = soup.head
    for old in head.find_all("base"):
        old.decompose()
    base = soup.new_tag("base", href=page_url)
    head.insert(0, base)
    return str(soup)


def _extract_links_and_media(html: str, base_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    origin = urlsplit(base_url).netloc.lower()
    internal: list[dict[str, str]] = []
    external: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, text_value(anchor.get("href")))
        if not href.startswith(("http://", "https://")) or href in seen_links:
            continue
        seen_links.add(href)
        item = {"href": href, "text": anchor.get_text(" ", strip=True)}
        if urlsplit(href).netloc.lower() == origin:
            internal.append(item)
        else:
            external.append(item)

    images: list[dict[str, str]] = []
    for image in soup.find_all("img"):
        src = text_value(image.get("src") or image.get("data-src"))
        if src:
            images.append(
                {
                    "src": urljoin(base_url, src),
                    "alt": text_value(image.get("alt")),
                }
            )
    return {"internal": internal, "external": external}, {"images": images}


async def _fetch_html(
    url: str,
    semaphore: asyncio.Semaphore,
    retries: int,
) -> tuple[str, str, float]:
    started = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with semaphore:
                html, resolved = await asyncio.to_thread(core._fetch_text, url)
            if not html.strip():
                raise RuntimeError("empty HTTP response")
            return html, resolved, time.perf_counter() - started
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
    assert last_error is not None
    raise last_error


async def _process_page(
    index: int,
    url: str,
    archive_root: Path,
    article_selector: str,
    fetch_semaphore: asyncio.Semaphore,
    process_semaphore: asyncio.Semaphore,
    retries: int,
) -> tuple[dict[str, Any], str | None]:
    page_dir = core.output_dir_for_url(url, archive_root)
    page_dir.mkdir(parents=True, exist_ok=True)
    output_rel = core.relative_page_dir(page_dir, archive_root)

    try:
        html, resolved, fetch_seconds = await _fetch_html(url, fetch_semaphore, retries)
    except Exception as exc:
        record = {
            "index": index,
            "url": url,
            "resolved_url": url,
            "title": None,
            "success": False,
            "error_message": str(exc),
            "output_dir": output_rel,
            "http_fetch_failed": True,
        }
        (page_dir / "metadata.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record, None

    title = _title_from_html(html, url)
    (page_dir / "source.html").write_text(html, encoding="utf-8")
    # Compatibility with the previous archive layout. For this static site the
    # HTTP source already contains the rendered KaTeX/SVG article DOM.
    (page_dir / "rendered.html").write_text(html, encoding="utf-8")

    links, media = _extract_links_and_media(html, resolved)
    (page_dir / "links.json").write_text(
        json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (page_dir / "media.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    source_formulas = extract_source_formulas(html, preferred_selector=article_selector)
    fidelity: dict[str, Any] = {}
    process_started = time.perf_counter()
    try:
        async with process_semaphore:
            fidelity = await build_high_fidelity_bundle(
                rendered_html=html,
                base_url=resolved,
                output_dir=page_dir,
                preferred_selector=article_selector,
                source_formulas=source_formulas,
            )
        success = True
        error_message = ""
    except Exception as exc:
        success = False
        error_message = f"postprocess failed: {exc}"

    record = {
        "index": index,
        "url": url,
        "resolved_url": resolved,
        "title": title,
        "success": success,
        "status_code": 200 if success else None,
        "error_message": error_message,
        "output_dir": output_rel,
        "http_fetch_seconds": round(fetch_seconds, 3),
        "postprocess_seconds": round(time.perf_counter() - process_started, 3),
        "source_html_chars": len(html),
        **fidelity,
    }
    (page_dir / "metadata.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record, html if success else None


async def _capture_static_page(
    browser: Browser,
    url: str,
    html: str,
    page_dir: Path,
    semaphore: asyncio.Semaphore,
    asset_wait_ms: int,
) -> dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        async def route_handler(route):
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)
        try:
            prepared = _prepare_browser_html(html, url)
            await page.set_content(prepared, wait_until="domcontentloaded", timeout=20_000)
            try:
                await page.wait_for_function(
                    "Array.from(document.images).every((img) => img.complete)",
                    timeout=asset_wait_ms,
                )
            except PlaywrightTimeoutError:
                pass
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass

            katex = page.locator(".katex")
            katex_count = await katex.count()
            katex_texts = [
                (await katex.nth(i).inner_text()).strip()
                for i in range(min(katex_count, 50))
            ]
            live_html = await page.content()
            (page_dir / "browser-rendered.html").write_text(live_html, encoding="utf-8")

            await page.emulate_media(media="print")
            pdf_path = page_dir / "browser-rendered.pdf"
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "12mm", "right": "12mm", "bottom": "12mm", "left": "12mm"},
            )
            return {
                "katex_nodes": katex_count,
                "katex_texts": katex_texts,
                "browser_rendered_html_chars": len(live_html),
                "pdf_bytes": pdf_path.stat().st_size,
                "browser_export_seconds": round(time.perf_counter() - started, 3),
                "browser_mode": "replay-http-html",
            }
        finally:
            await page.close()


async def archive_site(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    archive_root = args.output_dir
    archive_root.mkdir(parents=True, exist_ok=True)

    discovery = await discover_from_sitemaps(args.base_url)
    urls = list(discovery["urls"])
    discovery_method = "sitemap"
    if not urls:
        urls = await discover_by_links(args.base_url, max_urls=args.discovery_limit)
        discovery_method = "link-bfs"

    if args.respect_robots:
        urls = [
            url
            for url in urls
            if core.allowed_by_robots(url, discovery["robots_text"], discovery["robots_url"])
        ]

    root_url = core.normalize_url(args.base_url, args.base_url)
    if root_url and root_url not in urls:
        urls.insert(0, root_url)
    urls = sorted(set(urls), key=lambda value: (urlsplit(value).path.count("/"), urlsplit(value).path))
    discovered_total = len(urls)
    if args.max_pages > 0:
        urls = urls[: args.max_pages]

    discovery_summary = {
        "base_url": args.base_url,
        "method": discovery_method,
        "discovered_total": discovered_total,
        "selected_total": len(urls),
        "sitemaps": discovery["sitemaps"],
        "sitemap_errors": discovery["errors"],
        "respect_robots": args.respect_robots,
        "pipeline": "http-first-static",
    }
    (archive_root / "discovery.json").write_text(
        json.dumps(discovery_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")

    fetch_semaphore = asyncio.Semaphore(args.fetch_concurrency)
    process_semaphore = asyncio.Semaphore(args.process_concurrency)
    page_tasks = [
        _process_page(
            index,
            url,
            archive_root,
            args.article_selector,
            fetch_semaphore,
            process_semaphore,
            args.retries,
        )
        for index, url in enumerate(urls, start=1)
    ]
    page_results = await asyncio.gather(*page_tasks)
    records = [item[0] for item in page_results]

    if not args.skip_pdf:
        targets = [
            (record, html)
            for record, (_same_record, html) in zip(records, page_results)
            if record.get("success") and html
        ]
        pdf_semaphore = asyncio.Semaphore(args.pdf_concurrency)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            pdf_tasks = [
                _capture_static_page(
                    browser,
                    record["url"],
                    html,
                    archive_root / record["output_dir"],
                    pdf_semaphore,
                    args.asset_wait_ms,
                )
                for record, html in targets
            ]
            pdf_results = await asyncio.gather(*pdf_tasks, return_exceptions=True)
            await browser.close()

        for (record, _html), pdf_result in zip(targets, pdf_results):
            page_dir = archive_root / record["output_dir"]
            if isinstance(pdf_result, Exception):
                record["browser_export_error"] = str(pdf_result)
            else:
                record.update(pdf_result)
            (page_dir / "metadata.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    failures = [
        record
        for record in records
        if not record.get("success") or record.get("browser_export_error")
    ]
    (archive_root / "tree.json").write_text(
        json.dumps(core.build_tree(records), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "pages.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    core.write_catalog(records, archive_root)

    summary = {
        **discovery_summary,
        "archived_success": sum(1 for record in records if record.get("success")),
        "archived_failed": sum(1 for record in records if not record.get("success")),
        "browser_export_failed": sum(1 for record in records if record.get("browser_export_error")),
        "pdf_enabled": not args.skip_pdf,
        "fetch_concurrency": args.fetch_concurrency,
        "process_concurrency": args.process_concurrency,
        "pdf_concurrency": args.pdf_concurrency,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "output_dir": str(archive_root),
    }
    (archive_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not records:
        raise RuntimeError("No document URLs were discovered")
    if summary["archived_success"] == 0:
        raise RuntimeError("All discovered pages failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=core.DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--article-selector", default=core.DEFAULT_ARTICLE_SELECTOR)
    parser.add_argument("--max-pages", type=int, default=0, help="0 means all discovered pages")
    parser.add_argument("--discovery-limit", type=int, default=5000)
    parser.add_argument("--fetch-concurrency", type=int, default=12)
    parser.add_argument("--process-concurrency", type=int, default=8)
    parser.add_argument("--pdf-concurrency", type=int, default=4)
    parser.add_argument("--asset-wait-ms", type=int, default=8000)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--ignore-robots", dest="respect_robots", action="store_false")
    parser.set_defaults(respect_robots=True)
    return parser.parse_args()


def main() -> None:
    asyncio.run(archive_site(parse_args()))


if __name__ == "__main__":
    main()
