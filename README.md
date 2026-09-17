# crawl4ai test harness

A minimal GitHub Actions test harness for evaluating [Crawl4AI](https://github.com/unclecode/crawl4ai) against documentation pages.

## Current target

`https://csgraduates.com/constitution_principle/instruction/concepts/`

## What the action exports

The workflow `.github/workflows/crawl4ai-test.yml` installs the pinned Crawl4AI dependency and Chromium, runs `scripts/crawl_test.py`, then uploads `crawl4ai-csgraduates-result` containing:

- `content.md` — Crawl4AI raw Markdown output
- `fit-content.md` — fit Markdown when Crawl4AI produces it
- `cleaned.html` — cleaned page HTML
- `metadata.json` — status, title and output-size summary
- `links.json` — discovered links
- `media.json` — discovered media

The workflow also supports manual dispatch with a custom URL.

## Local usage

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python scripts/crawl_test.py --url "https://csgraduates.com/constitution_principle/instruction/concepts/"
```
