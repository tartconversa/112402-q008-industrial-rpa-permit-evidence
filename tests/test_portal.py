"""门户提交编排测试：幂等、失败/超时核对、防重复申请。"""
import unittest
from datetime import date
from tempfile import TemporaryDirectory
from pathlib import Path

from _helpers import make_service, seed_application_with_materials

from filing.models import AppState, SubmissionState
from filing.portal import (
    FakePortalDriver,
    PortalError,
    make_idempotency_key,
)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.driver = FakePortalDriver()
        self.svc = make_service(
            Path(self.tmp.name), today=date(2026, 11, 25), driver=self.driver
        )
        self.app_id = seed_application_with_materials(self.svc)
        self.svc.engine.evaluate(self.app_id)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_就绪申请成功提交并生成门户编号(self):
        sub = self.svc.submissions.submit(self.app_id)
        self.assertEqual(sub.state, SubmissionState.SUCCEEDED)
        self.assertTrue(sub.portal_ref.startswith("EXT-"))
        self.assertEqual(
            self.svc.storage.get_application(self.app_id).state,
            AppState.SUBMITTED,
        )

    def test_幂等键由申请材料版本与政策版本共同决定(self):
        key1 = make_idempotency_key("A-1", 1, "P1", 2)
        key2 = make_idempotency_key("A-1", 2, "P1", 2)
        key3 = make_idempotency_key("A-1", 1, "P1", 1)
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertEqual(key1, make_idempotency_key("A-1", 1, "P1", 2))

    def test_重复调用提交不会产生第二张门户申请(self):
        first = self.svc.submissions.submit(self.app_id)
        second = self.svc.submissions.submit(self.app_id)
        self.assertEqual(first.submission_id, second.submission_id)
        self.assertEqual(second.state, SubmissionState.SUCCEEDED)
        submit_calls = [c for c in self.driver.calls if c.startswith("submit:")]
        self.assertEqual(len(submit_calls), 1)

    def test_有待确认项时提交被阻断(self):
        app_id = "A-200"
        self.svc.engine.ingest_application(
            app_id, company="C-200", category="green-building",
            location="Zone-Z", investment=100000,
        )
        self.svc.engine.evaluate(app_id)
        with self.assertRaises(PortalError):
            self.svc.submissions.submit(app_id)

    def test_超时但门户实际已建单时核对后复用而不是重提(self):
        driver = FakePortalDriver(timeout_next=1, materialize_on_timeout=True)
        svc = make_service(
            Path(self.tmp.name) / "timeout",
            today=date(2026, 11, 25), driver=driver,
        )
        app_id = seed_application_with_materials(svc, app_id="A-201")
        svc.engine.evaluate(app_id)
        try:
            sub = svc.submissions.submit(app_id)
            self.assertEqual(sub.state, SubmissionState.SUCCEEDED)
            self.assertIsNotNone(sub.portal_ref)
            self.assertIn("核对门户确认已建单", sub.detail)
            # 再次提交直接复用，门户侧始终只有一张单。
            again = svc.submissions.submit(app_id)
            self.assertEqual(again.portal_ref, sub.portal_ref)
            self.assertEqual(
                len([c for c in driver.calls if c.startswith("submit:")]), 1
            )
        finally:
            svc.close()

    def test_上传失败且门户无单时标记失败允许重试(self):
        driver = FakePortalDriver(fail_next=1)
        svc = make_service(
            Path(self.tmp.name) / "fail",
            today=date(2026, 11, 25), driver=driver,
        )
        app_id = seed_application_with_materials(svc, app_id="A-202")
        svc.engine.evaluate(app_id)
        try:
            failed = svc.submissions.submit(app_id)
            self.assertEqual(failed.state, SubmissionState.FAILED)
            # 重试成功，沿用同一提交记录与幂等键。
            recovered = svc.submissions.retry_failed(app_id)
            self.assertEqual(recovered.state, SubmissionState.SUCCEEDED)
            self.assertEqual(recovered.submission_id, failed.submission_id)
            self.assertEqual(recovered.idempotency_key, failed.idempotency_key)
        finally:
            svc.close()

    def test_重试前发现门户已建单则转为重复状态(self):
        driver = FakePortalDriver(fail_next=1, )
        svc = make_service(
            Path(self.tmp.name) / "dup",
            today=date(2026, 11, 25), driver=driver,
        )
        app_id = seed_application_with_materials(svc, app_id="A-203")
        svc.engine.evaluate(app_id)
        try:
            failed = svc.submissions.submit(app_id)
            self.assertEqual(failed.state, SubmissionState.FAILED)
            # 模拟门户侧实际已建单（人工或其他渠道补录）。
            driver.portal_side[failed.idempotency_key] = type(
                "S", (),
                {"exists": True, "portal_ref": "EXT-MANUAL",
                 "state": "submitted", "files": failed.files},
            )()
            recovered = svc.submissions.retry_failed(app_id)
            self.assertEqual(recovered.state, SubmissionState.DUPLICATE)
            self.assertEqual(recovered.portal_ref, "EXT-MANUAL")
        finally:
            svc.close()

    def test_提交历史完整保留每次尝试(self):
        driver = FakePortalDriver(fail_next=1)
        svc = make_service(
            Path(self.tmp.name) / "history",
            today=date(2026, 11, 25), driver=driver,
        )
        app_id = seed_application_with_materials(svc, app_id="A-204")
        svc.engine.evaluate(app_id)
        try:
            svc.submissions.submit(app_id)
            svc.submissions.retry_failed(app_id)
            history = svc.storage.list_submissions(app_id)
            self.assertEqual(len(history), 1)  # 同幂等键一条记录
            self.assertEqual(history[0].state, SubmissionState.SUCCEEDED)
            self.assertIn("核对门户确认无此单", history[0].detail)
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
