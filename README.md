# crawl4ai test harness

A GitHub Actions test harness for evaluating [Crawl4AI](https://github.com/unclecode/crawl4ai) against documentation pages while preserving formulas, images and inline SVG diagrams for downstream AI/document workflows.

## Current target

`https://csgraduates.com/constitution_principle/instruction/concepts/`

The default article selector is `.td-content`; it can be overridden from `workflow_dispatch` or with `--article-selector` locally.

## High-fidelity export pipeline

The crawler keeps Crawl4AI as the browser/DOM acquisition layer, then post-processes the article DOM before converting it back to Markdown:

1. Fetch the HTTP source and also crawl the rendered page with Chromium.
2. Select the article root instead of converting the whole page shell.
3. Recover formula TeX when it is actually available from KaTeX/MathJax/MathML annotations, supported TeX attributes, or source delimiters such as `$$...$$`, `\(...\)` and `\[...\]`.
4. Replace recoverable formulas with safe tokens before Markdown conversion, then restore them as `$...$` or `$$...$$`.
5. Never invent TeX when the served page has already discarded the original expression; unrecovered formulas remain in the rendered article DOM, are converted by the normal Markdown path as best-effort visible text, and are recorded in `formulas.json`.
6. Export content SVG diagrams to standalone `.svg` files while removing decorative/`aria-hidden` SVG icons.
7. Download normal article images into `assets/images/` and rewrite their paths locally.
8. Normalize article links to absolute URLs.
9. Package the complete result as a Windows-compatible ZIP.

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
- exact TeX recovered: `0`
- post-processing unit tests: `5/5` passing

### Why the formula is not reconstructed as LaTeX

For this page, both the HTTP source and the Chromium-rendered DOM already contain KaTeX's visual HTML spans but no `application/x-tex` annotation, MathML source, TeX data attribute, or original `$$...$$` / `\(...\)` / `\[...\]` delimiter text. That means the original TeX expression is no longer present in the served page.

The crawler therefore intentionally does **not** guess a replacement LaTeX expression. It keeps the available rendered representation and records the recovery status. Exact TeX recovery for such pages requires access to the original Markdown/content source or another authoritative source containing the TeX.

## What the action exports

The workflow `.github/workflows/crawl4ai-test.yml` installs the pinned dependencies and Chromium, runs unit tests, crawls the page, then uploads `crawl4ai-csgraduates-result` containing:

- `content.md` — post-processed article Markdown with local asset paths and recoverable formulas restored
- `crawl4ai-raw.md` — Crawl4AI's direct Markdown conversion for comparison/debugging
- `source.html` — HTTP response before browser-side rendering
- `rendered.html` — Crawl4AI `result.html` after browser rendering
- `raw.html` — compatibility alias of `rendered.html`
- `cleaned.html` — Crawl4AI cleaned HTML
- `article.html` — post-processed article DOM used to generate `content.md`
- `source-formulas.json` — formula candidates found in the pre-render HTTP source
- `formulas.json` — recovered/unrecovered formula manifest and recovery source
- `assets.json` — downloaded image and exported SVG manifest, including failures and skipped decorative SVG count
- `assets/images/*` — locally downloaded image resources
- `assets/svg/*` — exported inline article SVG diagrams
- `metadata.json` — crawl status plus article/formula/SVG/image counts
- `links.json` — Crawl4AI discovered links
- `media.json` — Crawl4AI discovered media
- `crawl4ai-csgraduates-result.zip` — packaged bundle suitable for Windows extraction

## Local usage

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m unittest discover -s tests -v
python scripts/crawl_test.py \
  --url "https://csgraduates.com/constitution_principle/instruction/concepts/" \
  --article-selector ".td-content"
```
