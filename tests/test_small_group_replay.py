import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.verify_small_groups import PassingGroup, offer_group


def report(nodeid, phase, outcome="passed"):
    return SimpleNamespace(nodeid=nodeid, when=phase, passed=outcome == "passed")


class PassingGroupTests(unittest.TestCase):
    def test_requires_exact_collection_and_all_phases(self):
        tracker = PassingGroup({"test_a.py::test_one", "test_a.py::test_two"})
        tracker.pytest_collection_finish(SimpleNamespace(
            items=[SimpleNamespace(nodeid=name) for name in tracker.expected]
        ))
        for phase in ("setup", "call", "teardown"):
            tracker.pytest_runtest_logreport(report("test_a.py::test_one", phase))
        tracker.pytest_runtest_logreport(report("test_a.py::test_two", "setup"))
        self.assertFalse(tracker.confirmed())
        for phase in ("call", "teardown"):
            tracker.pytest_runtest_logreport(report("test_a.py::test_two", phase))
        self.assertTrue(tracker.confirmed())

    def test_a_failed_subtest_invalidates_a_passing_parent_report(self):
        tracker = PassingGroup({"test_a.py::test_one"})
        tracker.pytest_collection_finish(SimpleNamespace(
            items=[SimpleNamespace(nodeid="test_a.py::test_one")]
        ))
        tracker.pytest_runtest_logreport(report("test_a.py::test_one", "call", "failed"))
        for phase in ("setup", "call", "teardown"):
            tracker.pytest_runtest_logreport(report("test_a.py::test_one", phase))
        self.assertFalse(tracker.confirmed())

    def test_unexpected_work_invalidates_confirmation(self):
        tracker = PassingGroup({"test_a.py::test_one"})
        with self.assertRaises(RuntimeError):
            tracker.audit("subprocess.Popen", ("worker",))
        self.assertFalse(tracker.confirmed())

    def test_reading_original_source_disables_an_overlay_pass(self):
        source = Path.cwd() / "sqlglot" / "example.py"
        tracker = PassingGroup({"test_a.py::test_one"}, original_path=source)
        with self.assertRaises(RuntimeError):
            tracker.audit("open", (str(source), "r", 0))
        self.assertFalse(tracker.confirmed())


class OfferGroupTests(unittest.TestCase):
    def test_only_a_small_entire_schedule_with_an_existing_source_edit_is_offered(self):
        tests = {1: "tests/test_a.py::test_one", 2: "tests/test_a.py::test_two"}
        source = "sqlglot/example.py"
        patch = f"--- a/{source}\n+++ b/{source}\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n"
        stock = "def f():\n    return 1\n"
        offer = offer_group([[1, 2]], patch, tests, {1: 40, 2: 70},
                            lambda path: stock, group_limit=4, total_ms=150)
        self.assertEqual(offer.file, source)
        self.assertEqual(offer.nodeids, tuple(tests.values()))
        self.assertIn("return 2", offer.patched)
        self.assertIsNone(offer_group([[1], [2]], patch, tests, {1: 40, 2: 70},
                                      lambda path: stock, group_limit=4, total_ms=150))
        self.assertIsNone(offer_group([[1, 2]], patch, tests, {1: 40, 2: 170},
                                      lambda path: stock, group_limit=4, total_ms=150))
        self.assertIsNone(offer_group([[1]], patch.replace("sqlglot/", "tests/"),
                                      tests, {1: 40}, lambda path: stock,
                                      group_limit=4, total_ms=150))
