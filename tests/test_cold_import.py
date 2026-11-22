import unittest

from selector import cold_import_unaffected, parse_diff


def modification(path):
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"


class ColdImportTests(unittest.TestCase):
    def test_unopened_existing_module_does_not_affect_cold_start(self):
        file = "sqlglot/dialects/example.py"
        entries = parse_diff(modification(file))
        self.assertTrue(cold_import_unaffected(entries, {file}, {"sqlglot/__init__.py"}))

    def test_loaded_module_or_other_change_requires_the_test(self):
        file = "sqlglot/dialects/example.py"
        patch = modification(file)
        entries = parse_diff(patch)
        self.assertFalse(cold_import_unaffected(entries, {file}, {file}))
        self.assertFalse(cold_import_unaffected(entries, set(), {"sqlglot/__init__.py"}))
        mixed = parse_diff(patch + modification("tests/dialects/test_dialect.py"))
        self.assertFalse(cold_import_unaffected(mixed, {file}, {"sqlglot/__init__.py"}))

    def test_missing_trace_or_new_source_has_no_proof(self):
        file = "sqlglot/dialects/example.py"
        self.assertFalse(cold_import_unaffected(parse_diff(modification(file)), {file}, set()))
        added = parse_diff(f"--- /dev/null\n+++ b/{file}\n@@ -0,0 +1 @@\n+new\n")
        self.assertFalse(cold_import_unaffected(added, {file}, {"sqlglot/__init__.py"}))
