#!/usr/bin/env python3
"""Extract the site's real Docsy sidebar hierarchy from archived browser HTML."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag


def normalize_href(href: str, base_url: str) -> str:
    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)
    return parsed._replace(query="", fragment="").geturl()


def _direct_anchor(li: Tag) -> Tag | None:
    for child in li.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "a" and child.get("href"):
            return child
        if child.name == "label":
            anchor = child.find("a", href=True, recursive=False)
            if anchor is not None:
                return anchor
    return li.find("a", href=True)


def _direct_child_ul(li: Tag) -> Tag | None:
    for child in li.children:
        if isinstance(child, Tag) and child.name == "ul":
            return child
    return None


def parse_sidebar_ul(
    ul: Tag,
    *,
    base_url: str,
    archived_pages: dict[str, str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for li in ul.find_all("li", recursive=False):
        anchor = _direct_anchor(li)
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        href = normalize_href(str(anchor.get("href") or ""), base_url)
        classes = set(anchor.get("class") or [])
        child_ul = _direct_child_ul(li)
        output_dir = archived_pages.get(href)
        item: dict[str, Any] = {
            "title": title or href,
            "url": href,
            "kind": "section" if "td-sidebar-link__section" in classes else "page",
            "children": (
                parse_sidebar_ul(
                    child_ul,
                    base_url=base_url,
                    archived_pages=archived_pages,
                )
                if child_ul is not None
                else []
            ),
        }
        if output_dir:
            item["output_dir"] = output_dir
            item["markdown"] = f"{output_dir}/content.md"
            item["pdf"] = f"{output_dir}/browser-rendered.pdf"
        items.append(item)
    return items


def parse_sidebar_document(
    html: str,
    *,
    base_url: str,
    archived_pages: dict[str, str],
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one("nav.td-sidebar-nav")
    if nav is None:
        return []
    root_ul = nav.find("ul", recursive=False)
    if root_ul is None:
        return []
    return parse_sidebar_ul(root_ul, base_url=base_url, archived_pages=archived_pages)


def count_entries(items: list[dict[str, Any]]) -> int:
    return sum(1 + count_entries(item.get("children", [])) for item in items)


def collect_navigation(archive_root: Path, base_url: str) -> dict[str, Any]:
    pages_file = archive_root / "pages.json"
    records = json.loads(pages_file.read_text(encoding="utf-8")) if pages_file.exists() else []
    archived_pages = {
        normalize_href(str(record["url"]), base_url): str(record["output_dir"])
        for record in records
        if record.get("success") and record.get("url") and record.get("output_dir")
    }

    sections: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.get("success") or not record.get("output_dir"):
            continue
        html_path = archive_root / str(record["output_dir"]) / "browser-rendered.html"
        if not html_path.exists():
            continue
        items = parse_sidebar_document(
            html_path.read_text(encoding="utf-8"),
            base_url=base_url,
            archived_pages=archived_pages,
        )
        for item in items:
            key = str(item.get("url") or item.get("title"))
            previous = sections.get(key)
            if previous is None or count_entries([item]) > count_entries([previous]):
                sections[key] = item

    ordered = sorted(
        sections.values(),
        key=lambda item: (urlsplit(str(item.get("url", ""))).path, str(item.get("title", ""))),
    )
    return {
        "base_url": base_url,
        "section_count": len(ordered),
        "entry_count": count_entries(ordered),
        "sections": ordered,
    }


def render_markdown(navigation: dict[str, Any]) -> str:
    lines = ["# csgraduates.com navigation", ""]

    def render(items: list[dict[str, Any]], depth: int) -> None:
        indent = "  " * depth
        for item in items:
            title = str(item.get("title") or item.get("url"))
            url = str(item.get("url") or "")
            local = item.get("markdown")
            pdf = item.get("pdf")
            suffix_parts = []
            if local:
                suffix_parts.append(f"[Markdown]({local})")
            if pdf:
                suffix_parts.append(f"[PDF]({pdf})")
            suffix = " — " + " · ".join(suffix_parts) if suffix_parts else ""
            lines.append(f"{indent}- [{title}]({url}){suffix}")
            render(item.get("children", []), depth + 1)

    render(navigation.get("sections", []), 0)
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=Path("artifacts/site"))
    parser.add_argument("--base-url", default="https://csgraduates.com/")
    args = parser.parse_args()

    navigation = collect_navigation(args.archive_root, args.base_url)
    (args.archive_root / "navigation.json").write_text(
        json.dumps(navigation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.archive_root / "navigation.md").write_text(
        render_markdown(navigation), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in navigation.items() if k != "sections"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
