#!/usr/bin/env python3
"""Merge per-shard ZIP archives into one complete csgraduates site archive."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import crawl_site as core


def merge_shards(shards_dir: Path, output_dir: Path) -> dict:
    archives = sorted(shards_dir.glob("*.zip"))
    if not archives:
        raise RuntimeError(f"No shard ZIP files found in {shards_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    pages_root = output_dir / "pages"
    pages_root.mkdir(parents=True, exist_ok=True)

    records_by_url: dict[str, dict] = {}
    shard_summaries: list[dict] = []
    duplicates: list[str] = []

    with tempfile.TemporaryDirectory(prefix="csgraduates-shards-") as temp:
        temp_root = Path(temp)
        for archive in archives:
            shard_root = temp_root / archive.stem
            shard_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(shard_root)
            site_root = shard_root / "site"
            if not site_root.exists():
                raise RuntimeError(f"{archive.name} does not contain site/")

            summary_file = site_root / "summary.json"
            if summary_file.exists():
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
                summary["artifact"] = archive.name
                shard_summaries.append(summary)

            pages_file = site_root / "pages.json"
            records = json.loads(pages_file.read_text(encoding="utf-8")) if pages_file.exists() else []
            for record in records:
                url = str(record.get("url") or "")
                if not url:
                    continue
                if url in records_by_url:
                    duplicates.append(url)
                    previous = records_by_url[url]
                    if previous.get("success") and not record.get("success"):
                        continue
                records_by_url[url] = record

            shard_pages = site_root / "pages"
            if shard_pages.exists():
                shutil.copytree(shard_pages, pages_root, dirs_exist_ok=True)

    records = sorted(
        records_by_url.values(),
        key=lambda item: (urlsplit(str(item.get("url", ""))).path.count("/"), urlsplit(str(item.get("url", ""))).path),
    )
    for index, record in enumerate(records, start=1):
        record["index"] = index

    failures = [
        record
        for record in records
        if not record.get("success") or record.get("browser_export_error")
    ]
    (output_dir / "pages.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "tree.json").write_text(
        json.dumps(core.build_tree(records), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "urls.txt").write_text(
        "\n".join(str(record["url"]) for record in records) + "\n", encoding="utf-8"
    )
    core.write_catalog(records, output_dir)

    summary = {
        "pipeline": "http-first-static-sharded",
        "shard_artifacts": len(archives),
        "shard_summaries": shard_summaries,
        "unique_pages": len(records),
        "archived_success": sum(1 for record in records if record.get("success")),
        "archived_failed": sum(1 for record in records if not record.get("success")),
        "browser_export_failed": sum(1 for record in records if record.get("browser_export_error")),
        "duplicate_urls": sorted(set(duplicates)),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "shards.json").write_text(
        json.dumps(shard_summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "shard_summaries"}, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards-dir", type=Path, default=Path("artifacts/shards"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    args = parser.parse_args()
    merge_shards(args.shards_dir, args.output_dir)


if __name__ == "__main__":
    main()
