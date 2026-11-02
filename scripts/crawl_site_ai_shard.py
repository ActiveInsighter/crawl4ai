#!/usr/bin/env python3
"""Archive one AI-oriented shard with full browser-rendered HTML and no PDF."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urljoin

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


async def screenshot_dom_without_font_wait(page, locator, target_path: Path) -> None:
    """Capture the actual DOM rendering via Chrome DevTools, bypassing Playwright's
    indefinite fonts.ready wait. Clone into a temporary visible container, so
    formulas in collapsed panels can still be preserved without editing source.
    """
    marker = "c4a-fidelity-capture-overlay"
    details = await locator.evaluate(r"""(node) => {
        const existing = document.getElementById('c4a-fidelity-capture-overlay');
        if (existing) existing.remove();
        window.scrollTo(0, 0);
        const box = node.getBoundingClientRect();
        const view = node.viewBox?.baseVal;
        const width = Math.min(2200, Math.max(40, Math.ceil(box.width || view?.width || 800)));
        const height = Math.min(2800, Math.max(30, Math.ceil(box.height || view?.height || 120)));
        const overlay = document.createElement('div');
        overlay.id = 'c4a-fidelity-capture-overlay';
        overlay.style.cssText = 'position:absolute;left:0;top:0;z-index:2147483647;' +
            'background:white;pointer-events:none;overflow:hidden;box-sizing:border-box;';
        overlay.style.width = width + 'px';
        overlay.style.height = height + 'px';
        const cloned = node.cloneNode(true);
        cloned.removeAttribute('data-archive-formula-shot-id');
        cloned.removeAttribute('data-archive-svg-shot-id');
        const originalStyle = getComputedStyle(node);
        overlay.style.fontFamily = originalStyle.fontFamily;
        overlay.style.fontSize = originalStyle.fontSize;
        overlay.style.color = originalStyle.color;
        cloned.style.visibility = 'visible';
        cloned.style.display = 'block';
        cloned.style.opacity = '1';
        if (cloned.tagName.toLowerCase() === 'svg') {
            cloned.setAttribute('width', String(width));
            cloned.setAttribute('height', String(height));
        }
        overlay.appendChild(cloned);
        document.body.appendChild(overlay);
        return {width, height};
    }""")
    session = None
    try:
        session = await page.context.new_cdp_session(page)
        image = await session.send("Page.captureScreenshot", {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": True,
            "clip": {
                "x": 0, "y": 0,
                "width": details["width"],
                "height": details["height"],
                "scale": 1,
            },
        })
        payload = base64.b64decode(image["data"])
        if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) < 24:
            raise ValueError("CDP failed to capture a valid PNG")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
    finally:
        if session:
            await session.detach()
        await page.evaluate("""id => {
            document.getElementById(id)?.remove();
        }""", marker)


async def capture_unrecoverable_math(page, page_dir: Path) -> tuple[int, list[dict]]:
    """Use lossless PNG screenshots only when no authoritative TeX is available.

    Capture from the live page before changing the formula node; never infer LaTeX
    from a visually rendered expression or an adjacent formula.
    """
    targets = await page.evaluate(r"""() => {
        const candidates = document.querySelectorAll(
            '.td-content .katex, .td-content mjx-container, .td-content math'
        );
        const seen = new Set();
        const ids = [];
        for (const node of candidates) {
            if (node.tagName.toLowerCase() === 'math' &&
                (node.closest('.katex') || node.closest('mjx-container'))) continue;
            const target = node.closest('.katex-display') || node;
            if (seen.has(target)) continue;
            seen.add(target);
            const annotation = target.querySelector('annotation[encoding="application/x-tex"]');
            let known = Boolean(annotation && annotation.textContent.trim());
            if (!known) {
                for (const element of [target, node]) {
                    if (['data-tex', 'data-latex', 'data-math', 'alttext'].some(
                        key => Boolean(element.getAttribute(key)))) known = true;
                    if (element.querySelector('script[type^="math/tex"]')) known = true;
                }
            }
            if (!known) {
                const id = String(ids.length + 1).padStart(4, '0');
                target.setAttribute('data-archive-formula-shot-id', id);
                ids.push(id);
            }
        }
        return ids;
    }""")
    failures = []
    count = 0
    destination = page_dir / "assets" / "formulas"
    destination.mkdir(parents=True, exist_ok=True)
    for identifier in targets:
        locator = page.locator('[data-archive-formula-shot-id="' + identifier + '"]').first
        relative = f"assets/formulas/formula-{identifier}.png"
        try:
            await screenshot_dom_without_font_wait(page, locator, page_dir / relative)
            await locator.evaluate(
                "(el, relative) => el.setAttribute('data-archive-formula-image', relative)",
                relative,
            )
            count += 1
        except Exception as exc:
            failures.append({"formula_id": identifier, "error": str(exc)})
        finally:
            await _remove_formula_marker(page, identifier)
    return count, failures


async def _remove_formula_marker(page, identifier: str) -> None:
    await page.evaluate(
        """id => { const el = document.querySelector('[data-archive-formula-shot-id="' + id + '"]');
            if (el) el.removeAttribute('data-archive-formula-shot-id'); }""",
        identifier,
    )


async def _self_contained_svg(xml: str, base_url: str) -> tuple[str, list[dict]]:
    """Inline network <image> dependencies in browser-serialized XML."""
    root = ET.fromstring(xml)
    failures = []
    changed = False
    for node in root.iter():
        if node.tag.split("}")[-1] != "image":
            continue
        for attr, value in list(node.attrib.items()):
            if attr.split("}")[-1] != "href" or value.startswith(("#", "data:")):
                continue
            absolute = urljoin(base_url, value)
            try:
                payload, mime = await asyncio.to_thread(core.fetch_binary, absolute, base_url)
                mime = mime.split(";", 1)[0].strip().lower()
                if not payload or not mime.startswith("image/"):
                    raise ValueError("empty or invalid SVG image resource")
                if mime == "image/svg+xml":
                    ET.fromstring(payload)
                node.set(attr, "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii"))
                changed = True
            except Exception as exc:
                failures.append({"url": absolute, "error": str(exc)})
    return (ET.tostring(root, encoding="unicode") if changed else xml), failures


async def capture_svg_previews(page, page_dir: Path) -> tuple[int, list[dict], list[dict]]:
    """Save a DOM-serialized XML SVG and a live-browser screenshot per diagram."""
    ids = await page.evaluate(r"""() => {
        const nodes = document.querySelectorAll('.td-content svg');
        const ids = [];
        for (const svg of nodes) {
            if (svg.closest('.katex') || svg.closest('mjx-container')) continue;
            if (svg.parentElement?.closest('svg')) continue;
            if (svg.getAttribute('aria-hidden') === 'true') continue;
            if (['presentation', 'none'].includes(svg.getAttribute('role'))) continue;
            if (svg.classList.contains('kp-icon')) continue;
            const id = String(ids.length + 1).padStart(3, '0');
            svg.setAttribute('data-archive-svg-shot-id', id);
            ids.push(id);
        }
        return ids;
    }""")
    previews, failures, xml_assets = 0, [], []
    (page_dir / "assets" / "svg-previews").mkdir(parents=True, exist_ok=True)
    (page_dir / "assets" / "svg").mkdir(parents=True, exist_ok=True)
    for identifier in ids:
        locator = page.locator('svg[data-archive-svg-shot-id="' + identifier + '"]').first
        preview_path = f"assets/svg-previews/figure-{identifier}.png"
        svg_path = f"assets/svg/figure-{identifier}.svg"
        screenshot_available = False
        try:
            await screenshot_dom_without_font_wait(page, locator, page_dir / preview_path)
            await locator.evaluate(
                "(el, path) => el.setAttribute('data-archive-svg-preview', path)",
                preview_path,
            )
            previews += 1
            screenshot_available = True
        except Exception as exc:
            failures.append({"svg_id": identifier, "error": "SVG preview: " + str(exc)})
        try:
            xml = await locator.evaluate("(svg) => new XMLSerializer().serializeToString(svg)")
            repair_note = None
            try:
                ET.fromstring(xml)
            except ET.ParseError as exc:
                # foreignObject's permissive HTML parser may create illegal XML
                # attribute names such as '#000000;'. Strip only invalid names
                # from a clone; the exact browser DOM remains in the raw HTML.
                repair_note = "invalid HTML attributes in foreignObject: " + str(exc)
                try:
                    # Many draw.io exports contain a malformed XHTML
                    # foreignObject plus a *native* base64 image in <switch>.
                    # Keep every vector path and use that original embedded
                    # fallback image instead of rasterizing the entire SVG.
                    repaired = await locator.evaluate(r"""svg => {
                        const clone = svg.cloneNode(true);
                        let dropped = 0;
                        for (const node of Array.from(clone.querySelectorAll('foreignObject'))) {
                            const parent = node.parentElement;
                            if (parent?.localName !== 'switch') continue;
                            const fallback = Array.from(parent.children).some(
                                child => child !== node &&
                                ['image', 'text', 'g'].includes(child.localName)
                            );
                            if (fallback) {
                                node.remove();
                                dropped++;
                            }
                        }
                        if (!dropped) throw new Error('no native SVG switch fallback');
                        return new XMLSerializer().serializeToString(clone);
                    }""")
                    ET.fromstring(repaired)
                    xml = repaired
                    repair_note = "malformed XHTML replaced with original embedded SVG switch fallback"
                except Exception:
                    try:
                        repaired = await locator.evaluate(r"""svg => {
                            const clone = svg.cloneNode(true);
                            for (const node of clone.querySelectorAll('*')) {
                                for (const attr of Array.from(node.attributes)) {
                                    if (!/^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(attr.name)) {
                                        node.removeAttribute(attr.name);
                                    }
                                }
                            }
                            return new XMLSerializer().serializeToString(clone);
                        }""")
                        ET.fromstring(repaired)
                        xml = repaired
                    except Exception as repair_error:
                        # A valid XML wrapper around the actual screenshot preserves
                        # the exact visual when the original vector cannot be saved.
                        if not screenshot_available:
                            raise RuntimeError("No XML-safe vector or preview image") from repair_error
                        png = (page_dir / preview_path).read_bytes()
                        if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("Invalid PNG fallback")
                        width, height = struct.unpack(">II", png[16:24])
                        if not width or not height:
                            raise ValueError("Empty SVG screenshot dimensions")
                        image_data = base64.b64encode(png).decode("ascii")
                        xml = (
                            f'<svg xmlns="http://www.w3.org/2000/svg" '
                            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
                            f'<image width="{width}" height="{height}" '
                            f'href="data:image/png;base64,{image_data}"/></svg>'
                        )
                        repair_note += "; raster-backed SVG visual fallback"
            xml, dependencies = await _self_contained_svg(xml, page.url)
            failures.extend({"svg_id": identifier, **item} for item in dependencies)
            ET.fromstring(xml)
            (page_dir / svg_path).write_text(xml, encoding="utf-8")
            await locator.evaluate(
                "(el, source) => el.setAttribute('data-archive-svg-serialized', source)",
                svg_path,
            )
            xml_assets.append({"path": svg_path, "preview": preview_path,
                               "repaired": bool(repair_note)})
            if repair_note:
                failures.append({"svg_id": identifier, "warning": repair_note})
        except Exception as exc:
            failures.append({"svg_id": identifier, "error": "SVG serialization: " + str(exc)})
        finally:
            await page.evaluate(
                """id => { const svg = document.querySelector(
                    'svg[data-archive-svg-shot-id="' + id + '"]');
                    if (svg) svg.removeAttribute('data-archive-svg-shot-id'); }""",
                identifier,
            )
    return previews, failures, xml_assets


async def render_pages(
    urls: list[str],
    root: Path,
    concurrency: int,
    timeout_ms: int,
) -> dict[str, dict]:
    """Capture source DOM, annotated DOM and missing-math screenshots with retry."""
    results: dict[str, dict] = {}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )

        async def render_one(index: int, url: str) -> None:
            async with semaphore:
                started = time.perf_counter()
                page = await browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
                error = None
                try:
                    for attempt in range(3):
                        try:
                            # 'commit' avoids waiting for enormous pages to load every asset.
                            response = await page.goto(url, wait_until="commit", timeout=min(timeout_ms, 45_000))
                            if response is None or response.status >= 400:
                                raise RuntimeError(f"HTTP {response.status if response else 'no response'}")
                            await page.wait_for_selector(".td-content", state="attached", timeout=20_000)
                            await page.wait_for_function(
                                "() => document.readyState !== 'loading'", timeout=15_000,
                            )
                            await page.wait_for_timeout(650)
                            original_dom = await page.content()
                            page_dir = core.output_dir_for_url(url, root)
                            page_dir.mkdir(parents=True, exist_ok=True)
                            (page_dir / "browser-rendered.html").write_text(original_dom, encoding="utf-8")
                            capture_count, capture_errors = await capture_unrecoverable_math(page, page_dir)
                            svg_preview_count, svg_preview_errors, xml_assets = await capture_svg_previews(page, page_dir)
                            await page.evaluate("""() => {
                                document.querySelectorAll('.td-content img').forEach(img => {
                                    const src = img.currentSrc;
                                    if (src && /^https?:\/\//.test(src))
                                        img.setAttribute('data-archive-current-src', src);
                                });
                            }""")
                            annotated_dom = await page.content()
                            results[url] = {
                                "success": True,
                                "html": annotated_dom,
                                "resolved_url": page.url or url,
                                "status_code": response.status,
                                "html_chars": len(annotated_dom),
                                "formula_images": capture_count,
                                "formula_capture_failures": capture_errors,
                                "svg_preview_count": svg_preview_count,
                                "svg_preview_failures": svg_preview_errors,
                                "svg_xml_assets": xml_assets,
                                "elapsed_seconds": round(time.perf_counter() - started, 3),
                            }
                            print(
                                f"[RENDER {index:03d}/{len(urls):03d}] ✓ "
                                f"html={len(annotated_dom)} formulas={capture_count} {url}", flush=True,
                            )
                            break
                        except Exception as exc:
                            error = exc
                            if attempt < 2:
                                await asyncio.sleep(1 + attempt * 2)
                    else:
                        results[url] = {
                            "success": False,
                            "error": str(error),
                            "resolved_url": page.url or url,
                            "elapsed_seconds": round(time.perf_counter() - started, 3),
                        }
                        print(f"[RENDER {index:03d}/{len(urls):03d}] ✗ {url}: {error}", flush=True)
                finally:
                    await page.close()

        await asyncio.gather(*(render_one(i, url) for i, url in enumerate(urls, 1)))
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
            try:
                record, navigation = await core.process_page(
                    url,
                    root,
                    article_selector=args.article_selector,
                    retries=args.retries,
                )
            except Exception as exc:
                record, navigation = {"url": url, "success": False, "error": str(exc)}, []
        rendered = rendered_pages.get(url, {})
        record["browser_rendered"] = bool(rendered.get("success"))
        record["browser_status_code"] = rendered.get("status_code")
        record["browser_rendered_html_chars"] = rendered.get("html_chars", 0)
        record["browser_render_seconds"] = rendered.get("elapsed_seconds")
        if not rendered.get("success"):
            record["browser_render_error"] = rendered.get("error", "render failed")
        record["formula_images"] = rendered.get("formula_images", 0)
        record["formula_capture_failures"] = rendered.get("formula_capture_failures", [])
        record["svg_preview_count"] = rendered.get("svg_preview_count", 0)
        record["svg_preview_failures"] = rendered.get("svg_preview_failures", [])
        record["browser_svg_xml_assets"] = rendered.get("svg_xml_assets", [])
        issues = list(record.get("quality_issues", []))
        if not record.get("success"):
            issues.append("page_extract_failed")
        if not rendered.get("success"):
            issues.append("browser_render_failed")
        if record["formula_capture_failures"]:
            issues.append("formula_screenshot_failed")
        if any("error" in item for item in record["svg_preview_failures"]):
            issues.append("svg_capture_failed")
        if any("warning" in item for item in record["svg_preview_failures"]):
            issues.append("svg_vector_repaired_or_rasterized")
        if rendered.get("success") and record.get("svg_count", 0) > record["svg_preview_count"]:
            issues.append("missing_svg_visual_preview")
        record["quality_issues"] = sorted(set(issues))
        record["quality_pass"] = not issues

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
        "quality_pass_count": sum(1 for item in records if item.get("quality_pass")),
        "quality_issue_count": sum(1 for item in records if not item.get("quality_pass")),
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
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--render-concurrency", type=int, default=2)
    parser.add_argument("--render-timeout-ms", type=int, default=45_000)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    asyncio.run(archive(parse_args()))


if __name__ == "__main__":
    main()
