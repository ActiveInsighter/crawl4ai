#!/usr/bin/env python3
"""Lean HTTP-first page archiver for AI-readable csgraduates snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import xml.etree.ElementTree as ET
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlparse, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as html_to_markdown

ARTICLE_SELECTORS = (
    ".td-content",
    "main article",
    "article",
    "main[role='main']",
    "[role='main']",
    "main",
)
MATH_ATTRS = ("data-tex", "data-latex", "data-math", "alttext")
SOURCE_MATH_RE = re.compile(
    r"(?P<dollar>\$\$(?P<dollar_tex>.+?)\$\$)"
    r"|(?P<bracket>\\\[(?P<bracket_tex>.+?)\\\])"
    r"|(?P<paren>\\\((?P<paren_tex>.+?)\\\))",
    re.DOTALL,
)
HTTP_USER_AGENT = "Crawl4AI-AI-Archive/3.0 (+https://github.com/ActiveInsighter/crawl4ai)"
SAFE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg"}
WINDOWS_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# BeautifulSoup's HTML parser lowercases these names. They are case-sensitive
# again after an inline SVG is serialized as a standalone SVG/XML document.
SVG_TAG_CASE = {
    "foreignobject": "foreignObject",
    "lineargradient": "linearGradient",
    "radialgradient": "radialGradient",
    "clippath": "clipPath",
    "textpath": "textPath",
    "feblend": "feBlend",
    "fecolormatrix": "feColorMatrix",
    "fecomponenttransfer": "feComponentTransfer",
    "fecomposite": "feComposite",
    "feconvolvematrix": "feConvolveMatrix",
    "fediffuselighting": "feDiffuseLighting",
    "fedisplacementmap": "feDisplacementMap",
    "fedistantlight": "feDistantLight",
    "fedropshadow": "feDropShadow",
    "feflood": "feFlood",
    "fefunca": "feFuncA",
    "fefuncb": "feFuncB",
    "fefuncg": "feFuncG",
    "fefuncr": "feFuncR",
    "fegaussianblur": "feGaussianBlur",
    "feimage": "feImage",
    "femerge": "feMerge",
    "femergenode": "feMergeNode",
    "femorphology": "feMorphology",
    "feoffset": "feOffset",
    "fepointlight": "fePointLight",
    "fespecularlighting": "feSpecularLighting",
    "fespotlight": "feSpotLight",
    "fetile": "feTile",
    "feturbulence": "feTurbulence",
}
SVG_ATTR_CASE = {
    "viewbox": "viewBox",
    "preserveaspectratio": "preserveAspectRatio",
    "requiredfeatures": "requiredFeatures",
    "requiredextensions": "requiredExtensions",
    "systemlanguage": "systemLanguage",
    "markerheight": "markerHeight",
    "markerwidth": "markerWidth",
    "refx": "refX",
    "refy": "refY",
    "gradientunits": "gradientUnits",
    "gradienttransform": "gradientTransform",
    "patternunits": "patternUnits",
    "patterncontentunits": "patternContentUnits",
    "patterntransform": "patternTransform",
    "pathlength": "pathLength",
    "textlength": "textLength",
    "lengthadjust": "lengthAdjust",
    "startoffset": "startOffset",
    "attributename": "attributeName",
    "attributetype": "attributeType",
    "calcmode": "calcMode",
    "keypoints": "keyPoints",
    "keysplines": "keySplines",
    "keytimes": "keyTimes",
    "basefrequency": "baseFrequency",
    "kernelmatrix": "kernelMatrix",
    "kernelunitlength": "kernelUnitLength",
    "primitiveunits": "primitiveUnits",
    "filterunits": "filterUnits",
    "numoctaves": "numOctaves",
    "stddeviation": "stdDeviation",
    "tablevalues": "tableValues",
    "targetx": "targetX",
    "targety": "targetY",
    "viewtarget": "viewTarget",
    "zoomandpan": "zoomAndPan",
}


def text_value(value: Any) -> str:
    return "" if value is None else (value if isinstance(value, str) else str(value))


def safe_segment(value: str, fallback: str = "index") -> str:
    value = unquote(value).strip().strip(".")
    value = WINDOWS_BAD_CHARS_RE.sub("-", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._ ") or fallback
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = f"_{value}"
    return value[:120]


def output_dir_for_url(url: str, archive_root: Path) -> Path:
    path = urlsplit(url).path.strip("/")
    if not path:
        return archive_root / "pages" / "_root"
    parts = [safe_segment(part) for part in path.split("/") if part]
    if parts and Path(parts[-1]).suffix.lower() in {".html", ".htm"}:
        parts[-1] = safe_segment(Path(parts[-1]).stem)
    return archive_root / "pages" / Path(*parts)


def relative_page_dir(page_dir: Path, archive_root: Path) -> str:
    return PurePosixPath(page_dir.relative_to(archive_root)).as_posix()


def fetch_html(url: str) -> tuple[str, str]:
    request = Request(url, headers={
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    })
    with urlopen(request, timeout=35) as response:
        payload = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace"), response.geturl()


def fetch_binary(url: str, referer: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": HTTP_USER_AGENT, "Referer": referer})
    with urlopen(request, timeout=35) as response:
        return response.read(), response.headers.get_content_type() or ""


def select_article(soup: BeautifulSoup, preferred_selector: str | None = None) -> tuple[Tag, str]:
    selectors = ((preferred_selector,) if preferred_selector else ()) + ARTICLE_SELECTORS
    for selector in selectors:
        if not selector:
            continue
        try:
            candidates = [node for node in soup.select(selector) if node.get_text(" ", strip=True)]
        except Exception:
            continue
        if candidates:
            return max(candidates, key=lambda node: len(node.get_text(" ", strip=True))), selector
    return (soup.body if soup.body else soup), "body" if soup.body else "document"


def clean_article(article: Tag) -> None:
    # Preserve <style> nodes inside SVGs: many diagrams depend on their embedded
    # class rules. Removing them was one of the causes of broken exported SVGs.
    for node in article.select(
        "script, noscript, template, nav, footer, .td-page-meta, .td-toc, "
        ".feedback--answer, .taxonomy-terms-cloud"
    ):
        if node.name == "script" and (node.get("type") or "").startswith("math/tex"):
            continue
        node.decompose()
    for style in list(article.find_all("style")):
        if style.find_parent("svg") is None:
            style.decompose()


def normalize_links(article: Tag, base_url: str) -> None:
    for anchor in article.find_all("a", href=True):
        href = text_value(anchor.get("href")).strip()
        if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            anchor["href"] = urljoin(base_url, href)


def extract_source_formulas(source_html: str, preferred_selector: str | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(source_html, "html.parser")
    article, _ = select_article(soup, preferred_selector)
    clean_article(article)
    for node in article.select("pre, code, kbd, samp"):
        node.decompose()
    text = article.get_text("\n", strip=False)
    formulas: list[dict[str, Any]] = []
    for match in SOURCE_MATH_RE.finditer(text):
        if match.group("dollar") is not None:
            tex, display = match.group("dollar_tex"), True
        elif match.group("bracket") is not None:
            tex, display = match.group("bracket_tex"), True
        else:
            tex, display = match.group("paren_tex"), False
        tex = (tex or "").strip()
        if tex:
            formulas.append({"tex": tex, "display": display})
    return formulas


def _extract_tex(node: Tag) -> str:
    annotation = node.select_one('annotation[encoding="application/x-tex"]')
    if annotation and annotation.get_text("", strip=True):
        return annotation.get_text("", strip=True)
    for attr in MATH_ATTRS:
        value = text_value(node.get(attr)).strip()
        if value:
            return value
    script = node.find("script", attrs={"type": re.compile(r"^math/tex")})
    return script.get_text("", strip=True) if script else ""


def _is_display_math(node: Tag) -> bool:
    return (
        "katex-display" in (node.get("class") or [])
        or node.find_parent(class_="katex-display") is not None
        or text_value(node.get("display")).lower() in {"block", "true"}
    )


def replace_math_with_tokens(
    article: Tag,
    source_formulas: list[dict[str, Any]] | None = None,
    *,
    stats: dict[str, int] | None = None,
) -> dict[str, str]:
    """Use only authoritative TeX or a screenshot of the actual browser formula.

    Source formulas must never be mapped to rendered math by ordinal index:
    hidden elements and duplicated MathML nodes make such matching unsafe.
    """
    replacements: dict[str, str] = {}
    processed: set[int] = set()
    counts = {"total": 0, "tex": 0, "image": 0, "unresolved": 0}
    index = 0
    for node in list(article.select(".katex, mjx-container, math")):
        if node.parent is None or not any(parent is article for parent in node.parents):
            continue
        if node.name == "math" and (node.find_parent(class_="katex") or node.find_parent("mjx-container")):
            continue
        container = node
        if "katex" in (node.get("class") or []):
            display_parent = node.find_parent(class_="katex-display")
            if display_parent is not None:
                container = display_parent
        marker = id(container)
        if marker in processed or container.parent is None:
            continue
        processed.add(marker)
        index += 1
        counts["total"] += 1
        tex = _extract_tex(node) or _extract_tex(container)
        if not tex:
            annotation = container.select_one('annotation[encoding="application/x-tex"]')
            if annotation:
                tex = annotation.get_text("", strip=True)
        token = f"C4AMATHTOKEN{index:04d}END"
        if tex:
            replacements[token] = (f"$$\n{tex}\n$$" if _is_display_math(container) else "$" + tex + "$")
            counts["tex"] += 1
        else:
            fallback = text_value(container.get("data-archive-formula-image") or node.get("data-archive-formula-image"))
            if fallback.startswith("assets/formulas/") and ".." not in fallback:
                replacements[token] = f"![Rendered mathematical formula]({fallback})"
                counts["image"] += 1
            else:
                counts["unresolved"] += 1
                # Do not guess TeX: leave the original DOM, and report for review.
                continue
        container.replace_with(NavigableString(token))
    if stats is not None:
        stats.update(counts)
    return replacements


def restore_svg_case(svg: Tag) -> None:
    for node in [svg, *svg.find_all(True)]:
        canonical_tag = SVG_TAG_CASE.get(node.name.lower())
        if canonical_tag:
            node.name = canonical_tag
        for key in list(node.attrs):
            canonical_attr = SVG_ATTR_CASE.get(key.lower())
            if canonical_attr and canonical_attr != key:
                node.attrs[canonical_attr] = node.attrs.pop(key)
    if not svg.get("xmlns"):
        svg["xmlns"] = "http://www.w3.org/2000/svg"
    if any("xlink:" in str(key) for node in [svg, *svg.find_all(True)] for key in node.attrs):
        svg["xmlns:xlink"] = "http://www.w3.org/1999/xlink"


def _is_decorative_svg(svg: Tag) -> bool:
    return (
        text_value(svg.get("aria-hidden")).lower() == "true"
        or text_value(svg.get("role")).lower() in {"none", "presentation"}
        or "kp-icon" in set(svg.get("class") or [])
    )


async def inline_svg_images(article: Tag, base_url: str) -> list[dict[str, str]]:
    """Embed external SVG <image> resources as data URIs for offline rendering."""
    failures: list[dict[str, str]] = []
    for image in article.select("svg image"):
        attr = next((key for key in ("href", "xlink:href") if image.get(key)), None)
        if not attr:
            continue
        original = text_value(image.get(attr)).strip()
        if not original or original.startswith(("#", "data:")):
            continue
        absolute = urljoin(base_url, original)
        try:
            if not absolute.startswith(("https://", "http://")):
                raise ValueError("unsupported SVG image URL")
            payload, mime = await asyncio.to_thread(fetch_binary, absolute, base_url)
            mime = mime.split(";", 1)[0].strip().lower()
            if not payload or not mime.startswith("image/"):
                raise ValueError("empty or non-image SVG dependency")
            if mime == "image/svg+xml":
                ET.fromstring(payload)
            image[attr] = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        except Exception as exc:
            failures.append({"url": absolute, "error": str(exc)})
    return failures


def export_inline_svgs(article: Tag, output_dir: Path) -> tuple[list[dict[str, Any]], int]:
    manifest: list[dict[str, Any]] = []
    skipped = 0
    for svg in list(article.find_all("svg")):
        if svg.parent is None:
            continue
        if _is_decorative_svg(svg):
            skipped += 1
            svg.decompose()
            continue
        preview = text_value(svg.attrs.pop("data-archive-svg-preview", "")).strip()
        restore_svg_case(svg)
        index = len(manifest) + 1
        svg_dir = output_dir / "assets" / "svg"
        svg_dir.mkdir(parents=True, exist_ok=True)
        path = svg_dir / f"figure-{index:03d}.svg"
        serialized = str(svg)
        ET.fromstring(serialized)
        path.write_text(serialized, encoding="utf-8")
        relative = PurePosixPath("assets", "svg", path.name).as_posix()
        title = svg.find("title")
        desc = svg.find("desc")
        visible = svg.get_text(" ", strip=True)
        alt = ((title.get_text(" ", strip=True) if title else "") or (desc.get_text(" ", strip=True) if desc else "") or visible or f"SVG figure {index}")[:240]
        image = article.new_tag("img", src=relative, alt=alt)
        svg.replace_with(image)
        manifest.append({"path": relative, "bytes": path.stat().st_size, "preview": preview})
    return manifest, skipped


def _image_source(img: Tag) -> str:
    """Skip lazy-loading placeholders when the real image URL is available."""
    source = text_value(img.get("src")).strip()
    lazy = next((text_value(img.get(k)).strip() for k in
                 ("data-src", "data-original", "data-lazy-src")
                 if text_value(img.get(k)).strip()), "")
    if lazy and (not source or source.startswith(("data:", "blob:"))):
        return lazy
    if source:
        return source
    srcset = text_value(img.get("srcset") or img.get("data-srcset")).strip()
    if srcset:
        return srcset.split(",")[-1].strip().split()[0]
    return lazy


def _image_filename(url: str, content_type: str) -> str:
    raw = unquote(Path(urlparse(url).path).name) or "image"
    suffix = Path(raw).suffix.lower()
    if suffix not in SAFE_EXTENSIONS:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ""
        suffix = guessed if guessed in SAFE_EXTENSIONS else ".bin"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(raw).stem).strip("-._") or "image"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{stem[:80]}-{digest}{suffix}"


async def localize_images(article: Tag, base_url: str, output_dir: Path) -> tuple[int, list[dict[str, str]]]:
    cache: dict[str, str] = {}
    count = 0
    failures: list[dict[str, str]] = []
    for img in article.find_all("img"):
        source = _image_source(img)
        if not source or source.startswith(("data:", "blob:", "assets/")):
            continue
        absolute = urljoin(base_url, source)
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute in cache:
            img["src"] = cache[absolute]
            img.attrs.pop("srcset", None)
            continue
        try:
            payload, content_type = await asyncio.to_thread(fetch_binary, absolute, base_url)
            image_dir = output_dir / "assets" / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            filename = _image_filename(absolute, content_type)
            if not payload or not content_type.split(";", 1)[0].strip().lower().startswith("image/"):
                raise ValueError("image fetch returned empty or non-image data")
            if filename.lower().endswith(".svg"):
                ET.fromstring(payload)
            (image_dir / filename).write_bytes(payload)
            relative = PurePosixPath("assets", "images", filename).as_posix()
            cache[absolute] = relative
            img["src"] = relative
            img.attrs.pop("srcset", None)
            for attr in ("data-src", "data-original", "data-lazy-src"):
                img.attrs.pop(attr, None)
            count += 1
        except Exception as exc:
            img["src"] = absolute
            failures.append({"url": absolute, "error": str(exc)})
    return count, failures


def _title_from_soup(soup: BeautifulSoup, fallback: str) -> str:
    if soup.title and soup.title.get_text(" ", strip=True):
        return soup.title.get_text(" ", strip=True)
    heading = soup.select_one(".td-content h1, main h1, article h1, h1")
    return heading.get_text(" ", strip=True) if heading else fallback


def _sidebar_anchor(li: Tag) -> Tag | None:
    for child in li.children:
        if isinstance(child, Tag) and child.name == "a" and child.get("href"):
            return child
        if isinstance(child, Tag) and child.name == "label":
            anchor = child.find("a", href=True, recursive=False)
            if anchor is not None:
                return anchor
    return li.find("a", href=True)


def _sidebar_ul(li: Tag) -> Tag | None:
    for child in li.children:
        if isinstance(child, Tag) and child.name == "ul":
            return child
    return None


def extract_navigation(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one("nav.td-sidebar-nav")
    root = nav.find("ul", recursive=False) if nav else None
    if root is None:
        return []

    def parse(ul: Tag) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for li in ul.find_all("li", recursive=False):
            anchor = _sidebar_anchor(li)
            if anchor is None:
                continue
            href = urljoin(base_url, text_value(anchor.get("href")))
            parsed = urlsplit(href)
            href = parsed._replace(query="", fragment="").geturl()
            child = _sidebar_ul(li)
            items.append({
                "title": anchor.get_text(" ", strip=True) or href,
                "url": href,
                "children": parse(child) if child else [],
            })
        return items
    return parse(root)


def count_navigation(items: list[dict[str, Any]]) -> int:
    return sum(1 + count_navigation(item.get("children", [])) for item in items)


async def process_page(url: str, archive_root: Path, article_selector: str = ".td-content", retries: int = 2) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = asyncio.get_running_loop().time()
    last_error: Exception | None = None
    html = ""
    resolved = url
    for attempt in range(retries + 1):
        try:
            html, resolved = await asyncio.to_thread(fetch_html, url)
            if not html.strip():
                raise RuntimeError("empty response")
            break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
    if not html:
        return ({"url": url, "success": False, "error": str(last_error or "fetch failed")}, [])

    page_dir = output_dir_for_url(url, archive_root)
    page_dir.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(html, "html.parser")
    title = _title_from_soup(soup, url)
    # Never match TeX to rendered DOM by its position alone.
    article, selector = select_article(soup, article_selector)
    clean_article(article)
    normalize_links(article, resolved)
    svg_dependency_failures = await inline_svg_images(article, resolved)
    svgs, skipped_svgs = export_inline_svgs(article, page_dir)
    image_count, image_failures = await localize_images(article, resolved, page_dir)

    # Keep formula DOM intact in HTML so an AI can recover visual formula text.
    content_html = str(article)
    (page_dir / "content.html").write_text(content_html, encoding="utf-8")

    md_soup = BeautifulSoup(content_html, "html.parser")
    md_article, _ = select_article(md_soup, article_selector)
    math_stats: dict[str, int] = {}
    replacements = replace_math_with_tokens(md_article, stats=math_stats)
    markdown = html_to_markdown(str(md_article), heading_style="ATX", bullets="-").strip()
    for token, latex in replacements.items():
        markdown = markdown.replace(token, latex)
    (page_dir / "content.md").write_text(markdown + "\n", encoding="utf-8")

    record = {
        "url": url,
        "resolved_url": resolved,
        "title": title,
        "success": True,
        "output_dir": relative_page_dir(page_dir, archive_root),
        "article_selector": selector,
        "markdown_chars": len(markdown),
        "html_chars": len(content_html),
        "svg_count": len(svgs),
        "svg_assets": svgs,
        "decorative_svg_skipped": skipped_svgs,
        "image_count": image_count,
        "image_failures": image_failures,
        "svg_dependency_failures": svg_dependency_failures,
        "math": math_stats,
        "elapsed_seconds": round(asyncio.get_running_loop().time() - started, 3),
    }
    issues = []
    if selector in {"body", "document"}:
        issues.append("article_selector_fell_back_to_body")
    if len(markdown.strip()) < 80:
        issues.append("short_or_empty_markdown")
    if image_failures:
        issues.append("image_download_failed")
    if svg_dependency_failures:
        issues.append("svg_external_dependency_failed")
    if math_stats["unresolved"]:
        issues.append("unresolved_math")
    record["quality_issues"] = issues
    record["quality_pass"] = not issues
    return record, extract_navigation(html, resolved)
