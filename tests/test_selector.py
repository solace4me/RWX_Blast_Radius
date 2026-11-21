import unittest

from selector import apply_hunks, change_regions, equivalent_none_test, parse_diff


class SelectorTests(unittest.TestCase):
    def test_zero_context_insertion_goes_after_old_line(self):
        [change] = parse_diff(
            "--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n"
            "@@ -1,0 +2 @@\n+inserted\n"
        )
        self.assertEqual(apply_hunks("first\nsecond\n", change.hunks), "first\ninserted\nsecond\n")

    def test_none_rewrite_with_default_equality(self):
        old = "if value is None:\n    pass\n"
        observations = [[8, 12, [["builtins.NoneType", "custom"],
                                  ["example.Table", "builtins.object"]]]]
        self.assertTrue(equivalent_none_test(old, old.replace("is", "=="), {"1": observations}))
        self.assertFalse(equivalent_none_test(old, old.replace("is", "!="), {"1": observations}))
        self.assertFalse(
            equivalent_none_test(old, "if value == None:\n    raise ValueError\n", {"1": observations})
        )

    def test_none_rewrite_with_overloaded_equality(self):
        observations = [[8, 12, [["example.Value", "example.Value"]]]]
        self.assertFalse(equivalent_none_test("if value is None:\n", "if value == None:\n",
                                              {"1": observations}))

    def test_none_rewrite_changes_debug_fstring_text(self):
        old = "f'{value is None=}'"
        observations = [[8, 12, [["builtins.NoneType", "custom"]]]]
        self.assertFalse(equivalent_none_test(old, old.replace("is", "=="), {"1": observations}))

    def test_clause_change_selects_lines_in_the_try_statement(self):
        old = (
            "def f():\n"
            "    try:\n"
            "        pass\n"
            "    except RuntimeError:\n"
            "        return 1\n"
            "    finally:\n"
            "        print('finish')\n"
        )
        [change] = parse_diff(
            "--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n"
            "@@ -6 +6 @@\n-    finally:\n+    else:\n"
        )
        new = apply_hunks(old, change.hunks)
        regions = change_regions(old, new, change.hunks)
        self.assertTrue(any(kind == "runtime" and {3, 5, 7} <= lines for kind, lines in regions))

    def test_inline_clause_belongs_to_the_compound_statement(self):
        old = "def f(x):\n    if x:\n        return 1\n    else: return 2\n"
        [change] = parse_diff(
            "--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n"
            "@@ -4 +3,0 @@\n-    else: return 2\n"
        )
        new = apply_hunks(old, change.hunks)
        regions = change_regions(old, new, change.hunks)
        self.assertTrue(any(kind == "runtime" and {2, 3, 4} <= lines for kind, lines in regions))
