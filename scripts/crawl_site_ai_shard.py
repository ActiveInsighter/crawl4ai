#!/usr/bin/env python3
"""Archive one AI-oriented shard without Chromium/PDF."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from ai_archive_core import count_navigation, process_page


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
        if previous is None or count_navigation([item]) > count_navigation([previous]):
            target[key] = item


async def archive(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    urls = sorted(set(decode_urls(args.urls_b64)), key=lambda value: urlsplit(value).path)
    total = len(urls)
    print(f"[SHARD] {args.shard_name} — {total} pages — AI HTML+Markdown mode", flush=True)

    semaphore = asyncio.Semaphore(args.concurrency)
    counters = {"done": 0}
    nav_sections: dict[str, dict] = {}

    async def run_one(index: int, url: str) -> dict:
        page_started = time.perf_counter()
        async with semaphore:
            record, navigation = await process_page(
                url,
                root,
                article_selector=args.article_selector,
                retries=args.retries,
            )
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
                f"svg={record.get('svg_count', 0)} img={record.get('image_count', 0)}",
                flush=True,
            )
        else:
            print(f"  error={record.get('error', 'unknown')}", flush=True)
        print(f"[DONE {index:03d}/{total:03d}] {time.perf_counter() - page_started:.2f}s", flush=True)
        return record

    records = await asyncio.gather(*(run_one(i, u) for i, u in enumerate(urls, start=1)))
    records = sorted(records, key=lambda item: urlsplit(str(item.get("url", ""))).path)
    manifest = {
        "pipeline": "ai-http-sharded-v1",
        "shard": args.shard_name,
        "page_count": len(records),
        "success_count": sum(1 for item in records if item.get("success")),
        "failure_count": sum(1 for item in records if not item.get("success")),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "pages": records,
        "navigation_sections": list(nav_sections.values()),
    }
    (root / "shard-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[SHARD COMPLETE]", json.dumps({k: v for k, v in manifest.items() if k not in {"pages", "navigation_sections"}}, ensure_ascii=False), flush=True)
    if manifest["success_count"] == 0:
        raise RuntimeError("All pages in shard failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls-b64", required=True)
    parser.add_argument("--shard-name", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--article-selector", default=".td-content")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    asyncio.run(archive(parse_args()))


if __name__ == "__main__":
    main()
