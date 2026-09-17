import base64
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from shard_plan import build_shards


class ShardPlanTests(unittest.TestCase):
    def test_groups_by_course_and_outer_directory_without_losing_urls(self):
        urls = [
            "https://csgraduates.com/",
            "https://csgraduates.com/data_structure/",
            "https://csgraduates.com/data_structure/linearlist/",
            "https://csgraduates.com/data_structure/linearlist/linked/",
            "https://csgraduates.com/data_structure/tree/",
            "https://csgraduates.com/constitution_principle/instruction/",
            "https://csgraduates.com/operating_system/process/",
            "https://csgraduates.com/computer_network/network_layer/",
            "https://csgraduates.com/study_methods/",
            "https://csgraduates.com/study_methods/math/math1/2021/",
            "https://csgraduates.com/study_methods/math/math1/2022/",
            "https://csgraduates.com/study_methods/math/math1/2023/",
            "https://csgraduates.com/study_methods/english/english1/2025/",
            "https://csgraduates.com/community/",
        ]
        shards = build_shards(urls, chunk_size=2)
        decoded = []
        labels = []
        for shard in shards:
            labels.append(shard["label"])
            decoded.extend(
                json.loads(base64.urlsafe_b64decode(shard["urls_b64"]).decode("utf-8"))
            )

        self.assertEqual(sorted(decoded), sorted(urls))
        self.assertEqual(len(decoded), len(set(decoded)))
        self.assertTrue(any(label.startswith("数据结构 / linearlist") for label in labels))
        self.assertTrue(any(label.startswith("计算机组成原理 / instruction") for label in labels))
        self.assertTrue(any(label.startswith("操作系统 / process") for label in labels))
        self.assertTrue(any(label.startswith("计算机网络 / network_layer") for label in labels))
        self.assertTrue(any(label.startswith("学习资源 / math") for label in labels))
        self.assertTrue(any(label == "其他站点内容" for label in labels))
        self.assertGreaterEqual(sum(1 for label in labels if label.startswith("学习资源 / math")), 2)


if __name__ == "__main__":
    unittest.main()
