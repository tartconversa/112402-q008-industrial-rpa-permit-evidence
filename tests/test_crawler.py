"""政策页面采集与附件版本化测试。"""
import unittest
from pathlib import Path

from _helpers import POLICY_CSV, build_local_site

from filing.crawler import LocalSource, PolicyCrawler, sha256_hex


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "_tmp_crawler"
        self.root.mkdir(exist_ok=True)
        self.url = build_local_site(self.root)
        self.crawler = PolicyCrawler(LocalSource(self.root))

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        self.root.rmdir()

    def test_采集CSV页面并解析条款版本(self):
        snapshot = self.crawler.crawl("file:///policy.csv")
        rules = self.crawler.parse_rules_csv(snapshot.body_text, snapshot)
        self.assertEqual(len(rules), 3)
        p1_v2 = next(r for r in rules if r.rule == "P1" and r.revision == 2)
        self.assertEqual(p1_v2.min_investment, 800000)
        self.assertEqual(p1_v2.valid_from.isoformat(), "2026-10-01")
        # 快照标识基于正文内容哈希，重复采集结果稳定。
        self.assertEqual(snapshot.snapshot_id, sha256_hex(POLICY_CSV.encode())[:16])

    def test_HTML页面提取正文与附件链接(self):
        snapshot = self.crawler.crawl(self.url)
        self.assertIn("节能技改项目备案办事指南", snapshot.body_text)
        filenames = {ref.filename for ref in snapshot.attachment_refs}
        self.assertEqual(filenames, {"p1-guide-v2.pdf", "budget-template.xlsx"})

    def test_附件内容不变时不产生新版本(self):
        first = self.crawler.crawl(self.url)
        versions_a = self.crawler.build_attachment_versions(first)
        second = self.crawler.crawl(self.url)
        versions_b = self.crawler.build_attachment_versions(second)
        self.assertEqual(
            {v.content_hash for v in versions_a},
            {v.content_hash for v in versions_b},
        )

    def test_附件修改后内容哈希变化(self):
        guide = self.root / "guides" / "p1-guide-v2.pdf"
        before = sha256_hex(guide.read_bytes())
        guide.write_bytes(b"%PDF-1.4 guide v3 changed content")
        after_snapshot = self.crawler.crawl(self.url)
        ref = next(
            r for r in after_snapshot.attachment_refs
            if r.filename == "p1-guide-v2.pdf"
        )
        self.assertNotEqual(before, ref.content_hash)

    def test_HTML内嵌CSV条款索引可解析(self):
        snapshot = self.crawler.crawl(self.url)
        from filing.service import _extract_embedded_csv

        rules = self.crawler.parse_rules_csv(
            _extract_embedded_csv(snapshot.body_text), snapshot
        )
        self.assertEqual({(r.rule, r.revision) for r in rules},
                         {("P1", 1), ("P1", 2)})


if __name__ == "__main__":
    unittest.main()
