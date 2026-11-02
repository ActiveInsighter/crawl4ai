#!/usr/bin/env python3
"""Render original csgraduates 408 knowledge pages directly with Chromium to PDF.

No BeautifulSoup, Markdown, SVG extraction, formula OCR or HTML reconstruction.
The original page (including browser-rendered KaTeX, SVG and images) is printed
by Chromium; mechanical checks run against the resulting PDF bytes.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pymupdf as fitz
from playwright.async_api import async_playwright

SUBJECTS = {
    "data_structure": "数据结构",
    "constitution_principle": "计算机组成原理",
    "operating_system": "操作系统",
    "computer_network": "计算机网络",
}
SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
PRINT_CSS = r"""
@page { size: A4; margin: 12mm 13mm 14mm; }
html, body {
  background: #fff !important;
  height: auto !important;
  min-height: 0 !important;
  overflow: visible !important;
  width: auto !important;
}
.td-navbar, .td-sidebar, .td-toc, .td-page-meta,
nav[aria-label], .td-breadcrumbs, .feedback--answer,
.d-print-none, button, .btn, .navbar, .sidebar,
.td-search, .td-footer, footer, body > header {
  display: none !important;
}
.td-main, .td-outer, .td-content, main, article, .container,
.container-fluid, .row, .col, [class*="col-"] {
  max-width: none !important;
  min-height: 0 !important;
  width: auto !important;
  height: auto !important;
  overflow: visible !important;
}
.td-main, .td-outer, .td-content, main, article {
  padding: 0 !important;
  margin: 0 !important;
  position: static !important;
}
.td-content { display: block !important; }
svg, img, canvas, figure, .katex-display, mjx-container, pre, table {
  max-width: 100% !important;
  break-inside: avoid;
  page-break-inside: avoid;
}
svg, img { max-height: 250mm !important; }
.katex-display, pre { overflow: visible !important; }
table { border-collapse: collapse; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
a { color: inherit !important; }
* { animation: none !important; transition: none !important; }
"""

def relative_pdf(url: str) -> str:
    parts = [unquote(p) for p in urlsplit(url).path.split("/") if p]
    if not parts or parts[0] not in SUBJECTS:
        raise ValueError("Not a 408 knowledge URL: " + url)
    sanitized = []
    for part in parts:
        clean = SAFE_SEGMENT.sub("-", part).strip("._-")[:100]
        if not clean or clean in {".", ".."}:
            raise ValueError("Invalid URL path: " + url)
        sanitized.append(clean)
    if len(sanitized) == 1:
        return "pages/" + sanitized[0] + "/_index.pdf"
    return "pages/" + "/".join(sanitized) + ".pdf"

def decode_urls(b64: str) -> list[str]:
    values = json.loads(base64.urlsafe_b64decode(b64).decode("utf-8"))
    if not isinstance(values, list):
        raise ValueError("URLs must be an array")
    urls = sorted(set(values))
    for url in urls:
        if not isinstance(url, str) or urlsplit(url).hostname != "csgraduates.com":
            raise ValueError("Unexpected URL: " + str(url))
        relative_pdf(url)
    return urls

def inspect_pdf(path: Path, source_chars: int) -> dict:
    with fitz.open(path) as pdf:
        if not pdf.is_pdf or pdf.page_count < 1:
            raise ValueError("Invalid or empty PDF")
        total_text = 0
        for page in pdf:
            total_text += len(page.get_text("text").strip())
        if pdf.page_count > 400:
            raise ValueError("Unexpectedly long PDF: %s pages" % pdf.page_count)
        if source_chars >= 180 and total_text < 50:
            raise ValueError("Text missing from browser PDF")
        return {
            "pdf_pages": pdf.page_count,
            "pdf_text_chars": total_text,
            "pdf_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

async def stabilize(page) -> dict:
    await page.wait_for_selector(".td-content", state="visible", timeout=30000)
    await page.wait_for_function(
        """() => {
            const node = document.querySelector('.td-content');
            if (!node) return false;
            const n = (node.innerText || '').trim().length;
            if (n < 80) return false;
            const now = Date.now(), old = window.__pdfStable;
            if (!old || old.n !== n) {
                window.__pdfStable = {n, time: now};
                return false;
            }
            return now - old.time >= 1400;
        }""",
        polling=250, timeout=35000,
    )
    # Expand the actual on-page content before printing. Do not parse or
    # rewrite its SVG and math. Opening HTML details reveals hidden notes.
    await page.evaluate("""() => {
        for (const el of document.querySelectorAll('.td-content details')) el.open = true;
        for (const el of document.querySelectorAll('.td-content .collapse')) {
            el.classList.add('show');
            el.style.height = 'auto';
        }
        for (const el of document.querySelectorAll('.td-content .tab-pane')) {
            el.classList.add('show', 'active');
            el.style.display = 'block';
        }
        for (const img of document.querySelectorAll('.td-content img')) {
            img.loading = 'eager';
            if (img.dataset.src && (!img.src || img.src.startsWith('data:')))
                img.src = img.dataset.src;
        }
    }""")
    # Bring lazy images/diagrams into view; return to top before PDF printing.
    await page.evaluate("""async () => {
        const pause = ms => new Promise(r => setTimeout(r, ms));
        let y = 0, steps = 0, lastHeight = 0;
        while (steps < 150) {
            const height = Math.max(document.body.scrollHeight,
                 document.documentElement.scrollHeight);
            if (y >= height && height === lastHeight) break;
            lastHeight = height;
            window.scrollTo(0, y);
            await pause(90);
            y += 850;
            steps++;
        }
        window.scrollTo(0, 0);
    }""")
    font_ready = True
    try:
        await asyncio.wait_for(page.evaluate("() => document.fonts.ready.then(() => true)"), 14)
    except Exception:
        font_ready = False
    try:
        await page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.td-content img')).every(i => i.complete)",
            timeout=12000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(500)
    result = await page.evaluate("""() => {
        const root = document.querySelector('.td-content');
        const images = Array.from(root.querySelectorAll('img'));
        const diagrams = Array.from(root.querySelectorAll('svg'))
          .filter(s => !s.closest('.katex, mjx-container'));
        const math = Array.from(root.querySelectorAll('.katex, mjx-container, math'))
          .filter(m => !m.parentElement?.closest('.katex, mjx-container'));
        return {
            title: document.title,
            article_chars: (root.innerText || '').trim().length,
            svg_count: diagrams.length,
            formula_count: math.length,
            image_count: images.length,
            broken_images: images.filter(i => !i.complete || i.naturalWidth === 0)
                                  .map(i => i.currentSrc || i.src).slice(0, 30),
            ready_state: document.readyState,
            document_height: document.documentElement.scrollHeight,
        };
    }""")
    result["fonts_ready"] = font_ready
    return result

async def render_one(browser, url: str, root: Path, semaphore: asyncio.Semaphore, attempts: int) -> dict:
    relative = relative_pdf(url)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "subject": urlsplit(url).path.strip("/").split("/")[0], "pdf": relative}
    async with semaphore:
        started = time.monotonic()
        for attempt in range(1, attempts + 1):
            page = await browser.new_page(
                viewport={"width": 1300, "height": 1000}, device_scale_factor=1,
                locale="zh-CN",
            )
            try:
                response = await page.goto(url, wait_until="commit", timeout=45000)
                if response is None or response.status >= 400:
                    raise RuntimeError("HTTP %s" % (response.status if response else "none"))
                stats = await stabilize(page)
                await page.add_style_tag(content=PRINT_CSS)
                await page.emulate_media(media="screen")
                # Print the original loaded Chromium document, not an HTML copy.
                await asyncio.wait_for(
                    page.pdf(
                        path=str(target), format="A4",
                        prefer_css_page_size=True,
                        print_background=True,
                        display_header_footer=False,
                        scale=0.92,
                    ),
                    timeout=100,
                )
                pdf_info = inspect_pdf(target, stats["article_chars"])
                warnings = []
                if stats["broken_images"]:
                    warnings.append("broken_browser_images")
                if not stats["fonts_ready"]:
                    warnings.append("web_fonts_not_fully_loaded")
                if (pdf_info["pdf_text_chars"] < stats["article_chars"] * 0.25
                        and stats["article_chars"] > 500):
                    warnings.append("possible_missing_or_unsearchable_text")
                record.update(stats)
                record.update(pdf_info)
                record.update({
                    "success": True, "warnings": warnings,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                })
                print("[PDF] OK %s pdf_pages=%s svg=%s math=%s warnings=%s" %
                      (url, pdf_info["pdf_pages"], stats["svg_count"], stats["formula_count"], warnings), flush=True)
                return record
            except Exception as exc:
                record["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:350])
                if target.exists():
                    target.unlink()
                print("[PDF] RETRY %s attempt=%s error=%s" %
                      (url, attempt, record["error"]), flush=True)
                if attempt < attempts:
                    await asyncio.sleep(attempt * 2)
            finally:
                await page.close()
        record.update({"success": False, "elapsed_seconds": round(time.monotonic() - started, 2)})
        return record

async def main_async(args) -> None:
    urls = decode_urls(args.urls_b64)
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.monotonic()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            records = await asyncio.gather(
                *(render_one(browser, u, root, semaphore, args.retries + 1) for u in urls)
            )
        finally:
            await browser.close()
    manifest = {
        "format": "408-knowledge-original-browser-pdf-v1",
        "source": "https://csgraduates.com/",
        "shard": args.shard_name,
        "page_count": len(records),
        "success_count": sum(bool(r.get("success")) for r in records),
        "failed_count": sum(not r.get("success") for r in records),
        "warning_count": sum(bool(r.get("warnings")) for r in records),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "pages": records,
    }
    (root / "shard-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[PDF SHARD] " + json.dumps({k: v for k, v in manifest.items() if k != "pages"}), flush=True)
    if args.strict and (
        manifest["failed_count"] > 0 or any(r.get("warnings") for r in records)
    ):
        raise SystemExit("Browser PDF archive incomplete: see shard-manifest.json")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls-b64", required=True)
    parser.add_argument("--shard-name", default="408 knowledge")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()
