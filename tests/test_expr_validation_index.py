import gzip
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from setting_index import catalog_digest


BEFORE = '''\
class Parent(Expression):
    arg_types = {"this": True, "index": False}

class Child(Parent):
    pass
'''
AFTER = BEFORE.replace('"index": False', '"index": True')
FILE = "sqlglot/expressions/example.py"


class ExpressionValidationIndexTests(unittest.TestCase):
    def test_recognizes_one_existing_boolean_in_an_ordinary_class(self):
        import expr_validation_index

        self.assertEqual(expr_validation_index.edited_arg_type(BEFORE, AFTER), ("Parent", "index"))
        self.assertEqual(expr_validation_index.edited_arg_type(AFTER, BEFORE), ("Parent", "index"))

    def test_rejects_other_semantic_edits(self):
        import expr_validation_index

        for revised in (
            BEFORE,
            AFTER.replace('"this": True', '"this": False'),
            AFTER.replace('"this": True, ', ""),
            AFTER.replace('"index": True', '"index": 1'),
            AFTER.replace('"this": True, "index": True', '"index": True, "this": True'),
            AFTER.replace("    pass", "    other = True"),
            AFTER.replace("class Child(Parent)", "class Child(Other)"),
            AFTER.replace('"index": True', '"index": True, "index": False'),
        ):
            with self.subTest(revised=revised):
                self.assertIsNone(expr_validation_index.edited_arg_type(BEFORE, revised))

    def test_index_keeps_validation_whole_readers_and_unverified_tests(self):
        from expr_validation_index import ExprValidationIndex

        tests = [{"id": str(n), "number": n} for n in range(5)]
        index = {
            "version": 1,
            "catalog": catalog_digest(test["id"] for test in tests),
            "verified": ["0", "1", "2", "3"],
            "source": {FILE: sha256(BEFORE.encode()).hexdigest()},
            "files": {FILE: {"Parent": {"whole": ["1"], "keys": {"index": ["2"]}}}},
        }
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "index.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as output:
                json.dump(index, output)
            loaded = ExprValidationIndex.load(path, tests)
            self.assertIsNone(ExprValidationIndex.load(path, tests[:-1]))

        self.assertEqual(loaded.narrow(FILE, BEFORE, AFTER, {0, 1, 3, 4}), {1, 2, 4})
        self.assertIsNone(loaded.narrow(FILE, BEFORE + "\n", AFTER, {0, 1, 2, 3, 4}))
        self.assertIsNone(loaded.narrow(FILE, BEFORE, AFTER.replace('"index": True', '"other": False'), {0}))

    def test_index_leaves_blocked_keys_to_the_original_selector(self):
        from expr_validation_index import ExprValidationIndex

        index = ExprValidationIndex(
            {1}, {FILE: {"Parent": (set(), {"index": None})}},
            {FILE: sha256(BEFORE.encode()).hexdigest()},
        )
        self.assertIsNone(index.narrow(FILE, BEFORE, AFTER, {1}))
        other = BEFORE.replace('"this": True', '"this": False')
        self.assertEqual(index.narrow(FILE, BEFORE, other, {1}), set())

    def test_recording_distinguishes_inherited_validation_and_shape_only_reads(self):
        from tools.record_expr_validation import CORE, Recorder

        source = '''\
class Expr:
    arg_types = {"first": False, "second": True, "third": True}

    def __init_subclass__(cls):
        cls.required_args = {k for k, v in cls.arg_types.items() if v}

class Expression(Expr):
    def __init__(self, **args):
        self.args = args

    def error_messages(self):
        missing = []
        for k in self.required_args:
            if self.args.get(k) is None:
                missing.append(k)
        return missing

class Parent(Expression):
    arg_types = {"first": False, "second": True, "third": True}

class Child(Parent):
    pass

class Shadow(Parent):
    arg_types = {"first": False, "second": True}

def raw_read():
    return getattr(Child, "arg_types")

def keyed_read():
    return "first" in Child.arg_types
'''
        recorder = Recorder(Path.cwd(), {"missing", "present", "multiple", "raw", "keyed", "shadow"})
        namespace = recorder.namespace()
        exec(recorder.compile(CORE, source, CORE), namespace)
        child, shadow = namespace["Child"], namespace["Shadow"]
        with recorder.in_test("missing"):
            self.assertEqual(child(first=[], second=1, third=2).error_messages(), [])
        with recorder.in_test("present"):
            child(first=1, second=1, third=2).error_messages()
        with recorder.in_test("multiple"):
            child(first=1).error_messages()
        with recorder.in_test("raw"):
            self.assertEqual(namespace["raw_read"](), {"first": False, "second": True, "third": True})
        with recorder.in_test("keyed"):
            self.assertTrue(namespace["keyed_read"]())
        with recorder.in_test("shadow"):
            shadow(second=1).error_messages()

        self.assertEqual(recorder.reads[(CORE, "Parent")], {"first": {"missing"}, None: {"raw", "multiple"}})
        self.assertEqual(recorder.reads[(CORE, "Shadow")], {"first": {"shadow"}})
        child(second=1, third=2).error_messages()
        self.assertEqual(recorder.blocked[(CORE, "Parent")], {"first"})


if __name__ == "__main__":
    unittest.main()
