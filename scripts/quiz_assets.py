"""XML-safe diagrams and self-contained browser-rendered formula SVGs."""

from __future__ import annotations

import base64
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from bs4 import BeautifulSoup

try:
    from .ai_archive_core import restore_svg_case
except ImportError:
    from ai_archive_core import restore_svg_case


def serialize_svg(source: str) -> str:
    soup = BeautifulSoup(source, "html.parser")
    root = soup.find("svg")
    if root is None or "<!DOCTYPE" in source.upper():
        raise ValueError("invalid SVG document")
    for node in root.select("script"):
        node.decompose()
    for node in [root, *root.find_all(True)]:
        for attr in list(node.attrs):
            if (not re.fullmatch(r"[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?", attr)
                    or attr.lower().startswith("on")):
                del node.attrs[attr]
    restore_svg_case(root)
    result = str(root) + "\n"
    ET.fromstring(result)
    return result


async def embedded_katex_css(page) -> str:
    """Localize KaTeX fonts so SVG-as-img needs no CDN or site stylesheet."""
    urls = await page.evaluate("[...document.styleSheets].map(s=>s.href).filter(u=>u && u.includes('katex'))")
    if not urls:
        raise ValueError("KaTeX stylesheet did not load")
    response = await page.request.get(urls[-1], timeout=45000)
    if not response.ok:
        raise ValueError("KaTeX stylesheet fetch failed")
    css = await response.text()
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css)
    css = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
    for face in faces:
        candidates = re.findall(r"url\(([^)]+)\)", face)
        font_url = next((u.strip('\"\'') for u in candidates if ".woff2" in u), None)
        if not font_url:
            raise ValueError("KaTeX font has no woff2 source")
        font = await page.request.get(urljoin(urls[-1], font_url), timeout=45000)
        if not font.ok:
            raise ValueError(f"KaTeX font fetch failed: {font_url}")
        encoded = base64.b64encode(await font.body()).decode("ascii")
        localized = re.sub(r"src:[^}]+", f'src:url(data:font/woff2;base64,{encoded}) format("woff2")', face)
        css += localized
    return css


FORMULA_JS = r"""(article) => {
  for (const el of article.querySelectorAll('.explanation,.solution-detail')) {
    el.style.setProperty('display','block','important');
    el.style.setProperty('visibility','visible','important');
  }
  const all = [], families = new Set();
  let question = null;
  for (const el of article.querySelectorAll('h5,.katex')) {
    if (el.matches('h5')) { if (/^\d+$/.test(el.textContent.trim())) question=Number(el.textContent.trim()); continue; }
    if (el.parentElement.closest('.katex')) continue;
    const style = getComputedStyle(el);
    const clone = el.cloneNode(true);
    clone.style.fontSize = style.fontSize;
    clone.style.color = '#000';
    // Measure the standalone layout, including ink extending outside its line
    // box (fractions, accents, and boxed math). The line box alone clips them.
    const holder=document.createElement('div');
    holder.style.cssText='position:absolute;left:-10000px;top:0;width:max-content;white-space:nowrap;font-size:16px;line-height:normal;font-family:serif';
    holder.append(clone);document.body.append(holder);
    const origin=holder.getBoundingClientRect();
    const boxes=[clone,...clone.querySelectorAll('.mord,.mop,.mbin,.mrel,.mopen,.mclose,.mpunct,.mfrac,.sqrt,.frac-line,.fbox,.accent-body,.stretchy,.enclosing')]
      .map(n=>n.getBoundingClientRect()).filter(r=>r.width>0 && r.height>0);
    const left=Math.min(...boxes.map(r=>r.left))-origin.left;
    const top=Math.min(...boxes.map(r=>r.top))-origin.top;
    const right=Math.max(...boxes.map(r=>r.right))-origin.left;
    const bottom=Math.max(...boxes.map(r=>r.bottom))-origin.top;
    if (!(right>left && bottom>top))throw new Error('Unrendered formula at question '+question);
    holder.remove();
    const localFamilies=new Set();
    for (const child of [el,...el.querySelectorAll('*')]) {
      for (const family of getComputedStyle(child).fontFamily.split(',')) {
        const name=family.trim().replaceAll('"','').replaceAll("'",'');
        if(name.startsWith('KaTeX_')) {families.add(name);localFamilies.add(name);}
      }
    }
    const id='C4AFORMULA'+String(all.length+1).padStart(4,'0');
    el.setAttribute('data-archive-formula',id);
    all.push({id,question,width:Math.ceil(right-left),height:Math.ceil(bottom-top),
      offsetX:4-left,offsetY:4-top,
      display:!!el.closest('.katex-display'),xml:new XMLSerializer().serializeToString(clone),
      fontSize:style.fontSize,families:[...localFamilies]});
  }
  return {formulas:all, families:[...families]};
}"""


def formula_svg(formula: dict, css: str) -> str:
    # Only embed the font families used on this page, not every KaTeX font.
    families = set(re.findall(r"font-family:([\w_]+)", formula["xml"]))
    # Family selection from the full formula DOM is supplied by the browser.
    families.update(formula.get("families", []))
    css = re.sub(r"@font-face\s*\{[^}]*\}", lambda m: m.group(0) if any(
        f"font-family:{family};" in m.group(0) for family in families) else "", css)
    width, height = formula["width"] + 8, formula["height"] + 8
    clone = formula["xml"]
    result = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
              f'viewBox="0 0 {width} {height}"><title>公式排版</title>'
              f'<foreignObject x="0" y="0" width="{width}" height="{height}">'
              f'<div xmlns="http://www.w3.org/1999/xhtml" style="white-space:nowrap;'
              f'font-size:16px;font-family:serif;line-height:normal;color:black;'
              f'transform:translate({formula.get("offsetX",4)}px,{formula.get("offsetY",4)}px)">'
              f'<style>{html.escape(css)}</style>{clone}</div>'
              f'</foreignObject></svg>\n')
    ET.fromstring(result)
    return result


FIGURES_JS = r"""(article) => {
  const figures=[]; let question=null;
  for(const el of article.querySelectorAll('h5,svg')) {
    if(el.matches('h5')) {if(/^\d+$/.test(el.textContent.trim()))question=Number(el.textContent.trim());continue;}
    if(el.parentElement.closest('svg,.katex') || el.getAttribute('aria-hidden')==='true' ||
       ['none','presentation'].includes(el.getAttribute('role')) || el.classList.contains('kp-icon'))continue;
    if(question===null)throw new Error('Unassigned diagram');
    const id='C4AFIGURE'+String(figures.length+1).padStart(3,'0');
    el.setAttribute('data-archive-figure',id);
    figures.push({id,question,xml:new XMLSerializer().serializeToString(el),
      alt:el.querySelector('title')?.textContent || `第 ${question} 题图示`});
  }
  return figures;
}"""
