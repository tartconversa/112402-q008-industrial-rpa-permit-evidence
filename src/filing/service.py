"""门面服务：把采集、规则、受理、门户、回执各模块组装为统一入口。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from .crawler import LocalSource, PolicyCrawler, Source
from .dashboard import Dashboard
from .intake import IntakeService
from .portal import (
    FakePortalDriver,
    PortalDriver,
    SubmissionService,
)
from .receipts import ReceiptService
from .rules import RuleEngine
from .storage import Storage


class FilingService:
    """应用服务根对象，依赖通过构造注入，便于替换为真实 HTTP/浏览器驱动。"""

    def __init__(
        self,
        storage: Storage,
        source: Optional[Source] = None,
        driver: Optional[PortalDriver] = None,
        today: Optional[date] = None,
        portal_url: str = "https://portal.example.gov/filing",
    ) -> None:
        self.storage = storage
        self.crawler = PolicyCrawler(source or LocalSource(Path.cwd()))
        self.engine = RuleEngine(storage, today=today)
        self.intake = IntakeService(storage)
        self.submissions = SubmissionService(
            storage, driver or FakePortalDriver(), portal_url=portal_url
        )
        self.receipts = ReceiptService(storage, self.submissions)
        self.dashboard = Dashboard(storage)

    # ------------------------------------------------------------- 政策采集

    def sync_policy(self, url: str) -> dict:
        """采集政策页面，落库条款与附件版本，返回变更摘要。"""
        snapshot = self.crawler.crawl(url)
        rule_changes, new_attachments = 0, 0
        if url.lower().endswith(".csv"):
            rules = self.crawler.parse_rules_csv(snapshot.body_text, snapshot)
        else:
            rules = self.crawler.parse_rules_csv(
                _extract_embedded_csv(snapshot.body_text), snapshot
            )
        for rule in rules:
            if self.storage.upsert_policy_rule(rule):
                rule_changes += 1
        for att in self.crawler.build_attachment_versions(snapshot):
            if self.storage.add_attachment(att):
                new_attachments += 1
        self.storage.commit()
        return {
            "snapshot_id": snapshot.snapshot_id,
            "rules_seen": len(rules),
            "rule_rows_changed": rule_changes,
            "new_attachment_versions": new_attachments,
        }

    def commit(self) -> None:
        self.storage.commit()

    def close(self) -> None:
        self.storage.close()


def _extract_embedded_csv(body_text: str) -> str:
    """HTML 正文中可能内嵌 CSV 形式的条款索引（演示页面使用）。

    以表头行为起点，只保留列数与表头一致的连续数据行，
    遇到不符合 CSV 行形态的正文即停止。
    """
    lines = body_text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if line.startswith("rule,category,location")),
        None,
    )
    if start is None:
        raise ValueError("页面中未找到条款索引（rule,category,location,...）")
    width = len(lines[start].split(","))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip() or len(line.split(",")) != width:
            break
        block.append(line)
    return "\n".join(block)
