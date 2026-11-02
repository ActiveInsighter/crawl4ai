#!/usr/bin/env python3
"""Merge four independently printed 408 subjects; validate every source PDF."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import fitz
from render_408_pdf_shard import SUBJECTS, inspect_pdf, relative_pdf


def merge(shards_dir: Path, output_dir: Path, expected_count: int | None = None) -> dict:
    manifests = sorted(shards_dir.rglob("shard-manifest.json"))
    if len(manifests) != 4:
        raise ValueError("Expected exactly four subject manifests, got %s" % len(manifests))
    output_dir.mkdir(parents=True, exist_ok=True)
    all_records: dict[str, dict] = {}
    for mf in manifests:
        data = json.loads(mf.read_text(encoding="utf-8"))
        for record in data["pages"]:
            url = record["url"]
            if url in all_records:
                raise ValueError("Duplicate page URL in shards: " + url)
            expected = relative_pdf(url)
            if record["pdf"] != expected:
                raise ValueError("Unsafe or inconsistent page path: " + url)
            if record.get("success"):
                source = mf.parent / expected
                if not source.exists():
                    raise ValueError("Missing PDF: " + str(source))
                destination = output_dir / expected
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                pdf_info = inspect_pdf(destination, record["article_chars"])
                if pdf_info["sha256"] != record.get("sha256"):
                    raise ValueError("PDF SHA mismatch: " + url)
                record.update(pdf_info)
            all_records[url] = record

    if expected_count is not None and len(all_records) != expected_count:
        raise ValueError("Expected %s source pages, got %s" %
                         (expected_count, len(all_records)))
    records = sorted(all_records.values(), key=lambda r: r["url"])
    subjects = {}
    for slug, label in SUBJECTS.items():
        group = [r for r in records if r["subject"] == slug]
        merged = fitz.open()
        toc = []
        for entry in group:
            if not entry.get("success"):
                continue
            source = output_dir / entry["pdf"]
            start = merged.page_count + 1
            with fitz.open(source) as part:
                merged.insert_pdf(part, links=True, annots=True)
            title = entry.get("title") or entry["url"].rstrip("/").split("/")[-1]
            toc.append([1, title[:120], start])
        target = output_dir / "subjects" / (slug + ".pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        if merged.page_count:
            if toc:
                merged.set_toc(toc)
            merged.save(target, garbage=3, deflate=True)
            validated = inspect_pdf(target, 100)
            subjects[slug] = {
                "label": label, "pages": len(group),
                "successful": sum(bool(p.get("success")) for p in group),
                "merged_pdf": target.relative_to(output_dir).as_posix(),
                "merged_pdf_pages": validated["pdf_pages"],
                "merged_pdf_bytes": validated["pdf_bytes"],
                "sha256": validated["sha256"],
            }
        else:
            subjects[slug] = {"label": label, "pages": len(group),
                              "successful": 0, "merged_pdf": None}
        merged.close()

    failures = [r for r in records if not r.get("success")]
    warnings = [r for r in records if r.get("warnings")]
    report = {
        "format": "408-original-browser-pdf-v1",
        "source": "https://csgraduates.com/",
        "source_page_count": len(records),
        "successful_pdf_count": len(records) - len(failures),
        "failed_pdf_count": len(failures),
        "warning_page_count": len(warnings),
        "subjects": subjects,
        "pages": records,
        "issues": [
            {"url": r["url"], "error": r.get("error"),
             "warnings": r.get("warnings", [])}
            for r in records if not r.get("success") or r.get("warnings")
        ],
        "notes": (
            "These are Chromium-printed PDFs of the source pages. "
            "No formula, SVG or Markdown reconstruction was performed. "
            "Mechanical print validation does not establish academic correctness."
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 408 四科知识点原网页 PDF",
        "",
        "以 Chromium 直接打印 csgraduates.com 四科知识点页面，不转换 Markdown、SVG 或公式。",
        "原网页本身的知识内容正确性仍需人工校对。",
        "",
        "## 四科合订 PDF",
        "",
    ]
    for slug, item in subjects.items():
        if item.get("merged_pdf"):
            lines.append("- %s：[%s](%s)，%d 篇，%d 个 PDF 页" %
                         (item["label"], slug, item["merged_pdf"],
                          item["pages"], item["merged_pdf_pages"]))
    lines += ["", "## 原知识点分页 PDF", "",
              "每个页面的文件按原网址层级存放于 pages/ 目录，",
              "完整对应关系、校验值、重试失败与字体图片告警见 manifest.json。", ""]
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print("[408 PDF SUMMARY]", json.dumps({
        k: v for k, v in report.items() if k not in ("pages", "issues")
    }, ensure_ascii=False), flush=True)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards-dir", type=Path, default=Path("artifacts/shards"))
    ap.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    ap.add_argument("--expected-count", type=int, default=None)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    result = merge(args.shards_dir, args.output_dir, args.expected_count)
    if args.strict and (result["failed_pdf_count"] or result["warning_page_count"]):
        raise SystemExit("PDF archive needs review. Files and report retained.")
if __name__ == "__main__":
    main()
