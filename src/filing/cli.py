"""命令行入口：面向服务中心工作人员的手工操作界面。

示例：
  python -m filing.cli --db data/filing.db sync-policy --url file:///.../policy.csv
  python -m filing.cli --db data/filing.db register --app A-8 ...
  python -m filing.cli --db data/filing.db evaluate --app A-8
  python -m filing.cli --db data/filing.db pending
  python -m filing.cli --db data/filing.db submit --app A-8
  python -m filing.cli --db data/filing.db view --app A-8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .crawler import LocalSource
from .models import ReceiptKind
from .service import FilingService
from .storage import Storage


def build_service(args: argparse.Namespace) -> FilingService:
    storage = Storage(args.db)
    today = date.fromisoformat(args.today) if args.today else None
    source = LocalSource(args.local_root) if args.local_root else None
    return FilingService(storage, source=source, today=today)


def print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_sync_policy(args, svc: FilingService) -> None:
    print_json(svc.sync_policy(args.url))


def cmd_register(args, svc: FilingService) -> None:
    app = svc.engine.ingest_application(
        args.app, args.company, args.category, args.location,
        args.investment, rule=args.rule, policy_revision=args.revision,
        note=args.note or "",
    )
    svc.commit()
    print_json({"app_id": app.app_id, "state": app.state.value})


def cmd_intake(args, svc: FilingService) -> None:
    material = svc.intake.intake_material_file(
        args.app, args.file, args.material_type, note=args.note or ""
    )
    svc.commit()
    print_json({
        "material_id": material.material_id, "filename": material.filename,
        "version": material.version, "hash": material.content_hash[:12],
    })


def cmd_evaluate(args, svc: FilingService) -> None:
    result = svc.engine.evaluate(args.app)
    svc.commit()
    print_json({
        "candidate_rules": [
            f"{r.rule}v{r.revision}" for r in result.candidate_rules
        ],
        "chosen": (
            f"{result.chosen_rule.rule}v{result.chosen_rule.revision}"
            if result.chosen_rule else None
        ),
        "blocking": result.blocking,
        "pending_created": [p.question for p in result.pending_created],
    })


def cmd_pending(args, svc: FilingService) -> None:
    items = svc.storage.list_pending_items(args.app, open_only=True)
    print_json([
        {
            "item_id": p.item_id, "app_id": p.app_id, "type": p.type.value,
            "role": p.role.value, "question": p.question,
            "options": p.options,
        }
        for p in items
    ])


def cmd_confirm_rule(args, svc: FilingService) -> None:
    svc.engine.confirm_rule_choice(args.item, args.rule, args.revision, args.actor)
    svc.commit()
    print_json({"item_id": args.item, "resolved": True})


def cmd_confirm_facts(args, svc: FilingService) -> None:
    svc.engine.confirm_facts(
        args.app, args.actor, args.adopt_new,
        category=args.category, location=args.location,
        investment=args.investment,
    )
    svc.commit()
    print_json({"app_id": args.app, "facts_confirmed": True})


def cmd_review_material(args, svc: FilingService) -> None:
    svc.engine.review_material(
        args.app, args.material_type, args.sufficient, args.actor,
        comment=args.comment or "",
    )
    svc.commit()
    print_json({"app_id": args.app, "material_type": args.material_type,
                "sufficient": args.sufficient})


def cmd_submit(args, svc: FilingService) -> None:
    sub = svc.submissions.submit(args.app)
    svc.commit()
    print_json({
        "submission_id": sub.submission_id, "state": sub.state.value,
        "portal_ref": sub.portal_ref, "idempotency_key": sub.idempotency_key,
        "files": sub.files, "detail": sub.detail,
    })


def cmd_receipt(args, svc: FilingService) -> None:
    kind = ReceiptKind(args.kind)
    receipt = svc.receipts.ingest(
        args.app, args.portal_ref, kind, args.text,
        accepted_files=args.accepted or [],
        rejected_files=args.rejected or [],
        received_at=args.received_at,
    )
    svc.commit()
    print_json({
        "receipt_id": receipt.receipt_id, "kind": receipt.kind.value,
        "actions": [
            a.condition_ref
            for a in svc.storage.list_action_items(args.app)
        ],
    })


def cmd_supply(args, svc: FilingService) -> None:
    material = svc.receipts.supply_material(
        args.app, Path(args.file).name, args.material_type,
        Path(args.file).read_bytes(), note=args.note or "",
    )
    svc.commit()
    print_json({
        "material_id": material.material_id, "version": material.version,
        "filename": material.filename,
    })


def cmd_resubmit(args, svc: FilingService) -> None:
    sub = svc.receipts.resubmit_affected(args.app)
    svc.commit()
    print_json({
        "submission_id": sub.submission_id, "state": sub.state.value,
        "portal_ref": sub.portal_ref, "files": sub.files,
        "detail": sub.detail,
    })


def cmd_view(args, svc: FilingService) -> None:
    print_json(svc.dashboard.view(args.app).to_dict())


def cmd_summary(args, svc: FilingService) -> None:
    print_json(svc.dashboard.stage_summary())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filing", description="节能技改项目备案辅助系统"
    )
    parser.add_argument("--db", default="filing.db", help="SQLite 数据库路径")
    parser.add_argument("--today", help="覆盖当前日期（YYYY-MM-DD），用于测试生效边界")
    parser.add_argument("--local-root", help="本地政策页面根目录（离线采集源）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync-policy", help="采集政策页面与附件")
    p.add_argument("--url", required=True)
    p.set_defaults(func=cmd_sync_policy)

    p = sub.add_parser("register", help="登记企业申请")
    p.add_argument("--app", required=True)
    p.add_argument("--company", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--location", required=True)
    p.add_argument("--investment", type=int, required=True)
    p.add_argument("--rule")
    p.add_argument("--revision", type=int)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("intake", help="受理企业材料文件")
    p.add_argument("--app", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--material-type", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_intake)

    p = sub.add_parser("evaluate", help="评估条款适用性与材料覆盖")
    p.add_argument("--app", required=True)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("pending", help="列出待确认项")
    p.add_argument("--app")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("confirm-rule", help="政策专员指定适用条款版本")
    p.add_argument("--item", type=int, required=True)
    p.add_argument("--rule", required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_confirm_rule)

    p = sub.add_parser("confirm-facts", help="企业经办人确认事实冲突")
    p.add_argument("--app", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--adopt-new", action="store_true")
    p.add_argument("--category")
    p.add_argument("--location")
    p.add_argument("--investment", type=int)
    p.set_defaults(func=cmd_confirm_facts)

    p = sub.add_parser("review-material", help="材料审核员给出覆盖判断")
    p.add_argument("--app", required=True)
    p.add_argument("--material-type", required=True)
    p.add_argument("--sufficient", action="store_true")
    p.add_argument("--actor", required=True)
    p.add_argument("--comment", default="")
    p.set_defaults(func=cmd_review_material)

    p = sub.add_parser("submit", help="向门户填报提交")
    p.add_argument("--app", required=True)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("receipt", help="登记门户回执")
    p.add_argument("--app", required=True)
    p.add_argument("--portal-ref", required=True)
    p.add_argument("--kind", required=True,
                   choices=[k.value for k in ReceiptKind])
    p.add_argument("--text", required=True)
    p.add_argument("--accepted", nargs="*", default=[])
    p.add_argument("--rejected", nargs="*", default=[])
    p.add_argument("--received-at")
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("supply", help="企业补交材料")
    p.add_argument("--app", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--material-type", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_supply)

    p = sub.add_parser("resubmit", help="补正后重新提交受影响部分")
    p.add_argument("--app", required=True)
    p.set_defaults(func=cmd_resubmit)

    p = sub.add_parser("view", help="查看申请完整办理视图")
    p.add_argument("--app", required=True)
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("summary", help="按环节统计申请")
    p.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    svc = build_service(args)
    try:
        args.func(args, svc)
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
