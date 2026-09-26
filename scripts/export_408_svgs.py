#!/usr/bin/env python3
"""Export all content SVG figures from csgraduates 408 past-paper pages.

Outputs one validated standalone SVG per figure, named with exam year + question number.
If one question has multiple figures, a stable -01/-02 suffix is appended.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "https://csgraduates.com/study_methods/408quiz/{year}/"
YEARS = range(2009, 2027)
SVG_NS = "http://www.w3.org/2000/svg"

EXTRACT_JS = r"""
(article) => {
  const out = [];
  let question = null;
  const nodes = article.querySelectorAll("h5, svg");
  for (const node of nodes) {
    if (node.tagName.toLowerCase() === "h5") {
      const m = node.textContent.trim().match(/^(\d+)$/);
      if (m) question = Number(m[1]);
      continue;
    }
    const ariaHidden = (node.getAttribute("aria-hidden") || "").toLowerCase();
    const role = (node.getAttribute("role") || "").toLowerCase();
    const cls = new Set((node.getAttribute("class") || "").split(/\s+/).filter(Boolean));
    if (ariaHidden === "true" || role === "none" || role === "presentation" || cls.has("kp-icon")) {
      continue;
    }
    const clone = node.cloneNode(true);
    const all = [clone, ...clone.querySelectorAll("*")];
    const validXmlName = /^[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?$/;
    for (const el of all) {
      for (const attr of [...el.attributes]) {
        if (!validXmlName.test(attr.name)) el.removeAttribute(attr.name);
      }
    }
    if (!clone.getAttribute("xmlns")) clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const xml = new XMLSerializer().serializeToString(clone);
    out.push({
      question,
      xml,
      title: node.querySelector("title")?.textContent?.trim() || "",
      desc: node.querySelector("desc")?.textContent?.trim() || "",
      text: node.textContent?.trim() || ""
    });
  }
  return out;
}
"""

def validate_svg(xml: str) -> ET.Element:
    if "<!DOCTYPE" in xml.upper():
        raise ValueError("DOCTYPE is not allowed")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        col = exc.position[1] if getattr(exc, "position", None) else 0
        lo, hi = max(0, col - 180), min(len(xml), col + 180)
        print("[svg-xml-error]", repr(xml[lo:hi]))
        raise
    local = root.tag.rsplit("}", 1)[-1]
    if local != "svg":
        raise ValueError(f"root element is {root.tag!r}, expected svg")
    if root.tag == "svg":
        root.set("xmlns", SVG_NS)
        xml = ET.tostring(root, encoding="unicode")
        root = ET.fromstring(xml)
    return root

def normalized_svg(xml: str) -> str:
    validate_svg(xml)
    if "xmlns=" not in xml.split(">", 1)[0]:
        xml = xml.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
        validate_svg(xml)
    if not xml.endswith("\n"):
        xml += "\n"
    return xml

async def export_year(page, year: int, output_dir: Path) -> dict:
    url = BASE_URL.format(year=year)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    if response is None or response.status >= 400:
        raise RuntimeError(f"{year}: HTTP failure for {url}: {getattr(response, 'status', None)}")

    await page.wait_for_selector(".td-content", timeout=30_000)
    await page.wait_for_timeout(1000)
    article = page.locator(".td-content").first
    title = await page.title()
    if f"{year}" not in title or "408" not in title:
        raise RuntimeError(f"{year}: unexpected title {title!r}")

    figures = await article.evaluate(EXTRACT_JS)
    if not figures:
        raise RuntimeError(f"{year}: no content SVG figures found")

    unknown = [i + 1 for i, f in enumerate(figures) if not isinstance(f.get("question"), int)]
    if unknown:
        raise RuntimeError(f"{year}: SVG(s) without a numeric question heading: {unknown}")

    q_counts = Counter(int(f["question"]) for f in figures)
    q_seen = defaultdict(int)
    manifest = []

    for index, fig in enumerate(figures, 1):
        q = int(fig["question"])
        q_seen[q] += 1
        suffix = f"-{q_seen[q]:02d}" if q_counts[q] > 1 else ""
        filename = f"{year}-{q}{suffix}.svg"
        xml = normalized_svg(fig["xml"])
        path = output_dir / filename
        path.write_text(xml, encoding="utf-8")

        # Re-read from disk and validate the exact bytes that will be packaged.
        parsed = ET.parse(path)
        if parsed.getroot().tag.rsplit("}", 1)[-1] != "svg":
            raise RuntimeError(f"{filename}: invalid root element")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append({
            "year": year,
            "question": q,
            "sequence_for_question": q_seen[q],
            "source_index": index,
            "filename": filename,
            "bytes": path.stat().st_size,
            "sha256": digest,
            "title": fig.get("title", ""),
            "description": fig.get("desc", ""),
            "source_url": url,
        })

    return {
        "year": year,
        "url": url,
        "page_title": title,
        "svg_count": len(figures),
        "question_count_with_svg": len(q_counts),
        "files": manifest,
    }

async def main_async(args) -> None:
    out = args.output_dir
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        try:
            for year in args.years:
                print(f"[408-svg] {year}")
                results.append(await export_year(page, year, out))
        finally:
            await browser.close()

    files = sorted(out.glob("*.svg"))
    if not files:
        raise RuntimeError("no SVG files exported")

    # Final package-level validation: unique names, parseable XML, expected full year range.
    names = [p.name for p in files]
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate output filenames")
    exported_years = {r["year"] for r in results}
    missing_years = sorted(set(args.years) - exported_years)
    if missing_years:
        raise RuntimeError(f"missing years: {missing_years}")
    for p in files:
        ET.parse(p)

    flat = [item for year in results for item in year["files"]]
    manifest = {
        "source": "csgraduates.com 408历年真题",
        "years": list(args.years),
        "year_count": len(results),
        "svg_count": len(flat),
        "all_xml_valid": True,
        "naming": "YEAR-QUESTION.svg; multiple figures use YEAR-QUESTION-NN.svg",
        "by_year": results,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "years": len(results),
        "svg_count": len(flat),
        "all_xml_valid": True,
    }, ensure_ascii=False))

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/408-svg"))
    parser.add_argument(
        "--years",
        type=lambda s: [int(x) for x in s.split(",")],
        default=list(YEARS),
        help="Comma-separated years; default: 2009..2026",
    )
    return parser.parse_args()

if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
