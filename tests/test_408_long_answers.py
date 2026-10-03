import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.organize_408_long_answers import local_asset, organize, subject_questions


def annual_fixture():
    # 2016: networks appear first, unlike the usual question-number mapping.
    subjects = {41: '计算机网络', 42: '数据结构', 44: '组成原理', 46: '操作系统'}
    html = ['<div class="td-content">']
    mdx = []
    for q in range(1, 48):
        if q in subjects:
            html.append(f'<h4>{subjects[q]}</h4>')
            mdx.append(f'#### {subjects[q]}\n\n')
        html.append(f'<h5>{q}</h5><p>题干 {q}</p>')
        mdx.append(f'## 第 {q} 题\n\n题干 {q}\n\n')
        if q == 42:
            mdx.append('```c\n## 第 43 题\n#### 操作系统\n```\n\n')
        mdx.append(f'解析 {q}\n\n')
    return ''.join(html) + '</div>', ''.join(mdx)


class LongAnswerTests(unittest.TestCase):
    def test_uses_actual_subject_headers_and_preserves_original_numbers(self):
        sections = subject_questions(*annual_fixture())
        self.assertEqual([(s, q) for s, q, _ in sections], [
            ('计算机网络', 41), ('数据结构', 42), ('数据结构', 43),
            ('计算机组成原理', 44), ('计算机组成原理', 45),
            ('操作系统', 46), ('操作系统', 47),
        ])

    def test_keeps_code_and_solutions_without_next_subject_header(self):
        sections = subject_questions(*annual_fixture())
        q42, q43 = sections[1][2], sections[2][2]
        self.assertIn('```c\n## 第 43 题\n#### 操作系统\n```', q42)
        self.assertIn('解析 42', q42)
        self.assertIn('解析 43', q43)
        self.assertNotIn('#### 组成原理', q43)
        self.assertNotIn('题干 40', ''.join(body for _, _, body in sections))

    def test_rejects_missing_question_and_unclassified_subject(self):
        source, mdx = annual_fixture()
        for html, text in ((source, mdx.replace('## 第 47 题', '第 47 题')),
                           (source.replace('<h4>计算机网络</h4>', '<h4>未知</h4>'), mdx)):
            with self.assertRaises(ValueError):
                subject_questions(html, text)

    def test_local_assets_reject_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'archive'
            (root / 'assets').mkdir(parents=True)
            outside = Path(tmp) / 'outside.svg'
            outside.write_text('<svg/>')
            (root / 'assets' / 'link.svg').symlink_to(outside)
            (root / 'assets' / 'ok.svg').write_text('<svg/>')
            self.assertEqual(local_asset(root, './assets/ok.svg'), root / 'assets/ok.svg')
            for src in ('../outside.svg', './assets/../../outside.svg', './assets/link.svg', 'https://example.com/x.svg'):
                with self.assertRaises(ValueError):
                    local_asset(root, src)

    def test_rejects_archive_inside_source_or_output_before_reading_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source', Path(tmp) / 'output'
            source.mkdir()
            for archive in (source / 'bundle.zip', output / 'bundle.zip'):
                with self.assertRaisesRegex(ValueError, 'Archive'):
                    organize(source, output, archive)

    def test_direct_cli_rejects_archive_without_import_error(self):
        script = Path(__file__).parents[1] / 'scripts' / 'organize_408_long_answers.py'
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(script), '--source', tmp + '/source',
                                     '--output', tmp + '/output', '--zip', tmp + '/output/bundle.zip'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Archive', result.stderr)
            self.assertNotIn('ModuleNotFoundError', result.stderr)
