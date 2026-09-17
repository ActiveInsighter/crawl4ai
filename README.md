# crawl4ai test harness

A GitHub Actions test harness for evaluating [Crawl4AI](https://github.com/unclecode/crawl4ai) against documentation pages while preserving formulas, images and inline SVG diagrams for downstream AI/document workflows.

## Current target

`https://csgraduates.com/constitution_principle/instruction/concepts/`

The default article selector is `.td-content`; it can be overridden from `workflow_dispatch` or with `--article-selector` locally.

## Two-track export pipeline

The project deliberately keeps **semantic extraction** and **visual preservation** separate.

### 1. AI / Markdown track

1. Fetch the HTTP source and crawl the rendered page with Crawl4AI + Chromium.
2. Select the article root instead of converting the whole page shell.
3. Recover formula TeX only when authoritative TeX data is actually present in KaTeX/MathJax/MathML annotations, TeX attributes, or original source delimiters.
4. Never invent TeX when the served source no longer contains it; record the unrecovered formula in `formulas.json`.
5. Export content SVG diagrams as standalone `.svg` files, skip decorative SVG icons, download ordinary images, and rewrite asset paths locally.
6. Generate `content.md` for AI/RAG/document workflows.

### 2. Browser-fidelity track

Some pages render KaTeX correctly in Chrome even though the original TeX source is no longer available. For those pages, exact source reconstruction is unnecessary for visual preservation.

The Action therefore opens the **original URL directly in Playwright Chromium**, waits for the live `.katex` DOM to finish rendering, and then, from that same live browser page:

1. saves `page.content()` as `browser-rendered.html`;
2. records rendered KaTeX text/counts;
3. calls Chromium `page.pdf()` directly and saves `browser-rendered.pdf`.

There is no Markdown/HTML round trip in this PDF path. It mirrors the browser-print approach and preserves the already-rendered formula visually.

## Full-site archive

`scripts/crawl_site.py` expands the single-page proof of concept into a complete `csgraduates.com` documentation archive.

Discovery is automatic:

1. fetch `robots.txt`;
2. read every declared sitemap plus common sitemap names;
3. recursively expand sitemap indexes;
4. keep same-origin document URLs and discard media/static assets and obvious taxonomy/search pages;
5. fall back to a same-origin breadth-first link crawl only if no usable sitemap exists;
6. honor `robots.txt` by default.

The site crawler then:

1. crawls all selected URLs with one shared Crawl4AI browser and `arun_many()`;
2. rate-limits requests and caps concurrent browser sessions;
3. maps each original URL path directly into the local archive directory tree;
4. creates AI-friendly Markdown plus localized images and inline SVG assets for every page;
5. opens successful pages in a shared Playwright Chromium instance and captures the final live DOM;
6. prints each page to PDF from the same rendered browser context;
7. creates a site-wide machine-readable index and AI corpus;
8. writes failure manifests instead of silently dropping failed pages;
9. packages the complete site into a Windows-compatible ZIP.

### Full-site output structure

```text
site/
├── summary.json
├── discovery.json
├── urls.txt
├── tree.json
├── pages.json
├── failures.json
├── catalog.md
├── site-content.jsonl
├── all-content.md
└── pages/
    └── constitution_principle/
        └── instruction/
            └── concepts/
                ├── content.md
                ├── crawl4ai-raw.md
                ├── article.html
                ├── rendered.html
                ├── cleaned.html
                ├── browser-rendered.html
                ├── browser-rendered.pdf
                ├── metadata.json
                ├── links.json
                ├── media.json
                ├── formulas.json
                ├── assets.json
                └── assets/
                    ├── images/
                    └── svg/
```

`tree.json` preserves the URL hierarchy, `catalog.md` is a human-readable table of contents, `site-content.jsonl` is convenient for RAG/import pipelines, and `all-content.md` concatenates all successful page Markdown with source URLs.

The workflow `.github/workflows/csgraduates-site-archive.yml` runs a small 3-page smoke test on feature-branch pushes. Manual `workflow_dispatch` defaults to `max_pages=0`, meaning all discovered pages.

## Latest single-page result

The current Action has been tested against the target page with Crawl4AI 0.9.3 and Chromium:

- HTTP status: `200`
- article root: `.td-content`
- high-fidelity Markdown: about 9.1k characters
- Crawl4AI direct Markdown: about 10.1k characters
- content SVG diagrams exported: `5`
- decorative SVG icons skipped: `3`
- normal images downloaded: `1`
- image download failures: `0`
- rendered formulas detected: `1`
- exact TeX recovered from source: `0`
- live-browser KaTeX nodes captured: `1`
- live-browser formula text: `EA=(EBX)+(ECX)×4+8`
- browser-rendered PDF: generated successfully, about 1.30 MB

### Formula behavior on this page

The original TeX expression cannot be recovered exactly from the served source because the page no longer exposes authoritative TeX/MathML source data. However, the browser-rendered formula **can** be preserved correctly.

The live Chromium capture contains the complete KaTeX DOM and the browser-printed PDF renders the formula correctly as:

`EA = (EBX) + (ECX) × 4 + 8`

So the limitation is specifically **exact LaTeX source recovery**, not formula preservation.

## Local usage

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m unittest discover -s tests -v

# Single page
python scripts/crawl_test.py \
  --url "https://csgraduates.com/constitution_principle/instruction/concepts/" \
  --article-selector ".td-content"

# Full site; max-pages=0 means all discovered documents
python scripts/crawl_site.py \
  --base-url "https://csgraduates.com/" \
  --output-dir artifacts/site \
  --article-selector ".td-content" \
  --max-pages 0 \
  --crawl-concurrency 3 \
  --pdf-concurrency 2
```
