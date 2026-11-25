"""端到端业务流程演示（内存库 + 本地政策站点 + 模拟门户，可离线运行）。

运行：PYTHONPATH=src python3 scripts/demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _helpers import build_local_site  # noqa: E402
from filing.crawler import LocalSource  # noqa: E402
from filing.models import (  # noqa: E402
    PendingType, PolicyRule, ReceiptKind,
)
from filing.portal import FakePortalDriver  # noqa: E402
from filing.service import FilingService  # noqa: E402
from filing.storage import Storage  # noqa: E402


def line(title: str) -> None:
    print("\n" + "=" * 24 + f" {title} " + "=" * 24)


def show(svc: FilingService, app_id: str) -> None:
    view = svc.dashboard.view(app_id).to_dict()
    print(json.dumps(view, ensure_ascii=False, indent=2))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        site_root = tmp_path / "site"
        build_local_site(site_root)
        driver = FakePortalDriver(timeout_next=0)
        svc = FilingService(
            Storage(":memory:"), source=LocalSource(site_root),
            driver=driver, today=date(2026, 11, 25),
        )

        # 1. 采集政策页面与附件 ------------------------------------------------
        line("1. 采集政策页面")
        print(svc.sync_policy("file:///policy.csv"))

        # 2. 登记企业申请并受理材料 -------------------------------------------
        line("2. 登记申请 A-8 并受理材料")
        svc.engine.ingest_application(
            "A-8", company="C-9", category="energy-saving",
            location="Zone-A", investment=900000,
        )
        svc.intake.intake_material("A-8", "budget.pdf", "budget_proof", b"budget v1")
        svc.intake.intake_material("A-8", "scope.pdf", "scope_statement", b"scope v1")
        result = svc.engine.evaluate("A-8")
        print("适用条款:", f"{result.chosen_rule.rule}v{result.chosen_rule.revision}",
              "阻断:", result.blocking)

        # 3. 条款冲突：同日发布两个修订，不自动选最宽松 -------------------------
        line("3. 同日多修订冲突 → 待政策专员确认")
        svc.storage.upsert_policy_rule(PolicyRule(
            rule="P1", revision=3, category="energy-saving", location="Zone-A",
            min_investment=950000, valid_from=date(2026, 10, 1), valid_to=None,
            state="active", source_url="file:///policy.csv",
            snapshot_id="s3", fetched_at="2026-11-25T00:00:00",
        ))
        svc.engine.evaluate("A-8")
        conflict = next(
            p for p in svc.storage.list_pending_items("A-8", open_only=True)
            if p.type == PendingType.RULE_CONFLICT
        )
        print("待确认:", conflict.question)
        # 由政策专员人工选定适用版本（示例中选定 v2，系统不代替人做选择）。
        svc.engine.confirm_rule_choice(conflict.item_id, "P1", 2, "政策专员李")
        print("确认后环节:", svc.storage.get_application("A-8").state.value)

        # 4. 门户提交（成功路径） ---------------------------------------------
        line("4. 门户填报提交")
        sub = svc.submissions.submit("A-8")
        print("提交状态:", sub.state.value, "门户编号:", sub.portal_ref)
        svc.submissions.submit("A-8")  # 重复调用不会产生第二张单
        print("门户侧实际建单数:", len(driver.portal_side))

        # 5. 部分受理回执 → 可办理事项 → 补件 → 只重传受影响附件 ---------------
        line("5. 部分受理与补正闭环")
        svc.receipts.ingest(
            "A-8", sub.portal_ref, ReceiptKind.PARTIAL,
            "预算证明予以接受；改造范围说明请补充后重新提交。",
            accepted_files=["budget.pdf"], rejected_files=["scope.pdf"],
        )
        print("可办理事项:", [
            (a.condition_ref, a.material_type)
            for a in svc.storage.list_action_items("A-8")
        ])
        svc.receipts.supply_material(
            "A-8", "scope.pdf", "scope_statement", b"scope v2 corrected",
        )
        action = svc.storage.list_action_items("A-8")[0]
        svc.receipts.review_supplied_material(
            action.action_id, sufficient=True, actor="材料审核员王",
        )
        resub = svc.receipts.resubmit_affected("A-8")
        print("补正重传附件:", resub.files, "状态:", resub.state.value)

        # 6. 超时场景：先核对门户再决定，避免重复申请 --------------------------
        line("6. 超时后核对门户实际状态")
        timeout_driver = FakePortalDriver(timeout_next=1)
        svc.submissions.driver = timeout_driver
        svc.engine.ingest_application(
            "A-9", company="C-8", category="automation", location="Zone-B",
            investment=450000,
        )
        for filename, mtype in [
            ("budget.pdf", "budget_proof"),
            ("scope.pdf", "scope_statement"),
            ("site.jpg", "site_photo"),
        ]:
            svc.intake.intake_material("A-9", filename, mtype, f"{filename}-1".encode())
        svc.engine.evaluate("A-9")
        timeout_sub = svc.submissions.submit("A-9")
        print("超时提交结论:", timeout_sub.state.value, "|", timeout_sub.detail)
        print("门户建单数（应只有 1）:", len(timeout_driver.portal_side))

        # 7. 受理锁定 + 指南更新影响判断 --------------------------------------
        line("7. 受理后政策变更")
        final_ref = resub.portal_ref
        svc.receipts.ingest(
            "A-8", final_ref, ReceiptKind.ACCEPTED, "补正材料齐全，予以受理。",
            accepted_files=resub.files,
        )
        svc.storage.upsert_policy_rule(PolicyRule(
            rule="P1", revision=4, category="energy-saving", location="Zone-A",
            min_investment=1200000, valid_from=date(2026, 12, 1), valid_to=None,
            state="active", source_url="file:///policy.csv",
            snapshot_id="s4", fetched_at="2026-11-30T00:00:00",
        ))
        svc.engine.evaluate("A-8")
        change = next(
            p for p in svc.storage.list_pending_items("A-8", open_only=True)
            if p.type == PendingType.POLICY_CHANGE
        )
        print("政策变更待判断:", change.question)
        svc.engine.resolve_policy_change("A-8", keep_locked=True, actor="政策专员李")
        locked = svc.storage.get_application("A-8")
        print("锁定版本保持:", f"{locked.locked_rule}v{locked.locked_policy_revision}",
              "环节:", locked.state.value)

        # 8. 服务中心总览 -----------------------------------------------------
        line("8. 办理总览")
        print("环节统计:", svc.dashboard.stage_summary())
        show(svc, "A-8")
        svc.close()


if __name__ == "__main__":
    main()
