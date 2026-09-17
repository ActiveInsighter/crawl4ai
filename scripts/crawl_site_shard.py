#!/usr/bin/env python3
"""Archive one deterministic URL shard with live progress logs."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import async_playwright

import crawl_site as core
import crawl_site_static as static


def decode_urls(payload: str) -> list[str]:
    data = base64.urlsafe_b64decode(payload.encode("ascii"))
    values = json.loads(data.decode("utf-8"))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("urls_b64 must decode to a JSON array of URL strings")
    return values


async def _robots_filter(urls: list[str], base_url: str) -> list[str]:
    robots_url = urljoin(base_url.rstrip("/") + "/", "robots.txt")
    try:
        robots_text, _ = await asyncio.to_thread(core._fetch_text, robots_url, "text/plain,*/*")
    except Exception as exc:
        print(f"[ROBOTS] unavailable, continuing: {exc}", flush=True)
        return urls
    allowed = [url for url in urls if core.allowed_by_robots(url, robots_text, robots_url)]
    if len(allowed) != len(urls):
        print(f"[ROBOTS] filtered {len(urls) - len(allowed)} page(s)", flush=True)
    return allowed


async def archive_shard(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    archive_root = args.output_dir
    archive_root.mkdir(parents=True, exist_ok=True)
    urls = sorted(set(decode_urls(args.urls_b64)), key=lambda value: urlsplit(value).path)
    if args.respect_robots:
        urls = await _robots_filter(urls, args.base_url)
    if not urls:
        raise RuntimeError("Shard contains no URLs after filtering")

    total = len(urls)
    print(f"[SHARD] {args.shard_name} — {total} pages", flush=True)
    for index, url in enumerate(urls, start=1):
        print(f"[QUEUE {index:03d}/{total:03d}] {url}", flush=True)

    discovery = {
        "base_url": args.base_url,
        "pipeline": "http-first-static-sharded",
        "shard_name": args.shard_name,
        "selected_total": total,
        "respect_robots": args.respect_robots,
    }
    (archive_root / "discovery.json").write_text(
        json.dumps(discovery, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive_root / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")

    fetch_semaphore = asyncio.Semaphore(args.fetch_concurrency)
    process_semaphore = asyncio.Semaphore(args.process_concurrency)
    pdf_semaphore = asyncio.Semaphore(args.pdf_concurrency)
    counters = {"content": 0, "pdf": 0}

    async def run_one(index: int, url: str, browser) -> dict:
        page_started = time.perf_counter()
        record, html = await static._process_page(
            index,
            url,
            archive_root,
            args.article_selector,
            fetch_semaphore,
            process_semaphore,
            args.retries,
        )
        counters["content"] += 1
        marker = "✓" if record.get("success") else "✗"
        print(
            f"[CONTENT {counters['content']:03d}/{total:03d}] {marker} "
            f"fetch={record.get('http_fetch_seconds', '-')}s "
            f"post={record.get('postprocess_seconds', '-')}s {url}",
            flush=True,
        )

        if record.get("success") and html and browser is not None:
            try:
                pdf_result = await static._capture_static_page(
                    browser,
                    url,
                    html,
                    archive_root / record["output_dir"],
                    pdf_semaphore,
                    args.asset_wait_ms,
                )
                record.update(pdf_result)
                pdf_marker = "✓"
                pdf_seconds = pdf_result.get("browser_export_seconds", "-")
            except Exception as exc:
                record["browser_export_error"] = str(exc)
                pdf_marker = "✗"
                pdf_seconds = "-"
            counters["pdf"] += 1
            print(
                f"[PDF {counters['pdf']:03d}/{total:03d}] {pdf_marker} "
                f"{pdf_seconds}s {url}",
                flush=True,
            )
            page_dir = archive_root / record["output_dir"]
            (page_dir / "metadata.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        print(
            f"[DONE {index:03d}/{total:03d}] {time.perf_counter() - page_started:.2f}s {url}",
            flush=True,
        )
        return record

    if args.skip_pdf:
        records = await asyncio.gather(
            *(run_one(index, url, None) for index, url in enumerate(urls, start=1))
        )
    else:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            records = await asyncio.gather(
                *(run_one(index, url, browser) for index, url in enumerate(urls, start=1))
            )
            await browser.close()

    records = sorted(records, key=lambda record: record.get("index", 0))
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
        **discovery,
        "archived_success": sum(1 for record in records if record.get("success")),
        "archived_failed": sum(1 for record in records if not record.get("success")),
        "browser_export_failed": sum(1 for record in records if record.get("browser_export_error")),
        "pdf_enabled": not args.skip_pdf,
        "fetch_concurrency": args.fetch_concurrency,
        "process_concurrency": args.process_concurrency,
        "pdf_concurrency": args.pdf_concurrency,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    (archive_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[SHARD COMPLETE]", json.dumps(summary, ensure_ascii=False), flush=True)

    if summary["archived_success"] == 0:
        raise RuntimeError("All pages in shard failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://csgraduates.com/")
    parser.add_argument("--urls-b64", required=True)
    parser.add_argument("--shard-name", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--article-selector", default=".td-content")
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
    asyncio.run(archive_shard(parse_args()))


if __name__ == "__main__":
    main()
