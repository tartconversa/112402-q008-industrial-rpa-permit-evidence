"""规则引擎测试：适用性判断、待确认项、角色确认与受理锁定。"""
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from _helpers import make_service, seed_application_with_materials

from filing.models import (
    AppState, PendingItem, PendingStatus, PendingType, Role,
)
from filing.rules import RuleEngine


class RuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.svc = make_service(
            Path(self.tmp.name), today=date(2026, 11, 25),
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_当前日期落在新版生效期后自动适用新版本(self):
        """2026-11-25 晚于 P1v2 生效日 2026-10-01，应选 v2 而非 v1。"""
        app_id = seed_application_with_materials(self.svc)
        result = self.svc.engine.evaluate(app_id)
        self.assertEqual(result.chosen_rule.rule, "P1")
        self.assertEqual(result.chosen_rule.revision, 2)
        self.assertFalse(result.blocking)
        app = self.svc.storage.get_application(app_id)
        self.assertEqual(app.state, AppState.READY)

    def test_新版生效前旧版继续适用(self):
        svc = make_service(
            Path(self.tmp.name) / "early",
            today=date(2026, 9, 1),
        )
        app_id = seed_application_with_materials(svc)
        try:
            result = svc.engine.evaluate(app_id)
            self.assertEqual(result.chosen_rule.revision, 1)
        finally:
            svc.close()

    def test_同日多版本生效产生冲突且不自动选择最宽松条款(self):
        """构造两个同日生效修订：v2 门槛 80 万（宽松）、v3 门槛 90 万（严格）。"""
        storage = self.svc.storage
        from filing.models import PolicyRule

        storage.upsert_policy_rule(PolicyRule(
            rule="P1", revision=3, category="energy-saving", location="Zone-A",
            min_investment=900000, valid_from=date(2026, 10, 1), valid_to=None,
            state="active", source_url="x", snapshot_id="s",
            fetched_at="2026-11-25T00:00:00",
        ))
        storage.commit()
        app_id = seed_application_with_materials(self.svc, investment=850000)
        result = self.svc.engine.evaluate(app_id)
        self.assertIsNone(result.chosen_rule)
        conflict = next(
            p for p in self.svc.storage.list_pending_items(app_id, open_only=True)
            if p.type == PendingType.RULE_CONFLICT
        )
        self.assertEqual(conflict.role, Role.POLICY_OFFICER)
        self.assertIn("不会自动选择门槛最低", conflict.question)
        app = storage.get_application(app_id)
        self.assertEqual(app.state, AppState.PENDING_CONFIRM)

    def test_投资额低于门槛时挂起而非退回式自动判断(self):
        app_id = seed_application_with_materials(self.svc, investment=700000)
        result = self.svc.engine.evaluate(app_id)
        self.assertIsNone(result.chosen_rule)
        items = self.svc.storage.list_pending_items(app_id, open_only=True)
        self.assertTrue(any("低于" in p.question for p in items))

    def test_缺少材料生成材料缺口待确认项并阻断提交(self):
        app_id = "A-101"
        self.svc.engine.ingest_application(
            app_id, company="C-101", category="energy-saving",
            location="Zone-A", investment=900000,
        )
        self.svc.engine.evaluate(app_id)
        gaps = [
            p for p in self.svc.storage.list_pending_items(app_id, open_only=True)
            if p.type == PendingType.MATERIAL_GAP
        ]
        self.assertEqual({p.related_material for p in gaps},
                         {"budget_proof", "scope_statement"})
        self.assertEqual(
            self.svc.storage.get_application(app_id).state,
            AppState.PENDING_CONFIRM,
        )

    def test_企业事实前后冲突生成经办人待确认项(self):
        app_id = seed_application_with_materials(self.svc)
        self.svc.engine.ingest_application(
            app_id, company="C-100", category="energy-saving",
            location="Zone-C", investment=900000,  # 地点变化
        )
        items = self.svc.storage.list_pending_items(app_id, open_only=True)
        self.assertTrue(any(p.type == PendingType.FACT_CONFLICT for p in items))
        # 经办人确认采用新值后重新评估。
        fact_item = next(
            p for p in items if p.type == PendingType.FACT_CONFLICT
        )
        self.svc.engine.confirm_facts(
            app_id, "经办人张", adopt_new=True, location="Zone-C"
        )
        self.assertEqual(
            self.svc.storage.get_application(app_id).location, "Zone-C"
        )
        # 新地点无匹配条款，应转成条款不清待确认。
        reopened = self.svc.storage.list_pending_items(app_id, open_only=True)
        self.assertTrue(all(p.item_id != fact_item.item_id for p in reopened))

    def test_角色越权确认被拒绝(self):
        app_id = seed_application_with_materials(self.svc, investment=600000)
        self.svc.engine.evaluate(app_id)
        item = next(
            p for p in self.svc.storage.list_pending_items(app_id, open_only=True)
            if p.type in (PendingType.RULE_CONFLICT, PendingType.RULE_UNCLEAR)
        )
        with self.assertRaises(PermissionError):
            self.svc.engine.confirm_pending(
                item.item_id, "企业人员试图越权确认", "经办人张",
                Role.ENTERPRISE_CONTACT,
            )

    def test_政策专员指定冲突版本后申请可继续(self):
        storage = self.svc.storage
        from filing.models import PolicyRule

        storage.upsert_policy_rule(PolicyRule(
            rule="P1", revision=3, category="energy-saving", location="Zone-A",
            min_investment=900000, valid_from=date(2026, 10, 1), valid_to=None,
            state="active", source_url="x", snapshot_id="s",
            fetched_at="2026-11-25T00:00:00",
        ))
        storage.commit()
        app_id = seed_application_with_materials(self.svc, investment=950000)
        self.svc.engine.evaluate(app_id)
        item = next(
            p for p in self.svc.storage.list_pending_items(app_id, open_only=True)
            if p.type == PendingType.RULE_CONFLICT
        )
        self.svc.engine.confirm_rule_choice(item.item_id, "P1", 3, "专员李")
        app = storage.get_application(app_id)
        self.assertEqual(app.state, AppState.READY)
        self.assertEqual(app.policy_revision, 3)
        # 确认日志留痕。
        log = storage.list_confirmation_log(app_id)
        self.assertTrue(any("指定条款版本" in row["decision"] for row in log))

    def test_无任何匹配条款时生成适用性不清项(self):
        app_id = "A-102"
        self.svc.engine.ingest_application(
            app_id, company="C-102", category="green-building",
            location="Zone-Z", investment=100000,
        )
        result = self.svc.engine.evaluate(app_id)
        self.assertTrue(result.blocking)
        self.assertTrue(
            any(p.type == PendingType.RULE_UNCLEAR
                for p in storage_pending(self.svc, app_id))
        )

    def test_材料审核员认定充分后缺口事项解除(self):
        app_id = "A-103"
        self.svc.engine.ingest_application(
            app_id, company="C-103", category="energy-saving",
            location="Zone-A", investment=900000,
        )
        self.svc.engine.evaluate(app_id)
        self.svc.intake.intake_material(
            app_id, "budget.pdf", "budget_proof", b"budget"
        )
        self.svc.intake.intake_material(
            app_id, "scope.pdf", "scope_statement", b"scope"
        )
        self.svc.engine.evaluate(app_id)
        self.assertEqual(
            self.svc.storage.get_application(app_id).state, AppState.READY
        )

    def test_仅有未来生效版本时挂起等待政策专员确认(self):
        """当前无生效条款、只有未来版本：不得提前自动适用新版。"""
        from filing.models import PolicyRule

        for revision, valid_from, threshold in (
            (1, date(2027, 1, 1), 500000),
            (2, date(2027, 6, 1), 800000),
        ):
            self.svc.storage.upsert_policy_rule(PolicyRule(
                rule="P9", revision=revision, category="green-future",
                location="Zone-F", min_investment=threshold,
                valid_from=valid_from, valid_to=None, state="active",
                source_url="x", snapshot_id="s", fetched_at="2026-11-25T00:00:00",
            ))
        self.svc.storage.commit()
        app_id = "A-105"
        self.svc.engine.ingest_application(
            app_id, company="C-105", category="green-future",
            location="Zone-F", investment=900000,
        )
        result = self.svc.engine.evaluate(app_id)
        self.assertIsNone(result.chosen_rule)
        self.assertTrue(result.blocking)
        item = self.svc.storage.list_pending_items(app_id, open_only=True)[0]
        self.assertEqual(item.type, PendingType.RULE_UNCLEAR)
        self.assertIn("尚未到生效日期", item.question)

    def test_审核员否决的旧材料不会因重新评估而自动解除缺口(self):
        app_id = "A-106"
        self.svc.engine.ingest_application(
            app_id, company="C-106", category="energy-saving",
            location="Zone-A", investment=900000,
        )
        self.svc.engine.evaluate(app_id)
        self.svc.intake.intake_material(
            app_id, "budget.pdf", "budget_proof", b"budget v1"
        )
        self.svc.intake.intake_material(
            app_id, "scope.pdf", "scope_statement", b"scope v1"
        )
        self.svc.engine.evaluate(app_id)  # 材料齐备，缺口解除
        # 模拟审核员对 scope 材料不满意，重新挂起缺口。
        self.svc.engine.review_material(
            app_id, "scope_statement", sufficient=False,
            actor="审核员王", comment="内容不完整",
        )
        from filing.storage import now_iso

        item = PendingItem(
            item_id=None, app_id=app_id, type=PendingType.MATERIAL_GAP,
            role=Role.MATERIAL_REVIEWER,
            question="scope_statement 复核不通过，需补交新版",
            status=PendingStatus.OPEN, created_at=now_iso(),
            resolved_at=None, resolution="",
            options=["补交 scope_statement"],
            related_rule="P1", related_material="scope_statement",
        )
        self.svc.storage.add_pending_item(item)
        self.svc.engine.evaluate(app_id)
        # 旧材料仍在，但缺口不应被自动解除。
        still_open = self.svc.storage.list_pending_items(app_id, open_only=True)
        self.assertTrue(
            any(p.related_material == "scope_statement" for p in still_open)
        )
        # 补交新版材料后缺口才解除。
        self.svc.intake.intake_material(
            app_id, "scope.pdf", "scope_statement", b"scope v2 fixed"
        )
        self.svc.engine.evaluate(app_id)
        remaining = [
            p for p in self.svc.storage.list_pending_items(app_id, open_only=True)
            if p.related_material == "scope_statement"
        ]
        self.assertEqual(remaining, [])

    def test_同一待确认问题不重复挂起(self):
        app_id = "A-104"
        self.svc.engine.ingest_application(
            app_id, company="C-104", category="green-building",
            location="Zone-Z", investment=100000,
        )
        self.svc.engine.evaluate(app_id)
        self.svc.engine.evaluate(app_id)
        items = self.svc.storage.list_pending_items(app_id, open_only=True)
        self.assertEqual(len(items), 1)


def storage_pending(svc, app_id):
    return svc.storage.list_pending_items(app_id, open_only=True)


if __name__ == "__main__":
    unittest.main()
