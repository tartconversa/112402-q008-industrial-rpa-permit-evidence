"""命令行入口端到端测试。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _helpers import build_local_site

from filing.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.site = self.root / "site"
        build_local_site(self.site)
        self.db = str(self.root / "filing.db")

    def tearDown(self):
        self.tmp.cleanup()

    def base_args(self, *extra: str) -> list[str]:
        return [
            "--db", self.db,
            "--local-root", str(self.site),
            "--today", "2026-11-25",
            *extra,
        ]

    def run_cli(self, *extra: str) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(self.base_args(*extra))
        return buf.getvalue()

    def test_完整命令行流程(self):
        out = self.run_cli("sync-policy", "--url", "file:///policy.csv")
        self.assertEqual(json.loads(out)["rules_seen"], 3)

        self.run_cli(
            "register", "--app", "A-CLI", "--company", "C-CLI",
            "--category", "energy-saving", "--location", "Zone-A",
            "--investment", "900000",
        )
        # 材料缺口阶段评估应阻断。
        blocked = json.loads(self.run_cli("evaluate", "--app", "A-CLI"))
        self.assertTrue(blocked["blocking"])

        for filename, mtype in [
            ("budget.pdf", "budget_proof"), ("scope.pdf", "scope_statement"),
        ]:
            path = self.root / filename
            path.write_bytes(f"{filename}-content".encode())
            self.run_cli(
                "intake", "--app", "A-CLI", "--file", str(path),
                "--material-type", mtype,
            )
        ready = json.loads(self.run_cli("evaluate", "--app", "A-CLI"))
        self.assertFalse(ready["blocking"])
        self.assertEqual(ready["chosen"], "P1v2")

        submitted = json.loads(self.run_cli("submit", "--app", "A-CLI"))
        self.assertEqual(submitted["state"], "succeeded")

        receipt = self.run_cli(
            "receipt", "--app", "A-CLI",
            "--portal-ref", submitted["portal_ref"],
            "--kind", "accepted", "--text", "予以受理",
            "--accepted", *submitted["files"],
        )
        self.assertEqual(json.loads(receipt)["kind"], "accepted")

        view = json.loads(self.run_cli("view", "--app", "A-CLI"))
        self.assertEqual(view["stage"], "accepted")
        self.assertIsNotNone(view["locked"])
        summary = json.loads(self.run_cli("summary"))
        self.assertEqual(summary["accepted"], 1)


if __name__ == "__main__":
    unittest.main()
