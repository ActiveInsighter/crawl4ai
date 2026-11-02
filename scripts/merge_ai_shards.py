#!/usr/bin/env python3
"""Merge AI shard ZIPs into the final browser-rich AI archive."""

from __future__ import annotations

import argparse
import json
import hashlib
import re
import xml.etree.ElementTree as ET
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
            if record.get("browser_rendered"):
                item["rendered_html"] = f"{base}/browser-rendered.html"
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
                local.append(f"[Article HTML]({item['html']})")
            if item.get("rendered_html"):
                local.append(f"[Rendered HTML]({item['rendered_html']})")
            suffix = " — " + " · ".join(local) if local else ""
            lines.append(f"{indent}- [{title}]({source}){suffix}")
            render(item.get("children", []), depth + 1)

    render(items, 0)
    lines.append("")
    return "\n".join(lines)



def verify_page_integrity(record: dict, output_dir: Path) -> list[str]:
    """Validate the actual merged files, not just a crawler success flag."""
    if not record.get("success"):
        return ["page_extract_failed"]
    relative = str(record.get("output_dir") or "")
    if not relative.startswith("pages/") or ".." in Path(relative).parts:
        return ["invalid_page_directory"]
    page = output_dir / relative
    issues = []
    for name in ("content.md", "content.html"):
        if not (page / name).is_file() or (page / name).stat().st_size == 0:
            issues.append("missing_" + name.replace(".", "_"))
    if record.get("browser_rendered") and not (page / "browser-rendered.html").is_file():
        issues.append("missing_browser_rendered_html")
    md_path = page / "content.md"
    if md_path.is_file():
        md = md_path.read_text(encoding="utf-8")
        # All generated relative assets must exist and have nonzero size.
        for asset in set(re.findall(r"(?:src=[\"']|]\()(assets/[^)\s\"']+)", md)):
            target = (page / asset).resolve()
            if not target.is_relative_to(page.resolve()) or not target.is_file():
                issues.append("missing_local_asset")
            elif not target.stat().st_size:
                issues.append("empty_local_asset")
    for svg in (page / "assets" / "svg").glob("*.svg"):
        try:
            ET.parse(svg)
        except ET.ParseError:
            issues.append("invalid_svg_xml")
    for file in (page / "assets").rglob("*") if (page / "assets").exists() else ():
        if file.is_file() and not file.stat().st_size:
            issues.append("empty_local_asset")
    stats = record.get("math") or {}
    if stats and stats.get("total", 0) != (
        stats.get("tex", 0) + stats.get("image", 0) + stats.get("unresolved", 0)
    ):
        issues.append("math_accounting_mismatch")
    return sorted(set(issues))


def build_quality_report(records: list[dict]) -> dict:
    by_subject: dict[str, dict[str, int]] = {}
    failed = []
    labels = {
        "data_structure": "数据结构",
        "constitution_principle": "计算机组成原理",
        "operating_system": "操作系统",
        "computer_network": "计算机网络",
    }
    for record in records:
        path = str(record.get("output_dir") or "")
        category = next((label for prefix, label in labels.items()
                         if path.startswith("pages/" + prefix + "/")), "其他")
        item = by_subject.setdefault(category, {"pages": 0, "passed": 0, "review": 0})
        item["pages"] += 1
        if record.get("quality_pass"):
            item["passed"] += 1
        else:
            item["review"] += 1
            failed.append({"url": record.get("url"), "issues": record.get("quality_issues", [])})
    return {
        "page_count": len(records),
        "quality_pass_count": sum(bool(r.get("quality_pass")) for r in records),
        "quality_review_count": len(failed),
        "subjects": by_subject,
        "needs_review": failed,
        "note": "Mechanical integrity checks do not establish subject-matter correctness.",
    }

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
                "browser_render_success_count": manifest.get("browser_render_success_count"),
                "browser_render_failure_count": manifest.get("browser_render_failure_count"),
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
    for record in records:
        issues = set(record.get("quality_issues", []))
        issues.update(verify_page_integrity(record, output_dir))
        if not record.get("browser_rendered"):
            issues.add("browser_render_failed")
        record["quality_issues"] = sorted(issues)
        record["quality_pass"] = not issues
    quality_report = build_quality_report(records)
    (output_dir / "quality-report.json").write_text(
        json.dumps(quality_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    page_map = {str(record.get("url")): record for record in records if record.get("url")}
    navigation = sorted(nav_sections.values(), key=lambda item: urlsplit(str(item.get("url", ""))).path)
    attach_local_paths(navigation, page_map)

    failures = [record for record in records if not record.get("success")]
    browser_render_failures = [record for record in records if not record.get("browser_rendered")]
    final_manifest = {
        "source": "https://csgraduates.com/",
        "format": "csgraduates-ai-archive-v2",
        "description": "AI-readable archive: full browser-rendered DOM + cleaned article HTML + Markdown + localized images/SVG. No PDF.",
        "page_count": len(records),
        "success_count": len(records) - len(failures),
        "failure_count": len(failures),
        "browser_render_success_count": len(records) - len(browser_render_failures),
        "browser_render_failure_count": len(browser_render_failures),
        "quality_pass_count": quality_report["quality_pass_count"],
        "quality_review_count": quality_report["quality_review_count"],
        "pages": records,
        "failures": failures,
        "browser_render_failures": browser_render_failures,
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
        "This archive keeps the full Chromium-rendered DOM for maximum fidelity while still providing compact AI-friendly derivatives. It intentionally contains no PDF.\n\n"
        "## Structure\n\n"
        "- `navigation.md` / `navigation.json`: site document hierarchy and local links.\n"
        "- `manifest.json`: page status, formula/image/SVG counts and audit issues.\n"
        "- `quality-report.json`: per-subject pass/review counts and every degraded URL.\n"
        "- `pages/<original-url-path>/browser-rendered.html`: full page DOM after Chromium loaded and executed the page. This is the richest HTML source.\n"
        "- `pages/<original-url-path>/content.html`: cleaned article HTML derived from the browser-rendered DOM.\n"
        "- `pages/<original-url-path>/content.md`: AI-friendly Markdown derived from the same rendered DOM.\n"
        "- `pages/<original-url-path>/assets/images/`: localized raster images when present.\n"
        "- `pages/<original-url-path>/assets/svg/`: standalone SVG diagrams with SVG case/style fidelity repaired.\n\n"
        "For maximum information fidelity, read `browser-rendered.html`; use `content.md` for fast text retrieval and `content.html`/SVG for focused formula or diagram inspection.\n",
        encoding="utf-8",
    )
    print(f"[QUALITY] {quality_report['quality_pass_count']}/{quality_report['page_count']} verified; "
          f"{quality_report['quality_review_count']} need review", flush=True)
    print(
        json.dumps(
            {k: v for k, v in final_manifest.items() if k not in {"pages", "failures", "browser_render_failures", "shards"}},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return final_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards-dir", type=Path, default=Path("artifacts/shards"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    args = parser.parse_args()
    merge(args.shards_dir, args.output_dir)


if __name__ == "__main__":
    main()
