# crawl4ai test harness

A GitHub Actions test harness for evaluating [Crawl4AI](https://github.com/unclecode/crawl4ai) against documentation pages while preserving formulas, images and inline SVG diagrams for downstream AI/document workflows.

## Current target

`https://csgraduates.com/constitution_principle/instruction/concepts/`

The default article selector is `.td-content`; it can be overridden from `workflow_dispatch` or with `--article-selector` locally.

## High-fidelity export pipeline

The crawler keeps Crawl4AI as the browser/DOM acquisition layer, then post-processes the article DOM before converting it back to Markdown:

1. Crawl the page with Chromium and keep Crawl4AI HTML/Markdown outputs.
2. Select the article root instead of converting the whole page shell.
3. Recover KaTeX/MathJax/MathML TeX when the rendered DOM contains an `application/x-tex` annotation or a supported TeX attribute.
4. Replace recoverable formulas with safe tokens before Markdown conversion, then restore them as `$...$` or `$$...$$`.
5. Export inline article SVGs to standalone `.svg` files and replace them with local Markdown image references.
6. Download normal article images into `assets/images/` and rewrite their paths locally.
7. Normalize article links to absolute URLs.
8. Package the complete result as a Windows-compatible ZIP.

## What the action exports

The workflow `.github/workflows/crawl4ai-test.yml` installs the pinned dependencies and Chromium, runs unit tests, crawls the page, then uploads `crawl4ai-csgraduates-result` containing:

- `content.md` — high-fidelity article Markdown with restored formulas and local asset paths
- `crawl4ai-raw.md` — Crawl4AI's direct Markdown conversion for comparison/debugging
- `raw.html` — Crawl4AI `result.html`
- `cleaned.html` — Crawl4AI cleaned HTML
- `article.html` — post-processed article DOM used to generate `content.md`
- `formulas.json` — recovered/unrecovered formula manifest
- `assets.json` — downloaded image and exported SVG manifest, including image failures
- `assets/images/*` — locally downloaded raster/vector image resources referenced by `<img>`
- `assets/svg/*` — exported inline article SVG diagrams
- `metadata.json` — crawl status plus article/formula/SVG/image counts
- `links.json` — Crawl4AI discovered links
- `media.json` — Crawl4AI discovered media
- `crawl4ai-csgraduates-result.zip` — packaged bundle suitable for Windows extraction

If a formula cannot be reconstructed as TeX, its original rendered DOM is left in place instead of silently inventing LaTeX; the failure is visible in `formulas.json`.

## Local usage

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m unittest discover -s tests -v
python scripts/crawl_test.py \
  --url "https://csgraduates.com/constitution_principle/instruction/concepts/" \
  --article-selector ".td-content"
```
