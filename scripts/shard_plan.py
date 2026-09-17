#!/usr/bin/env python3
"""Build deterministic archive shards from csgraduates.com sitemap URLs."""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

USER_AGENT = "Crawl4AI-Archive/2.0 (+https://github.com/ActiveInsighter/crawl4ai)"
SITEMAP_CANDIDATES = ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml")
CORE_LABELS = {
    "data_structure": "数据结构",
    "constitution_principle": "计算机组成原理",
    "operating_system": "操作系统",
    "computer_network": "计算机网络",
}
SKIP_RE = re.compile(r"(?:^|/)(?:404(?:\.html)?|search|tags?|categories?|authors?)(?:/|$)", re.I)
NON_DOCUMENT_SUFFIXES = {
    ".7z", ".avi", ".avif", ".bin", ".bmp", ".css", ".csv", ".doc", ".docx",
    ".eot", ".epub", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".map", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".otf", ".pdf", ".png",
    ".ppt", ".pptx", ".rar", ".rss", ".svg", ".tar", ".tgz", ".ttf", ".txt",
    ".wav", ".webm", ".webp", ".woff", ".woff2", ".xml", ".zip",
}


def _host_alias(host: str | None) -> str:
    value = (host or "").lower()
    return value[4:] if value.startswith("www.") else value


def canonicalize(url: str, base_url: str) -> str | None:
    base = urlsplit(base_url)
    parsed = urlsplit(urljoin(base_url, url.strip()))
    if parsed.scheme not in {"http", "https"}:
        return None
    if _host_alias(parsed.hostname) != _host_alias(base.hostname):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    suffix = Path(path).suffix.lower()
    if suffix in NON_DOCUMENT_SUFFIXES:
        return None
    if suffix and suffix not in {".html", ".htm"}:
        return None
    if SKIP_RE.search(path.strip("/")):
        return None
    return urlunsplit((base.scheme, base.netloc, path, "", ""))


def _fetch(url: str, accept: str) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    with urlopen(request, timeout=40) as response:
        return response.read(), response.geturl()


def _parse_sitemap(payload: bytes) -> tuple[str, list[str]]:
    root = ET.fromstring(payload)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    values: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
            values.append(node.text.strip())
    return kind, values


def discover_urls(base_url: str) -> list[str]:
    base_url = base_url.rstrip("/") + "/"
    queue: deque[str] = deque()
    seen: set[str] = set()
    urls: set[str] = set()

    try:
        robots, _ = _fetch(urljoin(base_url, "robots.txt"), "text/plain,*/*")
        for line in robots.decode("utf-8", errors="replace").splitlines():
            if line.lower().startswith("sitemap:"):
                candidate = line.split(":", 1)[1].strip()
                if candidate:
                    canonical = canonicalize(candidate, base_url)
                    if canonical:
                        queue.append(canonical)
    except Exception:
        pass

    for name in SITEMAP_CANDIDATES:
        queue.append(urljoin(base_url, name))

    while queue and len(seen) < 100:
        sitemap_url = queue.popleft()
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        try:
            payload, _resolved = _fetch(sitemap_url, "application/xml,text/xml,*/*")
            kind, values = _parse_sitemap(payload)
        except Exception:
            continue
        if kind == "sitemapindex":
            for value in values:
                parsed = urlsplit(value)
                if _host_alias(parsed.hostname) == _host_alias(urlsplit(base_url).hostname):
                    rewritten = urlunsplit((urlsplit(base_url).scheme, urlsplit(base_url).netloc, parsed.path, "", ""))
                    queue.append(rewritten)
        elif kind == "urlset":
            for value in values:
                normalized = canonicalize(value, base_url)
                if normalized:
                    urls.add(normalized)

    root = canonicalize(base_url, base_url)
    if root:
        urls.add(root)
    return sorted(urls, key=lambda value: (urlsplit(value).path.count("/"), urlsplit(value).path))


def _safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return value or "shard"


def _group_urls(urls: list[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    deferred_indexes: dict[str, list[str]] = defaultdict(list)
    other_urls: list[str] = []

    for url in urls:
        parts = [part for part in urlsplit(url).path.strip("/").split("/") if part]
        if not parts:
            other_urls.append(url)
            continue
        first = parts[0]
        if first in CORE_LABELS:
            if len(parts) == 1:
                deferred_indexes[first].append(url)
                continue
            second = parts[1]
            key = f"{first}/{second}"
            groups[key].append(url)
            labels[key] = f"{CORE_LABELS[first]} / {second}"
            continue
        if first == "study_methods":
            if len(parts) == 1:
                deferred_indexes[first].append(url)
                continue
            second = parts[1]
            key = f"study_methods/{second}"
            groups[key].append(url)
            labels[key] = f"学习资源 / {second}"
            continue
        other_urls.append(url)

    for top, index_urls in deferred_indexes.items():
        candidates = [key for key in groups if key.startswith(f"{top}/")]
        if candidates:
            target = max(candidates, key=lambda key: len(groups[key]))
            groups[target] = index_urls + groups[target]
        else:
            key = f"{top}/_index"
            groups[key].extend(index_urls)
            labels[key] = f"{CORE_LABELS.get(top, '学习资源')} / 首页"

    if other_urls:
        groups["other"] = other_urls
        labels["other"] = "其他站点内容"

    return groups, labels


def build_shards(urls: list[str], chunk_size: int = 35) -> list[dict[str, Any]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    groups, labels = _group_urls(urls)
    shards: list[dict[str, Any]] = []
    for key in sorted(groups):
        group_urls = sorted(set(groups[key]), key=lambda value: urlsplit(value).path)
        parts = max(1, math.ceil(len(group_urls) / chunk_size))
        for part_index in range(parts):
            chunk = group_urls[part_index * chunk_size : (part_index + 1) * chunk_size]
            if not chunk:
                continue
            suffix = f" [{part_index + 1}/{parts}]" if parts > 1 else ""
            shard_id = _safe_id(key.replace("/", "-"))
            if parts > 1:
                shard_id += f"-p{part_index + 1:02d}"
            payload = base64.urlsafe_b64encode(
                json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            shards.append(
                {
                    "id": shard_id,
                    "label": labels.get(key, key) + suffix,
                    "count": len(chunk),
                    "urls_b64": payload,
                    "group": key,
                }
            )
    return shards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://csgraduates.com/")
    parser.add_argument("--chunk-size", type=int, default=35)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    urls = discover_urls(args.base_url)
    shards = build_shards(urls, args.chunk_size)
    result = {
        "base_url": args.base_url,
        "url_count": len(urls),
        "shard_count": len(shards),
        "chunk_size": args.chunk_size,
        "shards": shards,
    }
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    matrix = json.dumps(shards, ensure_ascii=False, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"matrix={matrix}\n")
            handle.write(f"url_count={len(urls)}\n")
            handle.write(f"shard_count={len(shards)}\n")
    print(json.dumps({"url_count": len(urls), "shard_count": len(shards)}, ensure_ascii=False), flush=True)
    for shard in shards:
        print(f"[SHARD PLAN] {shard['label']}: {shard['count']} pages", flush=True)


if __name__ == "__main__":
    main()
