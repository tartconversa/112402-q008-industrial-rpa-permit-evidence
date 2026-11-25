"""回执处理、可办理事项与补件重提测试。"""
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from _helpers import make_service, seed_application_with_materials

from filing.models import (
    ActionStatus,
    AppState,
    ReceiptKind,
    SubmissionState,
)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.svc = make_service(
            Path(self.tmp.name), today=date(2026, 11, 25)
        )
        self.app_id = seed_application_with_materials(self.svc)
        self.svc.engine.evaluate(self.app_id)
        self.sub = self.svc.submissions.submit(self.app_id)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_受理回执锁定提交时的政策与材料版本(self):
        self.svc.receipts.ingest(
            self.app_id, self.sub.portal_ref, ReceiptKind.ACCEPTED,
            "材料齐全，予以受理。",
            accepted_files=["budget.pdf", "scope.pdf"],
        )
        app = self.svc.storage.get_application(self.app_id)
        self.assertEqual(app.state, AppState.ACCEPTED)
        self.assertEqual(app.locked_rule, "P1")
        self.assertEqual(app.locked_policy_revision, 2)
        self.assertEqual(app.locked_material_version, self.sub.material_version)

    def test_受理后指南更新只生成政策变更判断项不替换版本(self):
        self.svc.receipts.ingest(
            self.app_id, self.sub.portal_ref, ReceiptKind.ACCEPTED,
            "予以受理。",
        )
        from filing.models import PolicyRule

        self.svc.storage.upsert_policy_rule(PolicyRule(
            rule="P1", revision=3, category="energy-saving", location="Zone-A",
            min_investment=1000000, valid_from=date(2026, 12, 1), valid_to=None,
            state="active", source_url="x", snapshot_id="s2",
            fetched_at="2026-11-30T00:00:00",
        ))
        self.svc.storage.commit()
        result = self.svc.engine.evaluate(self.app_id)
        self.assertTrue(result.blocking)
        item = result.pending_created[0]
        self.assertEqual(item.related_rule, "P1")
        # 锁定版本保持不变。
        app = self.svc.storage.get_application(self.app_id)
        self.assertEqual(app.locked_policy_revision, 2)
        # 工作人员判断维持原版后，待确认项解除。
        self.svc.engine.resolve_policy_change(
            self.app_id, keep_locked=True, actor="专员李",
            comment="在办项目不受新版约束",
        )
        self.assertEqual(
            self.svc.storage.list_pending_items(self.app_id, open_only=True), []
        )

    def test_部分受理回执生成按条件映射的可办理事项(self):
        receipt = self.svc.receipts.ingest(
            self.app_id, self.sub.portal_ref, ReceiptKind.PARTIAL,
            "预算证明予以接受；改造范围说明请补充后重新提交。",
            accepted_files=["budget.pdf"],
            rejected_files=["scope.pdf"],
        )
        app = self.svc.storage.get_application(self.app_id)
        self.assertEqual(app.state, AppState.PARTIAL)
        actions = self.svc.storage.list_action_items(self.app_id)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.material_type, "scope_statement")
        self.assertTrue(action.condition_ref.startswith("P1v2#"))
        self.assertEqual(action.receipt_id, receipt.receipt_id)
        self.assertEqual(action.status, ActionStatus.OPEN)

    def test_补正文本可自动解析出材料类型(self):
        findings = self.svc.receipts.parse_issues(
            "经审核存在以下问题：\n"
            "缺少投资预算证明，请补充；\n"
            "现场照片不予接受，需重新拍摄。"
        )
        types = {f["material_type"] for f in findings}
        self.assertEqual(types, {"budget_proof", "site_photo"})

    def test_补件后只重新提交受影响附件且保留完整历史(self):
        self.svc.receipts.ingest(
            self.app_id, self.sub.portal_ref, ReceiptKind.PARTIAL,
            "改造范围说明退回。",
            accepted_files=["budget.pdf"],
            rejected_files=["scope.pdf"],
        )
        # 未办结事项前禁止重提。
        with self.assertRaises(RuntimeError):
            self.svc.receipts.resubmit_affected(self.app_id)

        material = self.svc.receipts.supply_material(
            self.app_id, "scope.pdf", "scope_statement",
            b"scope content v2 corrected",
        )
        # 材料版本按申请全局递增：budget=v1、scope=v2，本次补件为 v3。
        self.assertEqual(material.version, 3)
        self.assertIsNotNone(material.supersedes)
        # 事项进入“已补件待审核”，仍阻断提交。
        self.assertEqual(
            self.svc.storage.list_action_items(
                self.app_id, open_only=True
            )[0].status,
            ActionStatus.MATERIAL_SUPPLIED,
        )
        with self.assertRaises(RuntimeError):
            self.svc.receipts.resubmit_affected(self.app_id)

        action = self.svc.storage.list_action_items(self.app_id)[0]
        self.svc.receipts.review_supplied_material(
            action.action_id, sufficient=True, actor="审核员王"
        )
        new_sub = self.svc.receipts.resubmit_affected(self.app_id)
        self.assertEqual(new_sub.state, SubmissionState.SUCCEEDED)
        # 只重传受影响附件。
        self.assertEqual(new_sub.files, ["scope.pdf"])
        self.assertNotEqual(new_sub.submission_id, self.sub.submission_id)
        # 两次提交都在历史中，幂等键不同。
        history = self.svc.storage.list_submissions(self.app_id)
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0].idempotency_key, history[1].idempotency_key)

    def test_审核不通过时事项保持打开且可继续补件(self):
        self.svc.receipts.ingest(
            self.app_id, self.sub.portal_ref, ReceiptKind.CORRECTION,
            "预算证明缺少，请补充。",
            issues=[{"material_type": "budget_proof", "text": "预算证明缺少"}],
        )
        self.svc.receipts.supply_material(
            self.app_id, "budget.pdf", "budget_proof", b"bad supplement"
        )
        action = self.svc.storage.list_action_items(self.app_id)[0]
        self.svc.receipts.review_supplied_material(
            action.action_id, sufficient=False, actor="审核员王",
            comment="未盖公章",
        )
        refreshed = next(
            a for a in self.svc.storage.list_action_items(self.app_id)
            if a.action_id == action.action_id
        )
        self.assertEqual(refreshed.status, ActionStatus.OPEN)
        self.assertIn("未盖公章", refreshed.reason)

    def test_未知门户编号登记回执被拒绝(self):
        with self.assertRaises(RuntimeError):
            self.svc.receipts.ingest(
                self.app_id, "EXT-NOPE", ReceiptKind.ACCEPTED, "x"
            )


class IntakeAndDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.svc = make_service(Path(self.tmp.name))

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_相同内容材料不产生新版本(self):
        app_id = seed_application_with_materials(self.svc, app_id="A-300")
        first = self.svc.intake.intake_material(
            app_id, "budget.pdf", "budget_proof", b"budget content v1"
        )
        second = self.svc.intake.intake_material(
            app_id, "budget.pdf", "budget_proof", b"budget content v1"
        )
        self.assertEqual(first.material_id, second.material_id)

    def test_总览视图包含环节回执待补与提交历史(self):
        app_id = seed_application_with_materials(self.svc, app_id="A-301")
        self.svc.engine.evaluate(app_id)
        sub = self.svc.submissions.submit(app_id)
        self.svc.receipts.ingest(
            app_id, sub.portal_ref, ReceiptKind.ACCEPTED, "受理",
            accepted_files=sub.files,
        )
        view = self.svc.dashboard.view(app_id).to_dict()
        self.assertEqual(view["stage"], "accepted")
        self.assertIsNotNone(view["locked"])
        self.assertEqual(len(view["submissions"]), 1)
        self.assertEqual(len(view["receipts"]), 1)
        self.assertEqual(len(view["materials"]), 2)
        summary = self.svc.dashboard.stage_summary()
        self.assertEqual(summary["accepted"], 1)


if __name__ == "__main__":
    unittest.main()
