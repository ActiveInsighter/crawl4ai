#!/usr/bin/env python3
"""Archive one AI-oriented shard with full browser-rendered HTML and no PDF."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import ai_archive_core as core
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


def decode_urls(payload: str) -> list[str]:
    values = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("urls_b64 must decode to a JSON URL array")
    return values


def merge_navigation_sections(target: dict[str, dict], items: list[dict]) -> None:
    for item in items:
        key = str(item.get("url") or item.get("title") or "")
        if not key:
            continue
        previous = target.get(key)
        if previous is None or core.count_navigation([item]) > core.count_navigation([previous]):
            target[key] = item


async def render_pages(
    urls: list[str],
    root: Path,
    concurrency: int,
    timeout_ms: int,
) -> dict[str, dict]:
    """Render every URL in Chromium and persist the exact post-render DOM."""
    results: dict[str, dict] = {}
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )

        async def render_one(index: int, url: str) -> None:
            async with semaphore:
                started = time.perf_counter()
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10_000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(500)

                    rendered_html = await page.content()
                    resolved_url = page.url or url
                    page_dir = core.output_dir_for_url(url, root)
                    page_dir.mkdir(parents=True, exist_ok=True)
                    (page_dir / "browser-rendered.html").write_text(
                        rendered_html,
                        encoding="utf-8",
                    )
                    results[url] = {
                        "success": True,
                        "html": rendered_html,
                        "resolved_url": resolved_url,
                        "status_code": response.status if response else None,
                        "html_chars": len(rendered_html),
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    }
                    print(
                        f"[RENDER {index:03d}/{len(urls):03d}] ✓ "
                        f"{results[url]['elapsed_seconds']}s html={len(rendered_html)} {url}",
                        flush=True,
                    )
                except Exception as exc:
                    results[url] = {
                        "success": False,
                        "error": str(exc),
                        "resolved_url": page.url or url,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    }
                    print(
                        f"[RENDER {index:03d}/{len(urls):03d}] ✗ {url} — {exc}",
                        flush=True,
                    )
                finally:
                    await page.close()

        await asyncio.gather(*(render_one(i, url) for i, url in enumerate(urls, start=1)))
        await browser.close()

    return results


async def archive(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    urls = sorted(set(decode_urls(args.urls_b64)), key=lambda value: urlsplit(value).path)
    total = len(urls)
    print(
        f"[SHARD] {args.shard_name} — {total} pages — browser-rendered HTML + Markdown mode",
        flush=True,
    )

    rendered_pages = await render_pages(
        urls,
        root,
        concurrency=args.render_concurrency,
        timeout_ms=args.render_timeout_ms,
    )

    # ai_archive_core already contains the article/math/SVG/image conversion logic.
    # Feed it Chromium's post-render DOM instead of fetching the pre-render source
    # again. If Chromium failed for one URL, fall back to the original HTTP fetcher.
    original_fetch_html = core.fetch_html

    def fetch_rendered_or_fallback(url: str) -> tuple[str, str]:
        rendered = rendered_pages.get(url)
        if rendered and rendered.get("success") and rendered.get("html"):
            return str(rendered["html"]), str(rendered.get("resolved_url") or url)
        return original_fetch_html(url)

    core.fetch_html = fetch_rendered_or_fallback

    semaphore = asyncio.Semaphore(args.concurrency)
    counters = {"done": 0}
    nav_sections: dict[str, dict] = {}

    async def run_one(index: int, url: str) -> dict:
        page_started = time.perf_counter()
        async with semaphore:
            record, navigation = await core.process_page(
                url,
                root,
                article_selector=args.article_selector,
                retries=args.retries,
            )
        rendered = rendered_pages.get(url, {})
        record["browser_rendered"] = bool(rendered.get("success"))
        record["browser_status_code"] = rendered.get("status_code")
        record["browser_rendered_html_chars"] = rendered.get("html_chars", 0)
        record["browser_render_seconds"] = rendered.get("elapsed_seconds")
        if not rendered.get("success"):
            record["browser_render_error"] = rendered.get("error", "render failed")

        counters["done"] += 1
        merge_navigation_sections(nav_sections, navigation)
        marker = "✓" if record.get("success") else "✗"
        print(
            f"[PAGE {counters['done']:03d}/{total:03d}] {marker} "
            f"{record.get('elapsed_seconds', '-') }s {url}",
            flush=True,
        )
        if record.get("success"):
            print(
                f"  md={record.get('markdown_chars', 0)} html={record.get('html_chars', 0)} "
                f"rendered={record.get('browser_rendered_html_chars', 0)} "
                f"svg={record.get('svg_count', 0)} img={record.get('image_count', 0)}",
                flush=True,
            )
        else:
            print(f"  error={record.get('error', 'unknown')}", flush=True)
        print(f"[DONE {index:03d}/{total:03d}] {time.perf_counter() - page_started:.2f}s", flush=True)
        return record

    try:
        records = await asyncio.gather(*(run_one(i, u) for i, u in enumerate(urls, start=1)))
    finally:
        core.fetch_html = original_fetch_html

    records = sorted(records, key=lambda item: urlsplit(str(item.get("url", ""))).path)
    render_failures = sum(1 for item in records if not item.get("browser_rendered"))
    manifest = {
        "pipeline": "ai-browser-rendered-sharded-v2",
        "shard": args.shard_name,
        "page_count": len(records),
        "success_count": sum(1 for item in records if item.get("success")),
        "failure_count": sum(1 for item in records if not item.get("success")),
        "browser_render_success_count": len(records) - render_failures,
        "browser_render_failure_count": render_failures,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "pages": records,
        "navigation_sections": list(nav_sections.values()),
    }
    (root / "shard-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "[SHARD COMPLETE]",
        json.dumps(
            {k: v for k, v in manifest.items() if k not in {"pages", "navigation_sections"}},
            ensure_ascii=False,
        ),
        flush=True,
    )
    if manifest["success_count"] == 0:
        raise RuntimeError("All pages in shard failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls-b64", required=True)
    parser.add_argument("--shard-name", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--article-selector", default=".td-content")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--render-concurrency", type=int, default=4)
    parser.add_argument("--render-timeout-ms", type=int, default=120_000)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    asyncio.run(archive(parse_args()))


if __name__ == "__main__":
    main()
