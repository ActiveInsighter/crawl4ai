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
2. records the rendered KaTeX text in `rendered-export.json`;
3. calls Chromium `page.pdf()` directly and saves `browser-rendered.pdf`.

There is no Markdown/HTML round trip in this PDF path. It mirrors the browser-print approach and preserves the already-rendered formula visually.

## Latest real-page result

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
- post-processing/render-export unit tests: `6/6` passing

### Formula behavior on this page

The original TeX expression cannot be recovered exactly from the served source because the page no longer exposes authoritative TeX/MathML source data. However, the browser-rendered formula **can** be preserved correctly.

The live Chromium capture contains the complete KaTeX DOM and the browser-printed PDF renders the formula correctly as:

`EA = (EBX) + (ECX) × 4 + 8`

So the limitation is specifically **exact LaTeX source recovery**, not formula preservation.

## What the action exports

The workflow `.github/workflows/crawl4ai-test.yml` uploads `crawl4ai-csgraduates-result` containing:

- `content.md` — AI-friendly article Markdown with localized assets
- `crawl4ai-raw.md` — Crawl4AI direct Markdown for comparison/debugging
- `source.html` — raw HTTP response
- `rendered.html` — Crawl4AI `result.html`
- `rendered-standalone.html` — replayable version of Crawl4AI rendered HTML with an explicit base URL
- `browser-rendered.html` — **live Chromium DOM captured directly from the original page after rendering**
- `browser-rendered.pdf` — **PDF printed directly from that same live Chromium page**
- `rendered-export.json` — live-browser status, KaTeX text/counts, DOM size and PDF metadata
- `raw.html` — compatibility alias of `rendered.html`
- `cleaned.html` — Crawl4AI cleaned HTML
- `article.html` — post-processed article DOM used to generate `content.md`
- `source-formulas.json` — formula candidates found in the HTTP source
- `formulas.json` — exact TeX recovery manifest
- `assets.json` — downloaded image/exported SVG manifest
- `assets/images/*` — localized image resources
- `assets/svg/*` — exported inline article SVG diagrams
- `metadata.json` — crawl status and extraction counts
- `links.json` — Crawl4AI discovered links
- `media.json` — Crawl4AI discovered media
- `crawl4ai-csgraduates-result.zip` — Windows-compatible bundle

## Local usage

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m unittest discover -s tests -v
python scripts/crawl_test.py \
  --url "https://csgraduates.com/constitution_principle/instruction/concepts/" \
  --article-selector ".td-content"

python scripts/export_rendered.py \
  --input artifacts/crawl/rendered.html \
  --base-url "https://csgraduates.com/constitution_principle/instruction/concepts/" \
  --output-replay-html artifacts/crawl/rendered-standalone.html \
  --output-live-html artifacts/crawl/browser-rendered.html \
  --output-pdf artifacts/crawl/browser-rendered.pdf \
  --metadata artifacts/crawl/rendered-export.json
```
