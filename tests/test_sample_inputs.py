"""Validate the delivered synthetic input files only."""
import csv
from pathlib import Path
import unittest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
HEADERS = {'policy': ['rule', 'category', 'location', 'min_investment', 'valid_from', 'revision', 'state'], 'applications': ['app', 'company', 'category', 'location', 'investment', 'rule', 'policy_revision', 'state'], 'receipts': ['submission', 'app', 'portal_ref', 'files', 'state', 'received']}
REFERENCES = [('applications', 'rule', 'policy', 'rule'), ('receipts', 'app', 'applications', 'app')]


class SampleInputs(unittest.TestCase):
    def read(self, name):
        with (SAMPLES / f"{name}.csv").open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            self.assertEqual(reader.fieldnames, HEADERS[name])
            rows = list(reader)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(None, row)
            self.assertNotIn(None, row.values())
        return rows

    def test_csv_files_have_documented_columns(self):
        for name in HEADERS:
            with self.subTest(name=name):
                self.read(name)

    def test_sample_references_resolve(self):
        for source, source_field, target, target_field in REFERENCES:
            with self.subTest(source=source, field=source_field, target=target):
                known = {row[target_field] for row in self.read(target)}
                observed = {row[source_field] for row in self.read(source)}
                self.assertTrue(observed)
                self.assertEqual(observed - known, set())


if __name__ == "__main__":
    unittest.main()
