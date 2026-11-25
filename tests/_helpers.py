"""测试共用夹具：构造内存服务、本地政策站点与样例材料。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from filing.crawler import LocalSource  # noqa: E402
from filing.portal import FakePortalDriver  # noqa: E402
from filing.service import FilingService  # noqa: E402
from filing.storage import Storage  # noqa: E402

POLICY_CSV = (
    "rule,category,location,min_investment,valid_from,revision,state\n"
    "P1,energy-saving,Zone-A,500000,2026-01-01,1,active\n"
    "P1,energy-saving,Zone-A,800000,2026-10-01,2,active\n"
    "P2,automation,Zone-B,300000,2026-03-01,1,active\n"
)

POLICY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>节能技改办事指南</title></head>
<body>
<h1>节能技改项目备案办事指南</h1>
<p>最新修订日期 2026-10-01</p>
<h2>条款索引</h2>
<pre>
rule,category,location,min_investment,valid_from,revision,state
P1,energy-saving,Zone-A,500000,2026-01-01,1,active
P1,energy-saving,Zone-A,800000,2026-10-01,2,active
</pre>
<h2>附件</h2>
<ul>
  <li><a href="/guides/p1-guide-v2.pdf">P1 申报指引第二版</a></li>
  <li><a href="/guides/budget-template.xlsx">预算证明模板</a></li>
</ul>
</body></html>
"""


def build_local_site(root: Path) -> str:
    """在临时目录中镜像一个静态政策站点，返回页面 URL。"""
    guides = root / "guides"
    guides.mkdir(parents=True)
    (root / "policy.csv").write_text(POLICY_CSV, encoding="utf-8")
    (root / "index.html").write_text(POLICY_HTML, encoding="utf-8")
    (guides / "p1-guide-v2.pdf").write_bytes(b"%PDF-1.4 guide v2 content")
    (guides / "budget-template.xlsx").write_bytes(b"PK fake xlsx bytes")
    return "file:///index.html"


def make_service(
    tmp_path: Path,
    today: date | None = None,
    driver: FakePortalDriver | None = None,
) -> FilingService:
    root = tmp_path / "site"
    url = build_local_site(root)
    storage = Storage(":memory:")
    service = FilingService(
        storage,
        source=LocalSource(root),
        driver=driver or FakePortalDriver(),
        today=today or date(2026, 11, 25),
    )
    service.sync_policy("file:///policy.csv")
    return service


def seed_application_with_materials(
    service: FilingService, app_id: str = "A-100", **app_kwargs
) -> str:
    kwargs = dict(
        company="C-100", category="energy-saving", location="Zone-A",
        investment=900000,
    )
    kwargs.update(app_kwargs)
    service.engine.ingest_application(app_id, **kwargs)
    service.intake.intake_material(
        app_id, "budget.pdf", "budget_proof", b"budget content v1"
    )
    service.intake.intake_material(
        app_id, "scope.pdf", "scope_statement", b"scope content v1"
    )
    service.commit()
    return app_id
