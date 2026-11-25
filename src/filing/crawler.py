"""政策页面与附件采集。

设计要点：
- 源可切换：生产用 ``HttpSource``（urllib），离线测试/演示用 ``LocalSource``。
- 页面正文与每个附件都按 SHA-256 内容哈希识别版本，内容不变不产生新版本。
- 附件可能相对路径或绝对 URL，统一由源负责解析。
- 下载到的内容属于外部不可信数据，本模块只做字节存储与哈希，不执行其中代码。
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .models import AttachmentVersion, PolicyRule
from .storage import now_iso

USER_AGENT = "filing-assistant/1.0 (+service-center)"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclasses.dataclass
class PageSnapshot:
    """一次抓取得到的页面快照。"""

    url: str
    body_text: str
    snapshot_id: str
    fetched_at: str
    attachment_refs: list["AttachmentRef"]


@dataclasses.dataclass
class AttachmentRef:
    """页面中引用的附件及其抓取到的字节。"""

    url: str
    filename: str
    revision_hint: Optional[str]
    content: bytes

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.content)


class _LinkExtractor(HTMLParser):
    """从 HTML 中提取附件链接与纯文本正文。"""

    ATTACH_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|csv|txt)$", re.IGNORECASE)

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            text = dict(attrs).get("title", "")
            if href:
                self.links.append((href, text))

    def handle_data(self, data):
        text = data.strip()
        if text:
            self._chunks.append(text)

    @property
    def body_text(self) -> str:
        return "\n".join(self._chunks)

    def attachment_links(self) -> list[tuple[str, str]]:
        return [(h, t) for h, t in self.links if self.ATTACH_RE.search(h)]


class Source(Protocol):
    """页面/附件来源协议。"""

    def fetch_page(self, url: str) -> tuple[str, bytes]: ...

    def fetch_attachment(self, base_url: str, href: str) -> tuple[str, bytes]: ...


class HttpSource:
    """通过 HTTP/HTTPS 抓取（仅标准库）。"""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
            return resp.read()

    def fetch_page(self, url: str) -> tuple[str, bytes]:
        return url, self._get(url)

    def fetch_attachment(self, base_url: str, href: str) -> tuple[str, bytes]:
        full = urljoin(base_url, href)
        return full, self._get(full)


class LocalSource:
    """从本地目录读取，目录布局镜像网站路径，用于离线测试与演示。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path_for(self, url: str) -> Path:
        parsed = urlparse(url)
        rel = parsed.path.lstrip("/")
        if not rel:
            rel = "index.html"
        return self.root / rel

    def fetch_page(self, url: str) -> tuple[str, bytes]:
        path = self._path_for(url)
        return url, path.read_bytes()

    def fetch_attachment(self, base_url: str, href: str) -> tuple[str, bytes]:
        full = urljoin(base_url, href)
        return full, self._path_for(full).read_bytes()


class CrawlError(RuntimeError):
    pass


class PolicyCrawler:
    """采集政策页面正文、附件，并解析条款版本。"""

    def __init__(self, source: Source) -> None:
        self.source = source

    def crawl(self, url: str) -> PageSnapshot:
        final_url, raw = self.source.fetch_page(url)
        snapshot_id = sha256_hex(raw)[:16]
        fetched_at = now_iso()
        lower = url.lower()
        if lower.endswith(".csv"):
            body_text = raw.decode("utf-8-sig")
            refs: list[AttachmentRef] = []
        else:
            parser = _LinkExtractor()
            parser.feed(raw.decode("utf-8", errors="replace"))
            body_text = parser.body_text
            refs = []
            for href, link_text in parser.attachment_links():
                full_url, content = self.source.fetch_attachment(final_url, href)
                filename = Path(urlparse(full_url).path).name
                refs.append(AttachmentRef(
                    url=full_url, filename=filename,
                    revision_hint=link_text.strip() or None, content=content,
                ))
        return PageSnapshot(
            url=final_url, body_text=body_text, snapshot_id=snapshot_id,
            fetched_at=fetched_at, attachment_refs=refs,
        )

    # ---------------------------------------------------------- 条款解析

    @staticmethod
    def parse_rules_csv(body_text: str, snapshot: PageSnapshot) -> list[PolicyRule]:
        """解析条款索引 CSV。

        列：rule, category, location, min_investment, valid_from, revision, state
        与样例 ``policy.csv`` 保持一致，空单元格表示来源未提供值。
        """
        rules: list[PolicyRule] = []
        reader = csv.DictReader(io.StringIO(body_text))
        expected = {
            "rule", "category", "location", "min_investment",
            "valid_from", "revision", "state",
        }
        if reader.fieldnames is None or not expected.issubset(reader.fieldnames):
            raise CrawlError(f"政策索引缺少必要列: {reader.fieldnames}")
        for row in reader:
            rules.append(PolicyRule(
                rule=row["rule"].strip(),
                revision=int(row["revision"]),
                category=row["category"].strip(),
                location=row["location"].strip(),
                min_investment=int(row["min_investment"]) if row.get("min_investment") else None,
                valid_from=_parse_iso_date(row.get("valid_from")),
                valid_to=_parse_iso_date(row.get("valid_to")) if "valid_to" in row else None,
                state=row["state"].strip(),
                source_url=snapshot.url,
                snapshot_id=snapshot.snapshot_id,
                fetched_at=snapshot.fetched_at,
            ))
        if not rules:
            raise CrawlError("政策索引未解析出任何条款")
        return rules

    def build_attachment_versions(self, snapshot: PageSnapshot) -> list[AttachmentVersion]:
        versions = []
        for ref in snapshot.attachment_refs:
            versions.append(AttachmentVersion(
                attachment_id=ref.filename,
                url=ref.url, filename=ref.filename,
                revision_hint=ref.revision_hint, content_hash=ref.content_hash,
                bytes_len=len(ref.content), snapshot_id=snapshot.snapshot_id,
                fetched_at=snapshot.fetched_at,
            ))
        return versions


def _parse_iso_date(value: Optional[str]):
    from datetime import date

    value = (value or "").strip()
    return date.fromisoformat(value) if value else None
