"""把仓库自带样例 CSV 导入系统并回放一次完整办理。

运行：PYTHONPATH=src python3 scripts/import_samples.py --db /tmp/filing-demo.db
不带 --db 时使用内存库，仅验证导入正确性。
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from filing.crawler import LocalSource  # noqa: E402
from filing.models import ReceiptKind  # noqa: E402
from filing.portal import FakePortalDriver  # noqa: E402
from filing.service import FilingService  # noqa: E402
from filing.storage import Storage  # noqa: E402

SAMPLES = ROOT / "samples"


def read_csv(name: str) -> list[dict]:
    with (SAMPLES / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, strict=True))


def _guess_material_type(filename: str) -> str:
    """样例文件名到材料类型的最小映射，复杂场景由回执服务的关键词解析处理。"""
    name = filename.lower()
    if "budget" in name:
        return "budget_proof"
    if "scope" in name:
        return "scope_statement"
    if "site" in name or "photo" in name:
        return "site_photo"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description="导入样例 CSV 并回放办理流程")
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--today", default="2026-11-25")
    args = parser.parse_args()

    # 本地源根目录即仓库根：policy.csv 位于 samples/ 下，用 file URL 指向。
    svc = FilingService(
        Storage(args.db),
        source=LocalSource(ROOT),
        driver=FakePortalDriver(),
        today=date.fromisoformat(args.today),
    )
    summary = svc.sync_policy(f"file:///{SAMPLES.name}/policy.csv")
    print("政策采集:", summary)

    for row in read_csv("applications"):
        svc.engine.ingest_application(
            row["app"], company=row["company"], category=row["category"],
            location=row["location"], investment=int(row["investment"]),
            rule=row["rule"] or None,
            policy_revision=int(row["policy_revision"])
            if row["policy_revision"] else None,
        )
        from filing.rules import REQUIRED_MATERIALS

        required = REQUIRED_MATERIALS.get(row["rule"], [])
        app_receipt_files = {
            _guess_material_type(fn): fn
            for receipt in read_csv("receipts") if receipt["app"] == row["app"]
            for fn in (receipt["files"].split("|") if receipt["files"] else [])
        }
        # 以条款要求的材料类型为准，文件名优先采用样例回执中的真实名称。
        for material_type in required:
            filename = app_receipt_files.get(
                material_type, material_type + ".pdf"
            )
            svc.intake.intake_material(
                row["app"], filename, material_type,
                f"{row['app']}-{material_type}".encode(),
            )
        svc.engine.evaluate(row["app"])

    # 回放门户提交与样例回执（回执编号与 submissions 对齐）。
    for row in read_csv("receipts"):
        app_id = row["app"]
        app = svc.storage.get_application(app_id)
        # 样例中的申请都已进入门户流程：挂起项按样例语义由专员确认后放行，
        # 确认过程仍写入确认日志，不绕过角色留痕。
        for item in svc.storage.list_pending_items(app_id, open_only=True):
            from filing.models import Role

            if item.role == Role.POLICY_OFFICER and app.rule:
                svc.engine.confirm_rule_choice(
                    item.item_id, app.rule,
                    app.policy_revision or 1, "样例回放专员",
                )
            elif item.role == Role.MATERIAL_REVIEWER:
                svc.engine.review_material(
                    app_id, item.related_material, True, "样例回放审核员",
                )
        app = svc.storage.get_application(app_id)
        if app.state.value == "ready":
            svc.submissions.submit(app_id)
        submissions = svc.storage.list_submissions(app_id)
        if not submissions:
            continue
        portal_ref = row["portal_ref"]
        # 用样例回执编号替换模拟编号，保证引用关系与样例一致。
        svc.storage.update_submission(
            submissions[-1].submission_id, submissions[-1].state,
            portal_ref=portal_ref,
        )
        files = row["files"].split("|") if row["files"] else []
        kind = ReceiptKind(row["state"])
        accepted = files if kind == ReceiptKind.ACCEPTED else files[:1]
        rejected = files[1:] if kind != ReceiptKind.ACCEPTED else []
        svc.receipts.ingest(
            app_id, portal_ref, kind,
            raw_text=f"样例回执 {row['submission']}（{row['state']}）",
            accepted_files=accepted, rejected_files=rejected,
            received_at=row["received"],
        )

    print("环节统计:", svc.dashboard.stage_summary())
    for view in svc.dashboard.list_views():
        print(f"- {view.app_id}: 环节={view.stage} "
              f"提交次数={len(view.submissions)} "
              f"回执={len(view.receipts)} 待办={len(view.action_items)} "
              f"待确认={len(view.pending_items)}")
    svc.commit()
    svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
