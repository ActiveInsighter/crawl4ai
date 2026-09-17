from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_navigation import count_entries, parse_sidebar_document  # noqa: E402


class NavigationTests(unittest.TestCase):
    def test_nested_docsy_sidebar_is_preserved(self) -> None:
        html = '''
        <nav class="td-sidebar-nav">
          <ul>
            <li>
              <a class="td-sidebar-link td-sidebar-link__section" href="/computer_network/">
                <span>计算机网络</span>
              </a>
              <ul>
                <li>
                  <label><a class="td-sidebar-link td-sidebar-link__section" href="/computer_network/network/">网络层</a></label>
                  <ul>
                    <li><label><a class="td-sidebar-link td-sidebar-link__page" href="/computer_network/network/ip/">IP</a></label></li>
                  </ul>
                </li>
              </ul>
            </li>
          </ul>
        </nav>
        '''
        pages = {
            "https://csgraduates.com/computer_network/": "pages/computer_network",
            "https://csgraduates.com/computer_network/network/": "pages/computer_network/network",
            "https://csgraduates.com/computer_network/network/ip/": "pages/computer_network/network/ip",
        }
        items = parse_sidebar_document(
            html,
            base_url="https://csgraduates.com/",
            archived_pages=pages,
        )
        self.assertEqual(count_entries(items), 3)
        self.assertEqual(items[0]["title"], "计算机网络")
        self.assertEqual(items[0]["children"][0]["title"], "网络层")
        self.assertEqual(items[0]["children"][0]["children"][0]["title"], "IP")
        self.assertEqual(
            items[0]["children"][0]["children"][0]["pdf"],
            "pages/computer_network/network/ip/browser-rendered.pdf",
        )


if __name__ == "__main__":
    unittest.main()
