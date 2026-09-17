#!/usr/bin/env python3
"""Build a balanced archive matrix: four 408 subjects + study-resource groups."""

from __future__ import annotations

import argparse
import base64
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shard_plan import CORE_LABELS, _safe_id, discover_urls


def build_balanced_shards(
    urls: list[str],
    chunk_size: int = 35,
    tiny_study_threshold: int = 4,
) -> list[dict[str, Any]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    groups: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}

    for url in urls:
        parts = [part for part in urlsplit(url).path.strip("/").split("/") if part]
        if not parts:
            groups["other"].append(url)
            labels["other"] = "其他站点内容"
            continue

        first = parts[0]
        if first in CORE_LABELS:
            key = f"core/{first}"
            groups[key].append(url)
            labels[key] = CORE_LABELS[first]
            continue

        if first == "study_methods":
            if len(parts) == 1:
                groups["study_methods/_index"].append(url)
                labels["study_methods/_index"] = "学习资源 / 首页"
            else:
                second = parts[1]
                key = f"study_methods/{second}"
                groups[key].append(url)
                labels[key] = f"学习资源 / {second}"
            continue

        groups["other"].append(url)
        labels["other"] = "其他站点内容"

    tiny_keys = [
        key
        for key, values in groups.items()
        if key.startswith("study_methods/")
        and key != "study_methods/_index"
        and len(values) <= tiny_study_threshold
    ]
    if tiny_keys:
        misc: list[str] = []
        for key in tiny_keys:
            misc.extend(groups.pop(key))
            labels.pop(key, None)
        groups["study_methods/_misc"] = misc
        labels["study_methods/_misc"] = "学习资源 / misc"

    if "study_methods/_index" in groups:
        study_candidates = [
            key for key in groups
            if key.startswith("study_methods/") and key != "study_methods/_index"
        ]
        if study_candidates:
            target = max(study_candidates, key=lambda key: len(groups[key]))
            groups[target] = groups.pop("study_methods/_index") + groups[target]
            labels.pop("study_methods/_index", None)

    shards: list[dict[str, Any]] = []
    for key in sorted(groups):
        group_urls = sorted(set(groups[key]), key=lambda value: urlsplit(value).path)
        part_count = max(1, math.ceil(len(group_urls) / chunk_size))
        for part_index in range(part_count):
            chunk = group_urls[part_index * chunk_size : (part_index + 1) * chunk_size]
            if not chunk:
                continue
            suffix = f" [{part_index + 1}/{part_count}]" if part_count > 1 else ""
            shard_id = _safe_id(key.replace("/", "-"))
            if part_count > 1:
                shard_id += f"-p{part_index + 1:02d}"
            urls_b64 = base64.urlsafe_b64encode(
                json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            shards.append(
                {
                    "id": shard_id,
                    "label": labels.get(key, key) + suffix,
                    "count": len(chunk),
                    "urls_b64": urls_b64,
                    "group": key,
                }
            )
    return shards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://csgraduates.com/")
    parser.add_argument("--chunk-size", type=int, default=35)
    parser.add_argument("--tiny-study-threshold", type=int, default=4)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    urls = discover_urls(args.base_url)
    shards = build_balanced_shards(urls, args.chunk_size, args.tiny_study_threshold)
    result = {
        "base_url": args.base_url,
        "url_count": len(urls),
        "shard_count": len(shards),
        "chunk_size": args.chunk_size,
        "tiny_study_threshold": args.tiny_study_threshold,
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
