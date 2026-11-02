"""Extract validated annual 408 MDX into four subject-based long-answer collections."""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup


SUBJECTS = {
    '数据结构': ('01-数据结构', 'DS'),
    '计算机组成原理': ('02-计算机组成原理', 'CO'),
    '操作系统': ('03-操作系统', 'OS'),
    '计算机网络': ('04-计算机网络', 'CN'),
}
ALIASES = {'组成原理': '计算机组成原理'}
IMG = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")')


def prose_headings(mdx):
    """Return genuine heading offsets, ignoring lookalikes inside fenced code."""
    offset, fence = 0, None
    for line in mdx.splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            run, rest = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = None
        elif fence is None and line.startswith('#'):
            yield offset, line.strip()
        offset += len(line)


def subject_questions(source, mdx):
    soup = BeautifulSoup(source, 'html.parser')
    subjects, current = {}, None
    for heading in soup.select('.td-content h4, .td-content h5'):
        title = heading.get_text(' ', strip=True)
        if heading.name == 'h4':
            current = ALIASES.get(title, title)
        elif title.isdigit() and 41 <= int(title) <= 47:
            number = int(title)
            if number in subjects or current not in SUBJECTS:
                raise ValueError(f'Invalid subject classification for question {number}: {current}')
            subjects[number] = current
    if set(subjects) != set(range(41, 48)):
        raise ValueError('Source must contain all seven classified long-answer questions')

    headings = list(prose_headings(mdx))
    questions = [(offset, int(match.group(1))) for offset, title in headings
                 if (match := re.fullmatch(r'## 第 (\d+) 题', title))]
    if [number for _, number in questions] != list(range(1, 48)):
        raise ValueError('MDX must contain the complete ordered paper, questions 1–47')
    result = []
    for index, (start, number) in enumerate(questions):
        if number < 41:
            continue
        end = questions[index + 1][0] if index + 1 < len(questions) else len(mdx)
        # Subject labels precede the NEXT question and must not leak into this answer.
        next_subject = [offset for offset, title in headings if start < offset < end
                        and title.startswith('#### ')
                        and ALIASES.get(title[5:], title[5:]) in SUBJECTS]
        if next_subject:
            end = next_subject[0]
        body = mdx[start:end].strip() + '\n'
        result.append((subjects[number], number, body))
    return result


def local_asset(root, src):
    relative = Path(html.unescape(src))
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'assets':
        raise ValueError(f'Image is not a local archive asset: {src}')
    path = (root / relative).resolve()
    if not path.is_relative_to((root / 'assets').resolve()) or not path.is_file():
        raise ValueError(f'Missing or escaping image: {src}')
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def organize(source_root, output, archive):
    if __package__:
        from .export_408_mdx import validate_package
    else:
        from export_408_mdx import validate_package

    source_root, output, archive = source_root.resolve(), output.resolve(), archive.resolve()
    if output.exists():
        raise ValueError('Use a new output directory to avoid including stale content')
    if output.is_relative_to(source_root) or source_root.is_relative_to(output):
        raise ValueError('Source and output directories must be separate')
    if archive.is_relative_to(source_root) or archive.is_relative_to(output):
        raise ValueError('Archive must be outside source and output directories')
    if archive.suffix != '.zip' or archive.exists():
        raise ValueError('Archive must be a new .zip file')
    source_manifest = json.loads((source_root / 'manifest.json').read_text())
    years = source_manifest['years']
    if years != list(range(2009, max(years) + 1)) or max(years) < 2026:
        raise ValueError('A continuous 2009–2026 or newer archive is required')
    validate_package(source_root, years)
    output.mkdir(parents=True)
    counters, entries = Counter(), []
    for year in years:
        source = (source_root / 'sources' / f'{year}.html').read_text()
        mdx = (source_root / f'{year}.mdx').read_text()
        sections = subject_questions(source, mdx)
        expected = {'数据结构': 2, '计算机组成原理': 2, '操作系统': 2, '计算机网络': 1}
        if Counter(subject for subject, _, _ in sections) != expected:
            raise ValueError(f'{year}: unexpected subject coverage')
        for subject, number, body in sections:
            folder, code = SUBJECTS[subject]
            counters[subject] += 1
            question_id = f'{code}-{counters[subject]:03d}'
            stem = f'{counters[subject]:03d}-{year}-{number}'
            course_root = output / folder
            (course_root / 'assets').mkdir(parents=True, exist_ok=True)
            images, assets, kinds = {}, [], Counter()

            def replace_image(match):
                original = local_asset(source_root, match.group(2))
                if original not in images:
                    parent = original.parent.name
                    kind = {'figures': 'figure', 'formulas': 'formula', 'images': 'image'}.get(parent)
                    if kind is None:
                        raise ValueError(f'Unknown asset kind: {original}')
                    kinds[kind] += 1
                    name = f'{year}-{number}-{kind}-{kinds[kind]:03d}{original.suffix}'
                    target = course_root / 'assets' / name
                    shutil.copy2(original, target)
                    if target.suffix == '.svg':
                        ET.parse(target)
                    images[original] = f'./assets/{name}'
                    assets.append({'path': target.relative_to(output).as_posix(), 'kind': kind,
                                   'source_path': original.relative_to(source_root).as_posix(),
                                   'bytes': target.stat().st_size, 'sha256': digest(target)})
                return match.group(1) + images[original] + match.group(3)

            converted = IMG.sub(replace_image, body)
            title = f'{question_id} · {year} 年第 {number} 题'
            frontmatter = (f'---\ntitle: "{title}"\nsubject: "{subject}"\nyear: {year}\n'
                           f'question: {number}\nsequence: {counters[subject]}\n'
                           f'questionId: "{question_id}"\n'
                           f'source: "https://csgraduates.com/study_methods/408quiz/{year}/#{number}"\n---\n\n')
            page = course_root / f'{stem}.mdx'
            page.write_text(frontmatter + f'# {title}\n\n科目：{subject}；原卷题号：{number}。\n\n' + converted,
                            encoding='utf-8')
            for match in IMG.finditer(converted):
                local_asset(course_root, match.group(2))
            entries.append({'id': question_id, 'subject': subject, 'year': year, 'question': number,
                            'mdx': page.relative_to(output).as_posix(), 'mdx_sha256': digest(page),
                            'source_body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                            'image_references': len(IMG.findall(body)), 'assets': assets})
    index = '---\ntitle: "408 历年大题（按科目）"\n---\n\n# 408 历年大题\n\n'
    for subject, (folder, code) in SUBJECTS.items():
        pages = [entry for entry in entries if entry['subject'] == subject]
        if len(pages) != len(years) * (1 if code == 'CN' else 2):
            raise ValueError(f'Unexpected annual subject coverage: {subject}')
        index += f'- [{subject}（{len(pages)} 题）](./{folder}/index.mdx)\n'
        course_index = f'---\ntitle: "{subject}"\n---\n\n# {subject}\n\n'
        course_index += '\n'.join(f'- [{e["id"]} · {e["year"]} 年第 {e["question"]} 题](./{Path(e["mdx"]).name})'
                                 for e in pages) + '\n'
        (output / folder / 'index.mdx').write_text(course_index, encoding='utf-8')
        write_json(output / folder / 'meta.json', {'title': subject, 'pages': ['index', *[Path(e['mdx']).stem for e in pages]]})
    (output / 'index.mdx').write_text(index, encoding='utf-8')
    write_json(output / 'meta.json', {'title': '408 历年大题', 'pages': ['index', *[v[0] for v in SUBJECTS.values()]]})
    asset_counts = Counter(a['kind'] for e in entries for a in e['assets'])
    manifest = {'years': years, 'questions': len(entries), 'subjects': dict(counters),
                'assets': dict(asset_counts), 'classification': 'Original source h4 subject headings',
                'source_manifest_sha256': digest(source_root / 'manifest.json'), 'pages': entries}
    write_json(output / 'manifest.json', manifest)
    shutil.copy2(source_root / 'KaTeX-LICENSE.txt', output / 'KaTeX-LICENSE.txt')
    (output / 'README.md').write_text(
        f'# 408 历年大题 · 四门课\n\n{years[0]}—{years[-1]} 年，共 {len(entries)} 道大题，保留题干、解析、代码、表格及相关图片。\n\n'
        '四个科目目录：数据结构、计算机组成原理、操作系统、计算机网络。每题一份 MDX，目录中有 index.mdx 和 meta.json。\n\n'
        '文件名规则：`科目内三位序号-年份-原卷题号.mdx`，例如 `001-2009-41.mdx`。\n'
        '科目编号：DS 数据结构、CO 计算机组成原理、OS 操作系统、CN 计算机网络；各科目分别从 001 连续编号，按年份和原题号升序。\n'
        '图片名规则：`年份-原题号-figure/formula/image-三位序号.扩展名`，分别代表题图、公式、原站位图。\n'
        '2014、2016、2019 年科目顺序不同，按原站科目标题分类，原卷题号保持不变。\n\n'
        '图片使用 `<img src="./assets/...svg" alt="..." />`，请保留每个科目的 assets 目录。\n'
        '部分公式 SVG 使用 foreignObject 并内嵌字体，需现代浏览器支持。MDX 编译需 remark-gfm 和 remark-frontmatter。\n'
        '若网站框架不处理相对图片路径，请将对应 assets 复制到 public，并设置所需 src 前缀。\n\n'
        'manifest.json 记录逐题科目、原题号、编号、图像来源和 SHA-256。mdx-validation.json 记录编译结果。\n'
        '来源：https://csgraduates.com/study_methods/408quiz/ 。内容按原站保留，未与官方原卷逐题校对。\n', encoding='utf-8')
    subprocess.run(['node', str(Path(__file__).with_name('validate_408_mdx.mjs')), str(output)], check=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                bundle.write(path, f'{output.name}/{path.relative_to(output).as_posix()}')
    temporary.replace(archive)
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size,
                      'questions': len(entries), 'subjects': dict(counters), 'assets': dict(asset_counts)}, ensure_ascii=False))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--zip', type=Path, required=True)
    args = parser.parse_args()
    organize(args.source, args.output, args.zip)


if __name__ == '__main__':
    main()
