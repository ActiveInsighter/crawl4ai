#!/usr/bin/env python3
"""Discover and archive an entire documentation site with Crawl4AI + Chromium."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    RateLimiter,
    SemaphoreDispatcher,
)
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from playwright.async_api import Browser, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from crawl_test import (
    HTTP_USER_AGENT,
    build_high_fidelity_bundle,
    extract_source_formulas,
    markdown_parts,
    text_value,
)

DEFAULT_BASE_URL = "https://csgraduates.com/"
DEFAULT_ARTICLE_SELECTOR = ".td-content"
ARCHIVE_USER_AGENT = "Crawl4AI-Archive/1.0 (+https://github.com/ActiveInsighter/crawl4ai)"
SITEMAP_CANDIDATES = ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml")
SKIP_PATH_RE = re.compile(
    r"(?:^|/)(?:404(?:\.html)?|search|tags?|categories?|authors?)(?:/|$)",
    re.IGNORECASE,
)
NON_DOCUMENT_SUFFIXES = {
    ".7z", ".avi", ".avif", ".bin", ".bmp", ".css", ".csv", ".doc", ".docx",
    ".eot", ".epub", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".map", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".otf", ".pdf", ".png",
    ".ppt", ".pptx", ".rar", ".rss", ".svg", ".tar", ".tgz", ".ttf", ".txt",
    ".wav", ".webm", ".webp", ".woff", ".woff2", ".xml", ".zip",
}
WINDOWS_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower()


def normalize_url(url: str, base_url: str) -> str | None:
    """Canonicalize a candidate URL and reject non-document/cross-origin targets."""
    absolute = urljoin(base_url, url.strip())
    parsed = urlsplit(absolute)
    base = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if (parsed.hostname or "").lower() != (base.hostname or "").lower():
        return None

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    suffix = Path(path).suffix.lower()
    if suffix in NON_DOCUMENT_SUFFIXES:
        return None
    if suffix and suffix not in {".html", ".htm"}:
        return None
    if SKIP_PATH_RE.search(path.strip("/")):
        return None

    # Tracking/query variants should not become separate archived documents.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def safe_segment(value: str, fallback: str = "index") -> str:
    value = unquote(value).strip().strip(".")
    value = WINDOWS_BAD_CHARS_RE.sub("-", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._ ")
    if not value:
        value = fallback
    # Avoid special Windows device names.
    if value.upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        value = f"_{value}"
    return value[:120]


def output_dir_for_url(url: str, archive_root: Path) -> Path:
    path = urlsplit(url).path.strip("/")
    if not path:
        return archive_root / "pages" / "_root"
    segments = [safe_segment(part) for part in path.split("/") if part]
    if segments and Path(segments[-1]).suffix.lower() in {".html", ".htm"}:
        segments[-1] = safe_segment(Path(segments[-1]).stem)
    return archive_root / "pages" / Path(*segments)


def relative_page_dir(page_dir: Path, archive_root: Path) -> str:
    return PurePosixPath(page_dir.relative_to(archive_root)).as_posix()


def _fetch_bytes(url: str, accept: str = "*/*") -> tuple[bytes, str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": ARCHIVE_USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    with urlopen(request, timeout=40) as response:
        return response.read(), response.geturl(), response.headers.get_content_type() or ""


def _fetch_text(url: str, accept: str = "text/html,*/*") -> tuple[str, str]:
    payload, resolved, _ = _fetch_bytes(url, accept=accept)
    return payload.decode("utf-8", errors="replace"), resolved


def parse_sitemap_xml(payload: bytes) -> tuple[str, list[str]]:
    root = ET.fromstring(payload)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    values: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
            values.append(node.text.strip())
    return kind, values


async def discover_from_sitemaps(base_url: str) -> dict[str, Any]:
    """Read robots.txt and recursively expand sitemap indexes."""
    base_url = base_url.rstrip("/") + "/"
    robots_url = urljoin(base_url, "robots.txt")
    sitemap_queue: deque[str] = deque()
    sitemap_seen: set[str] = set()
    urls: set[str] = set()
    errors: list[dict[str, str]] = []
    robots_text = ""

    try:
        robots_text, _ = await asyncio.to_thread(_fetch_text, robots_url, "text/plain,*/*")
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                location = line.split(":", 1)[1].strip()
                if location:
                    sitemap_queue.append(urljoin(base_url, location))
    except Exception as exc:
        errors.append({"url": robots_url, "error": str(exc)})

    for name in SITEMAP_CANDIDATES:
        sitemap_queue.append(urljoin(base_url, name))

    while sitemap_queue and len(sitemap_seen) < 100:
        sitemap_url = sitemap_queue.popleft()
        if sitemap_url in sitemap_seen:
            continue
        sitemap_seen.add(sitemap_url)
        try:
            payload, resolved, _ = await asyncio.to_thread(
                _fetch_bytes,
                sitemap_url,
                "application/xml,text/xml,*/*",
            )
            kind, values = parse_sitemap_xml(payload)
            if kind == "sitemapindex":
                for value in values:
                    if _origin(value) == _origin(base_url):
                        sitemap_queue.append(value)
            elif kind == "urlset":
                for value in values:
                    normalized = normalize_url(value, base_url)
                    if normalized:
                        urls.add(normalized)
            else:
                errors.append({"url": resolved, "error": f"unsupported sitemap root: {kind}"})
        except Exception as exc:
            errors.append({"url": sitemap_url, "error": str(exc)})

    return {
        "urls": sorted(urls),
        "robots_url": robots_url,
        "robots_text": robots_text,
        "sitemaps": sorted(sitemap_seen),
        "errors": errors,
    }


async def discover_by_links(base_url: str, max_urls: int = 5000) -> list[str]:
    """Fallback same-origin BFS used only when no usable sitemap is available."""
    queue: deque[str] = deque([base_url])
    seen: set[str] = set()
    documents: set[str] = set()

    while queue and len(seen) < max_urls:
        current = queue.popleft()
        normalized = normalize_url(current, base_url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            html, resolved = await asyncio.to_thread(_fetch_text, normalized)
        except Exception:
            continue
        normalized_resolved = normalize_url(resolved, base_url)
        if normalized_resolved:
            documents.add(normalized_resolved)
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            candidate = normalize_url(text_value(anchor.get("href")), base_url)
            if candidate and candidate not in seen:
                queue.append(candidate)
    return sorted(documents)


def allowed_by_robots(url: str, robots_text: str, robots_url: str) -> bool:
    if not robots_text:
        return True
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(ARCHIVE_USER_AGENT, url)


def build_tree(page_records: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {"name": "csgraduates.com", "type": "root", "children": {}}
    for record in page_records:
        path = urlsplit(record["url"]).path.strip("/")
        parts = [unquote(part) for part in path.split("/") if part] or ["_root"]
        node = root
        for part in parts:
            node = node["children"].setdefault(
                part,
                {"name": part, "type": "directory", "children": {}},
            )
        node["type"] = "page"
        node["url"] = record["url"]
        node["title"] = record.get("title") or parts[-1]
        node["output_dir"] = record.get("output_dir")

    def materialize(node: dict[str, Any]) -> dict[str, Any]:
        children = node.get("children", {})
        if isinstance(children, dict):
            node["children"] = [materialize(children[key]) for key in sorted(children)]
        return node

    return materialize(root)


def write_catalog(page_records: list[dict[str, Any]], archive_root: Path) -> None:
    successful = [record for record in page_records if record.get("success")]
    successful.sort(key=lambda item: urlsplit(item["url"]).path)

    md_lines = [
        "# csgraduates.com archive catalog",
        "",
        f"Pages archived: {len(successful)}",
        "",
    ]
    jsonl_lines: list[str] = []
    all_content: list[str] = ["# csgraduates.com AI-readable archive", ""]

    for record in successful:
        page_dir = archive_root / record["output_dir"]
        markdown_path = page_dir / "content.md"
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
        rel = PurePosixPath(record["output_dir"])
        title = record.get("title") or record["url"]
        md_lines.append(
            f"- [{title}]({(rel / 'content.md').as_posix()}) — "
            f"[PDF]({(rel / 'browser-rendered.pdf').as_posix()}) — {record['url']}"
        )
        jsonl_lines.append(
            json.dumps(
                {
                    "url": record["url"],
                    "title": title,
                    "output_dir": record["output_dir"],
                    "markdown": markdown,
                },
                ensure_ascii=False,
            )
        )
        all_content.extend(
            [
                f"## {title}",
                "",
                f"Source: {record['url']}",
                "",
                markdown.rstrip(),
                "",
                "---",
                "",
            ]
        )

    (archive_root / "catalog.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    (archive_root / "site-content.jsonl").write_text(
        "\n".join(jsonl_lines) + ("\n" if jsonl_lines else ""),
        encoding="utf-8",
    )
    (archive_root / "all-content.md").write_text("\n".join(all_content), encoding="utf-8")


async def _capture_live_page(
    browser: Browser,
    url: str,
    page_dir: Path,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(500)

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
                "browser_status_code": response.status if response else None,
                "browser_final_url": page.url,
                "katex_nodes": katex_count,
                "katex_texts": katex_texts,
                "browser_rendered_html_chars": len(live_html),
                "pdf_bytes": pdf_path.stat().st_size,
            }
        finally:
            await page.close()


async def archive_site(args: argparse.Namespace) -> None:
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
            if allowed_by_robots(url, discovery["robots_text"], discovery["robots_url"])
        ]

    root_url = normalize_url(args.base_url, args.base_url)
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
    }
    (archive_root / "discovery.json").write_text(
        json.dumps(discovery_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")

    browser_config = BrowserConfig(headless=True, verbose=False)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=120_000,
        delay_before_return_html=0.5,
        remove_overlay_elements=True,
        excluded_tags=["nav", "footer"],
        stream=False,
        markdown_generator=DefaultMarkdownGenerator(
            content_source="cleaned_html",
            options={"ignore_links": False},
        ),
    )
    dispatcher = SemaphoreDispatcher(
        max_session_permit=args.crawl_concurrency,
        rate_limiter=RateLimiter(
            base_delay=(args.delay_min, args.delay_max),
            max_delay=30.0,
            max_retries=args.retries,
            rate_limit_codes=[429, 503],
        ),
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        results = await crawler.arun_many(urls=urls, config=run_config, dispatcher=dispatcher)

    records: list[dict[str, Any]] = []
    live_targets: list[tuple[str, Path, dict[str, Any]]] = []

    for index, result in enumerate(results, start=1):
        requested_url = urls[index - 1] if index - 1 < len(urls) else text_value(getattr(result, "url", ""))
        resolved_url = text_value(getattr(result, "url", requested_url)) or requested_url
        page_dir = output_dir_for_url(requested_url, archive_root)
        page_dir.mkdir(parents=True, exist_ok=True)
        output_rel = relative_page_dir(page_dir, archive_root)
        success = bool(getattr(result, "success", False))
        metadata = getattr(result, "metadata", {}) or {}
        title = metadata.get("title") if isinstance(metadata, dict) else None
        rendered_html = text_value(getattr(result, "html", ""))
        cleaned_html = text_value(getattr(result, "cleaned_html", ""))
        raw_markdown, fit_markdown = markdown_parts(getattr(result, "markdown", None))
        links = getattr(result, "links", {}) or {}
        media = getattr(result, "media", {}) or {}

        (page_dir / "rendered.html").write_text(rendered_html, encoding="utf-8")
        (page_dir / "cleaned.html").write_text(cleaned_html, encoding="utf-8")
        (page_dir / "crawl4ai-raw.md").write_text(raw_markdown, encoding="utf-8")
        (page_dir / "links.json").write_text(
            json.dumps(links, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        (page_dir / "media.json").write_text(
            json.dumps(media, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        if fit_markdown:
            (page_dir / "fit-content.md").write_text(fit_markdown, encoding="utf-8")

        source_formulas: list[dict[str, Any]] = []
        source_fetch_error = ""
        if args.fetch_source_html:
            try:
                source_html, _ = await asyncio.to_thread(_fetch_text, requested_url)
                (page_dir / "source.html").write_text(source_html, encoding="utf-8")
                source_formulas = extract_source_formulas(
                    source_html, preferred_selector=args.article_selector
                )
            except Exception as exc:
                source_fetch_error = str(exc)

        fidelity: dict[str, Any] = {}
        if success and rendered_html:
            fidelity = await build_high_fidelity_bundle(
                rendered_html=rendered_html,
                base_url=resolved_url,
                output_dir=page_dir,
                preferred_selector=args.article_selector,
                source_formulas=source_formulas,
            )

        record = {
            "index": index,
            "url": requested_url,
            "resolved_url": resolved_url,
            "title": title,
            "success": success,
            "status_code": getattr(result, "status_code", None),
            "error_message": text_value(getattr(result, "error_message", "")),
            "output_dir": output_rel,
            "rendered_html_chars": len(rendered_html),
            "content_markdown_chars": fidelity.get("content_markdown_chars", 0),
            "source_fetch_error": source_fetch_error,
            **fidelity,
        }
        (page_dir / "metadata.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        records.append(record)
        if success:
            live_targets.append((requested_url, page_dir, record))

    # Preserve the exact browser-rendered DOM and print each page from that live context.
    if not args.skip_pdf and live_targets:
        semaphore = asyncio.Semaphore(args.pdf_concurrency)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            tasks = [
                _capture_live_page(browser, url, page_dir, semaphore)
                for url, page_dir, _record in live_targets
            ]
            live_results = await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()

        for (_url, page_dir, record), live_result in zip(live_targets, live_results):
            if isinstance(live_result, Exception):
                record["browser_export_error"] = str(live_result)
            else:
                record.update(live_result)
            (page_dir / "metadata.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

    failures = [record for record in records if not record.get("success") or record.get("browser_export_error")]
    tree = build_tree(records)
    (archive_root / "tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "pages.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (archive_root / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_catalog(records, archive_root)

    summary = {
        **discovery_summary,
        "archived_success": sum(1 for record in records if record.get("success")),
        "archived_failed": sum(1 for record in records if not record.get("success")),
        "browser_export_failed": sum(1 for record in records if record.get("browser_export_error")),
        "pdf_enabled": not args.skip_pdf,
        "fetch_source_html": args.fetch_source_html,
        "output_dir": str(archive_root),
    }
    (archive_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not records:
        raise RuntimeError("No document URLs were discovered")
    if summary["archived_success"] == 0:
        raise RuntimeError("All discovered pages failed to crawl")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--article-selector", default=DEFAULT_ARTICLE_SELECTOR)
    parser.add_argument("--max-pages", type=int, default=0, help="0 means all discovered pages")
    parser.add_argument("--discovery-limit", type=int, default=5000)
    parser.add_argument("--crawl-concurrency", type=int, default=3)
    parser.add_argument("--pdf-concurrency", type=int, default=2)
    parser.add_argument("--delay-min", type=float, default=0.6)
    parser.add_argument("--delay-max", type=float, default=1.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--fetch-source-html", action="store_true")
    parser.add_argument("--ignore-robots", dest="respect_robots", action="store_false")
    parser.set_defaults(respect_robots=True)
    return parser.parse_args()


def main() -> None:
    asyncio.run(archive_site(parse_args()))


if __name__ == "__main__":
    main()
