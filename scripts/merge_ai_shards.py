#!/usr/bin/env python3
"""Merge AI shard ZIPs into a small, clean final archive."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def nav_count(items: list[dict]) -> int:
    return sum(1 + nav_count(item.get("children", [])) for item in items)


def merge_nav(target: dict[str, dict], items: list[dict]) -> None:
    for item in items:
        key = str(item.get("url") or item.get("title") or "")
        if not key:
            continue
        previous = target.get(key)
        if previous is None or nav_count([item]) > nav_count([previous]):
            target[key] = item


def attach_local_paths(items: list[dict], page_map: dict[str, dict]) -> None:
    for item in items:
        record = page_map.get(str(item.get("url") or ""))
        if record and record.get("success") and record.get("output_dir"):
            base = str(record["output_dir"])
            item["markdown"] = f"{base}/content.md"
            item["html"] = f"{base}/content.html"
        attach_local_paths(item.get("children", []), page_map)


def render_navigation(items: list[dict]) -> str:
    lines = ["# csgraduates.com 文档目录", ""]
    def render(nodes: list[dict], depth: int) -> None:
        for item in nodes:
            indent = "  " * depth
            title = str(item.get("title") or item.get("url") or "Untitled")
            source = str(item.get("url") or "")
            local = []
            if item.get("markdown"):
                local.append(f"[Markdown]({item['markdown']})")
            if item.get("html"):
                local.append(f"[HTML]({item['html']})")
            suffix = " — " + " · ".join(local) if local else ""
            lines.append(f"{indent}- [{title}]({source}){suffix}")
            render(item.get("children", []), depth + 1)
    render(items, 0)
    lines.append("")
    return "\n".join(lines)


def merge(shards_dir: Path, output_dir: Path) -> dict:
    archives = sorted(shards_dir.glob("*.zip"))
    if not archives:
        raise RuntimeError(f"No shard ZIP files found in {shards_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_root = output_dir / "pages"
    pages_root.mkdir(parents=True, exist_ok=True)

    records_by_url: dict[str, dict] = {}
    nav_sections: dict[str, dict] = {}
    shard_stats: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="csgraduates-ai-") as temp:
        temp_root = Path(temp)
        for archive in archives:
            shard_root = temp_root / archive.stem
            shard_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(shard_root)
            site_root = shard_root / "site"
            if not site_root.exists():
                candidates = list(shard_root.glob("*/shard-manifest.json"))
                site_root = candidates[0].parent if candidates else shard_root
            manifest_file = site_root / "shard-manifest.json"
            if not manifest_file.exists():
                raise RuntimeError(f"{archive.name} has no shard-manifest.json")
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            shard_stats.append({
                "shard": manifest.get("shard"),
                "page_count": manifest.get("page_count"),
                "success_count": manifest.get("success_count"),
                "failure_count": manifest.get("failure_count"),
                "elapsed_seconds": manifest.get("elapsed_seconds"),
            })
            for record in manifest.get("pages", []):
                url = str(record.get("url") or "")
                if url:
                    records_by_url[url] = record
            merge_nav(nav_sections, manifest.get("navigation_sections", []))
            shard_pages = site_root / "pages"
            if shard_pages.exists():
                shutil.copytree(shard_pages, pages_root, dirs_exist_ok=True)

    records = sorted(records_by_url.values(), key=lambda item: urlsplit(str(item.get("url", ""))).path)
    page_map = {str(record.get("url")): record for record in records if record.get("url")}
    navigation = sorted(nav_sections.values(), key=lambda item: urlsplit(str(item.get("url", ""))).path)
    attach_local_paths(navigation, page_map)

    failures = [record for record in records if not record.get("success")]
    final_manifest = {
        "source": "https://csgraduates.com/",
        "format": "csgraduates-ai-archive-v1",
        "description": "AI-readable archive: Markdown + cleaned article HTML + localized images/SVG. No PDF or browser snapshot.",
        "page_count": len(records),
        "success_count": len(records) - len(failures),
        "failure_count": len(failures),
        "pages": records,
        "failures": failures,
        "shards": shard_stats,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(final_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "navigation.json").write_text(
        json.dumps({"source": final_manifest["source"], "sections": navigation}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "navigation.md").write_text(render_navigation(navigation), encoding="utf-8")
    (output_dir / "README.md").write_text(
        "# csgraduates.com AI archive\n\n"
        "This archive is intentionally compact. It contains no PDF, browser screenshot, raw full-page HTML, or duplicate crawler output.\n\n"
        "## Structure\n\n"
        "- `navigation.md` / `navigation.json`: site document hierarchy and local links.\n"
        "- `manifest.json`: source URL, page status, output path and asset counts for every page.\n"
        "- `pages/<original-url-path>/content.md`: AI-friendly Markdown.\n"
        "- `pages/<original-url-path>/content.html`: cleaned article HTML with rendered math DOM preserved.\n"
        "- `pages/<original-url-path>/assets/images/`: localized raster images when present.\n"
        "- `pages/<original-url-path>/assets/svg/`: standalone SVG diagrams with SVG case/style fidelity repaired.\n\n"
        "Use Markdown for normal reading and HTML/SVG as a fallback when formulas or diagrams need more fidelity.\n",
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in final_manifest.items() if k not in {"pages", "failures", "shards"}}, ensure_ascii=False, indent=2), flush=True)
    return final_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards-dir", type=Path, default=Path("artifacts/shards"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    args = parser.parse_args()
    merge(args.shards_dir, args.output_dir)


if __name__ == "__main__":
    main()
