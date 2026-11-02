import base64
import json
import tempfile
import unittest
from pathlib import Path

import fitz

from scripts.render_408_pdf_shard import SUBJECTS, decode_urls, inspect_pdf, relative_pdf


class PdfOnlyTests(unittest.TestCase):
    def test_four_subject_paths(self):
        for subject in SUBJECTS:
            self.assertEqual(
                relative_pdf("https://csgraduates.com/" + subject + "/"),
                "pages/" + subject + "/_index.pdf",
            )
            self.assertEqual(
                relative_pdf("https://csgraduates.com/" + subject + "/search/tree/"),
                "pages/" + subject + "/search/tree.pdf",
            )

    def test_refuse_non_subject_and_traversal(self):
        for url in (
            "https://csgraduates.com/study_methods/408quiz/",
            "https://csgraduates.com/",
            "https://csgraduates.com/data_structure/%2e%2e/",
        ):
            with self.assertRaises(ValueError):
                relative_pdf(url)

    def test_only_allowed_source_in_urls(self):
        urls = ["https://csgraduates.com/data_structure/tree/btree/"]
        encoded = base64.urlsafe_b64encode(json.dumps(urls).encode()).decode()
        self.assertEqual(decode_urls(encoded), urls)
        bad = base64.urlsafe_b64encode(
            json.dumps(["https://evil.example/data_structure/"]).encode()
        ).decode()
        with self.assertRaises(ValueError):
            decode_urls(bad)

    def test_pdf_check_checks_page_and_real_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((60, 60), "CPU cache instruction memory", fontsize=12)
            doc.save(path)
            doc.close()
            info = inspect_pdf(path, 100)
            self.assertEqual(info["pdf_pages"], 1)
            self.assertEqual(len(info["sha256"]), 64)
            self.assertGreater(info["pdf_text_chars"], 10)


if __name__ == "__main__":
    unittest.main()
