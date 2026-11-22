import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class QueryIndexTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.inputs = Path(directory.name, "inputs.json")
        self.source = Path(directory.name, "source.tgz")
        self.probe = Path(directory.name, "probe.py")
        self.index = Path(directory.name, "query_lines.json.gz")
        self.inputs.write_text(json.dumps({"queries": [[], [], [], []]}), encoding="utf-8")
        self.source.write_bytes(b"pinned sources")
        self.probe.write_bytes(b"pinned probe")
        data = {
            "version": 2,
            "inputs_sha256": hashlib.sha256(self.inputs.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "probe_sha256": hashlib.sha256(self.probe.read_bytes()).hexdigest(),
            "query_count": 4,
            "lines": {
                "sqlglot/parser.py": {"10": 0b1010, "11": 0b10001},
                "sqlglot/schema.py": {"20": 0b0100},
            },
        }
        self.index.write_bytes(gzip.compress(json.dumps(data).encode()))

    def load_index(self):
        from tpcds_query_index import QueryIndex

        return QueryIndex.load(self.index, self.inputs, self.source, self.probe)

    def test_unions_query_coverage_in_fixture_order(self):
        index = self.load_index()
        self.assertEqual(index.affected_queries({
            "sqlglot/parser.py": {9, 10},
            "sqlglot/schema.py": {20},
        }), (1, 2, 3))

    def test_startup_or_unknown_changes_cannot_be_screened(self):
        index = self.load_index()
        for changed in ({"sqlglot/parser.py": {11}}, {"sqlglot/parser.py": {99}},
                        {"sqlglot/new.py": {10}, "sqlglot/parser.py": {10}}, {}):
            with self.subTest(changed=changed):
                self.assertIsNone(index.affected_queries(changed))

    def test_ignores_index_when_inputs_change(self):
        self.inputs.write_text('{"queries":[]}', encoding="utf-8")
        self.assertIsNone(self.load_index())

    def test_ignores_index_when_probe_or_source_changes(self):
        for path in (self.source, self.probe):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(before + b" changed")
                self.assertIsNone(self.load_index())
                path.write_bytes(before)

    def test_ignores_invalid_index_file(self):
        self.index.write_bytes(b"not gzip")
        self.assertIsNone(self.load_index())


class ProbeQuerySelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name)
        with tarfile.open(ROOT / "sqlglot_src.tgz", "r:gz") as archive:
            archive.extractall(cls.directory, filter="data")
        cls.inputs = cls.directory / "inputs.json"
        cls.inputs.write_text(json.dumps({
            "schema": {},
            "queries": [["SELECT 1", False], ["SELECT 2", False], ["SELECT 3", False]],
        }), encoding="utf-8")
        cls.full_baseline = json.loads(cls.run_probe().stdout)

    @classmethod
    def run_probe(cls, *args):
        env = {**os.environ, "PYTHONPATH": str(cls.directory), "PYTHONHASHSEED": "0"}
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "tpcds_probe.py"), str(cls.inputs), *args],
            cwd=cls.directory, env=env, check=True, capture_output=True, text=True,
        )

    def test_subset_fingerprints_preserve_query_order(self):
        subset = json.loads(self.run_probe("--queries", "2,0,2").stdout)
        self.assertEqual(subset, [self.full_baseline[0], self.full_baseline[2]])

    def test_subset_compares_against_original_baseline_indices(self):
        base = self.directory / "mismatch.json"
        base.write_text(json.dumps(["wrong", *self.full_baseline[1:]]), encoding="utf-8")
        self.assertEqual(self.run_probe(str(base)).stdout.strip(), "DIFF")
        self.assertEqual(self.run_probe(str(base), "--queries", "2,1").stdout.strip(), "SAME")
        self.assertEqual(self.run_probe(str(base), "--queries", "0,2").stdout.strip(), "DIFF")

    def test_subset_does_not_accept_an_incomplete_baseline(self):
        base = self.directory / "incomplete.json"
        base.write_text(json.dumps(self.full_baseline[:2]), encoding="utf-8")
        self.assertEqual(self.run_probe(str(base), "--queries", "1").stdout.strip(), "DIFF")


if __name__ == "__main__":
    unittest.main()
