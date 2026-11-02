#!/usr/bin/env python3
"""Export all 408 annual papers with local diagrams and visual math to MDX."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

try:
    from .quiz_assets import FIGURES_JS, FORMULA_JS, embedded_katex_css, formula_svg, serialize_svg
    from .quiz_mdx import convert_article, validate_article
except ImportError:
    from quiz_assets import FIGURES_JS, FORMULA_JS, embedded_katex_css, formula_svg, serialize_svg
    from quiz_mdx import convert_article, validate_article

BASE = "https://csgraduates.com/study_methods/408quiz/"


def validate_years(years: list[int]) -> None:
    if (not years or years != list(range(2009, max(years) + 1))
            or not set(range(2009, 2027)).issubset(years)):
        raise ValueError(f"Incomplete annual coverage, require 2009–2026: {years}")


def save_asset(root: Path, relative: str, payload: bytes, **metadata) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {"path": relative, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), **metadata}


async def open_page(page, url: str, year: int | None = None):
    last_error = None
    for attempt in range(4):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status >= 400:
                raise ValueError(f"HTTP {response.status if response else 'unknown'}")
            await page.wait_for_selector(".td-content", timeout=15000)
            if year is not None:
                title = await page.title()
                if str(year) not in title or "408" not in title:
                    raise ValueError(f"Unexpected title {title}")
                validate_article(await page.content(), year)
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"{url}: {last_error}")


async def discover_years(page) -> list[int]:
    await open_page(page, BASE)
    hrefs = await page.locator("a[href]").evaluate_all("els=>els.map(e=>e.href)")
    years = sorted({int(m.group(1)) for href in hrefs
                    if (m := re.fullmatch(re.escape(BASE) + r"(\d{4})/", href))})
    validate_years(years)
    return years


async def export_year(page, year: int, root: Path, css: str | None) -> tuple[dict, str]:
    await open_page(page, f"{BASE}{year}/", year)
    original = await page.content()
    counts = validate_article(original, year)
    save_asset(root, f"sources/{year}.html", original.encode(), kind="source")
    article = page.locator(".td-content").first
    # Revealing already-present answers is essential: hidden math has no bounds.
    await article.evaluate("el=>{for(const n of el.querySelectorAll('.explanation,.solution-detail'))n.style.setProperty('display','block','important')}")
    await page.evaluate("document.fonts.ready")
    css = css or await embedded_katex_css(page)
    math = await article.evaluate(FORMULA_JS)
    figures = await article.evaluate(FIGURES_JS)
    soup = BeautifulSoup(await article.inner_html(), "html.parser")
    holder = soup.new_tag("div", attrs={"class": "td-content"})
    for node in list(soup.contents):
        holder.append(node)
    soup.append(holder)
    assets = []
    question_figures = Counter()
    for figure in figures:
        q = figure["question"]
        question_figures[q] += 1
        relative = f"assets/figures/{year}-{q}-{question_figures[q]:02d}.svg"
        payload = serialize_svg(figure["xml"]).encode()
        assets.append(save_asset(root, relative, payload, kind="diagram", question=q))
        node = soup.select_one(f'[data-archive-figure="{figure["id"]}"]')
        image = soup.new_tag("img", src="./" + relative, alt=figure["alt"])
        node.replace_with(image)
    for formula in math["formulas"]:
        payload = formula_svg(formula, css).encode()
        digest = hashlib.sha256(payload).hexdigest()[:24]
        relative = f"assets/formulas/{digest}.svg"
        assets.append(save_asset(root, relative, payload, kind="formula",
                                 question=formula["question"], display=formula["display"]))
        node = soup.select_one(f'[data-archive-formula="{formula["id"]}"]')
        image = soup.new_tag("img", src="./" + relative,
                             alt=f'{year} 年第 {formula["question"]} 题公式（排版见图）')
        image["data-formula"] = "true"
        node.replace_with(image)
    images = [i for i in soup.select("img") if not i.get("src", "").startswith("./assets/")]
    for index, image in enumerate(images, 1):
        src = image.get("src") or image.get("data-src")
        if not src:
            raise ValueError(f"{year}: image without source")
        url = urljoin(f"{BASE}{year}/", src)
        response = await page.request.get(url, timeout=45000)
        if not response.ok or not response.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"Image fetch failed: {url}")
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
            raise ValueError(f"Unsupported image type: {url}")
        relative = f"assets/images/{year}-{index:02d}{suffix}"
        payload = await response.body()
        if not payload:
            raise ValueError(f"Empty image: {url}")
        assets.append(save_asset(root, relative, payload, kind="image", source_url=url))
        image["src"] = "./" + relative
        image["alt"] = image.get("alt") or f"{year} 年题目图片 {index}"
    mdx = convert_article(str(holder), year, {})
    (root / f"{year}.mdx").write_text(mdx, encoding="utf-8")
    record = {"year": year, "source_url": f"{BASE}{year}/", "mdx": f"{year}.mdx",
              "mdx_sha256": hashlib.sha256(mdx.encode()).hexdigest(),
              **counts, "diagram_count": len(figures), "formula_count": len(math["formulas"]),
              "image_count": len(images), "assets": assets}
    (root / "sources" / f"{year}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"[408] {year}: 47 questions, {len(figures)} diagrams, "
          f"{len(math['formulas'])} formulas, {len(images)} images", flush=True)
    return record, css


def validate_package(root: Path, years: list[int]) -> dict:
    records = []
    for year in years:
        record = json.loads((root / "sources" / f"{year}.json").read_text())
        if record["year"] != year:
            raise ValueError("Year manifest mismatch")
        validate_article((root / "sources" / f"{year}.html").read_text(), year)
        mdx = (root / f"{year}.mdx").read_text()
        if re.findall(r"(?m)^## 第 (\d+) 题$", mdx) != [str(q) for q in range(1, 48)]:
            raise ValueError(f"{year}: MDX missing question headings")
        for relative in re.findall(r'<img\s+src="([^"]+)"', mdx):
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"{year}: invalid image reference {relative}")
        for asset in record["assets"]:
            path = root / asset["path"]
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Asset path escapes package")
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValueError(f"Asset hash mismatch: {path}")
            if path.suffix == ".svg":
                ET.parse(path)
        if hashlib.sha256(mdx.encode()).hexdigest() != record.get("mdx_sha256"):
            raise ValueError(f"{year}: MDX body hash missing or mismatched")
        records.append(record)
    return {"years": years, "year_count": len(years), "question_count": len(years) * 47,
            "diagram_count": sum(r["diagram_count"] for r in records),
            "formula_occurrences": sum(r["formula_count"] for r in records),
            "unique_formula_files": len({a["path"] for r in records for a in r["assets"] if a["kind"] == "formula"}),
            "image_count": sum(r["image_count"] for r in records), "pages": records,
            "validated_at": datetime.now(timezone.utc).isoformat(), "all_checks_passed": True}


async def run(args):
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path=args.browser_executable,
                                           args=["--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        css = None
        try:
            years = args.years or await discover_years(page)
            for year in years:
                if args.resume and (root / "sources" / f"{year}.json").exists():
                    try:
                        validate_package(root, [year])
                        print(f"[408] {year}: resume validated", flush=True)
                        continue
                    except (OSError, ValueError, ET.ParseError, KeyError):
                        pass
                for attempt in range(3):
                    try:
                        _, css = await export_year(page, year, root, css)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(2 ** attempt)
        finally:
            await browser.close()
    manifest = validate_package(root, years)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (root / "index.mdx").write_text('---\ntitle: "408 历年真题"\n---\n\n# 408 历年真题\n\n'
                                    + "\n".join(f"- [{y} 年 408 真题](./{y}.mdx)" for y in years) + "\n")
    (root / "meta.json").write_text(json.dumps({"title": "408 历年真题", "pages": ["index", *map(str, years)]}, ensure_ascii=False, indent=2))
    license_file = Path(__file__).parents[1] / "licenses" / "KaTeX-0.16.10.txt"
    shutil.copy2(license_file, root / "KaTeX-LICENSE.txt")
    (root / "README.md").write_text(
        "# 408 历年真题 MDX\n\n"
        f"包含 {len(years)} 年、{len(years)*47} 道题，题干、选项、答案和解析。\n\n"
        "每个年份一个 MDX；图片使用本地相对路径 `<img src=\"./assets/...svg\" alt=\"...\" />`。\n"
        "保留 assets 目录与 MDX 的相对位置。网页框架若不处理相对图片地址，请将 assets 复制到 public 并调整 src 前缀。\n\n"
        "公式为嵌入 KaTeX 字体的独立 SVG，完整保留分式和上下标；不是猜测的 LaTeX。\n"
        "SVG foreignObject 需要现代浏览器支持；可直接在浏览器中查看 SVG。\n"
        "MDX 编译建议使用 remark-gfm（表格）和 remark-frontmatter（元数据）。\n\n"
        "sources/ 保存抓取 DOM 和逐年清单；manifest.json 包含完整性结果、图片路径与 SHA-256。\n"
        "原站内容与答案未做官方原卷逐题校对。\n", encoding="utf-8")
    # The real MDX parser catches JSX/code/table errors before packaging.
    subprocess.run(["node", str(Path(__file__).with_name("validate_408_mdx.mjs")),
                    str(root)], check=True)
    # Archive only after all requested years and all referenced assets validate.
    files = {"index.mdx", "meta.json", "README.md", "manifest.json", "mdx-validation.json", "KaTeX-LICENSE.txt"}
    for record in manifest["pages"]:
        year = record["year"]
        files.update({f"{year}.mdx", f"sources/{year}.html", f"sources/{year}.json"})
        files.update(a["path"] for a in record["assets"])
    zip_path = root.with_suffix(".zip")
    temporary_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(files):
            archive.write(root / relative, f"{root.name}/{relative}")
    temporary_zip.replace(zip_path)
    print(f"[PACKAGE] {zip_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/408-mdx"))
    parser.add_argument("--years", type=lambda value: sorted({int(y) for y in value.split(",")}))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--browser-executable")
    args = parser.parse_args()
    if args.years and any(y < 2009 or y > datetime.now().year + 1 for y in args.years):
        parser.error("Invalid exam year")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
