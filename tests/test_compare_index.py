import gzip
import json
from enum import Enum
import os
from pathlib import Path
import subprocess
import sys

import pytest

from compare_index import ComparisonIndex, comparison_edit, operand_fact, source_digest
from setting_index import catalog_digest
from tools.record_comparisons import FIRST, SECOND, SINGLETON, Recorder


FILE = "sqlglot/example.py"
CATALOG = [{"id": f"test_{i}", "number": i} for i in range(5)]
BEFORE = "def test(value):\n    return value <= 10\n"


def index_for(tmp_path, before, after, groups, catalog=CATALOG):
    edit = comparison_edit(before, after)
    assert edit is not None
    data = {
        "version": 1,
        "catalog": catalog_digest(test["id"] for test in CATALOG),
        "source": {FILE: source_digest(before)},
        "files": {FILE: [[list(edit.site), groups]]},
    }
    path = tmp_path / "comparison_reads.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(data, handle)
    return ComparisonIndex.load(path, catalog)


def pair(left, right):
    return (operand_fact(left), operand_fact(right), left is right, type(left) is type(right))


def test_narrow_keeps_a_test_with_any_changed_or_unrecorded_operand(tmp_path):
    after = BEFORE.replace("<=", "<")
    index = index_for(tmp_path, BEFORE, after, [
        [pair(3, 10), [0, 1]],
        [pair(10, 10), [1]],
        [pair(15, 10), [2]],
    ])
    assert index.narrow(FILE, BEFORE, after, {0, 1, 2, 3}) == {1, 3}


def test_runtime_index_checks_source_and_catalog(tmp_path):
    after = BEFORE.replace("<=", "<")
    index = index_for(tmp_path, BEFORE, after, [[pair(3, 10), [0]]])
    assert index.narrow(FILE, BEFORE + "# different source\n", after, {0}) is None
    assert index_for(tmp_path, BEFORE, after, [], catalog=CATALOG[:1]) is None


def test_records_spawned_tasks_and_keeps_unobserved_worker_tests(tmp_path):
    package = tmp_path / "sqlglot"
    package.mkdir()
    (package / "__init__.py").touch()
    before = """\
def check(value):
    return value <= 10

class Result:
    def __reduce__(self):
        return bool, (check(10),)

def reject_boundary():
    if check(10):
        raise ValueError("boundary")
"""
    (package / "example.py").write_text(before, encoding="utf-8")
    (tmp_path / "test_pool.py").write_text("""\
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import os
import pytest
from sqlglot.example import Result, check, reject_boundary

@pytest.fixture(scope="module")
def shared():
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        yield pool

def test_safe(shared):
    assert shared.submit(check, 3).result()

def test_boundary(shared):
    assert shared.submit(check, 10).result()

def test_result_serialization(shared):
    assert shared.submit(Result).result() is True

def test_unknown():
    assert check(3)
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"),
                             initializer=os.getpid) as pool:
        assert pool.submit(check, 10).result()

def test_main():
    assert check(3)

@pytest.fixture(scope="module")
def incomplete():
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        yield pool

def test_incomplete_safe(incomplete):
    assert incomplete.submit(check, 3).result()

def test_incomplete_exception(incomplete):
    with pytest.raises(ValueError, match="boundary"):
        incomplete.submit(reject_boundary).result()
""", encoding="utf-8")
    names = ("safe", "boundary", "result_serialization", "unknown", "main",
             "incomplete_safe", "incomplete_exception")
    catalog = [{"id": f"test_pool.py::test_{name}", "number": n} for n, name in enumerate(names)]
    tests = tmp_path / "tests.json"
    tests.write_text(json.dumps({"tests": catalog}), encoding="utf-8")
    output = tmp_path / "comparisons.json.gz"
    repo = Path(__file__).resolve().parents[1]
    recorded = subprocess.run(
        [sys.executable, "-B", str(repo / "tools" / "record_comparisons.py"),
         str(tests), str(output), "test_pool.py"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=40,
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    index = ComparisonIndex.load(output, catalog)
    chosen = index.narrow(FILE, before, before.replace("<=", "<"), set(range(len(names))))
    assert chosen == {1, 2, 3, 5, 6}


def test_literal_replacement_propagates_through_a_tuple(tmp_path):
    before = "def test(value):\n    return value < ('z', 2)\n"
    after = before.replace("('z', 2)", "('z', 3)")
    index = index_for(tmp_path, before, after, [
        [pair(("z", 1), ("z", 2)), [0]],
        [pair(("z", 2), ("z", 2)), [1]],
        [pair(("a", 8), ("z", 2)), [2]],
        [pair(("z", "a"), ("z", 2)), [3]],
    ])
    assert index.narrow(FILE, before, after, {0, 1, 2, 3, 4}) == {1, 3, 4}


def test_equality_checks_exact_builtin_and_default_dispatch(tmp_path):
    class DifferentEquality:
        def __eq__(self, other):
            return True

    class IntegerSubclass(int):
        pass

    before = "def test(value):\n    return value is None\n"
    after = before.replace("is None", "== None")
    assert operand_fact(DifferentEquality()) is None
    assert operand_fact(IntegerSubclass(1)) is None
    index = index_for(tmp_path, before, after, [
        [pair(0, None), [0]],
        [pair(None, None), [1]],
        [pair(object(), None), [2]],
        [pair(DifferentEquality(), None), [3]],
    ])
    assert index.narrow(FILE, before, after, set(range(5))) == {3, 4}


def test_identity_does_not_assume_string_or_integer_interning(tmp_path):
    before = "def test(a, b):\n    return a == b\n"
    after = before.replace("==", "is")
    a = "some text beyond one interned name"
    index = index_for(tmp_path, before, after, [
        [pair(a, a), [0]],
        [pair(a, "different"), [1]],
        [pair(1, 1.0), [2]],
        [pair(True, True), [3]],
    ])
    assert index.narrow(FILE, before, after, set(range(5))) == {0, 2, 4}


def test_same_class_string_enum_identity_and_equality(tmp_path):
    class Mode(str, Enum):
        FIRST = "first"
        SECOND = "second"
        ALIAS = "second"

    class ForeignMode(str, Enum):
        FIRST = "first"

    before = "def test(mode):\n    return mode is Mode.FIRST\n"
    after = before.replace("is Mode", "== Mode")
    index = index_for(tmp_path, before, after, [
        [pair(Mode.FIRST, Mode.FIRST), [0]],
        [pair(Mode.SECOND, Mode.FIRST), [1]],
        [pair(Mode.ALIAS, Mode.FIRST), [2]],
        [pair(ForeignMode.FIRST, Mode.FIRST), [3]],
        [pair("first", Mode.FIRST), [4]],
    ])
    assert index.narrow(FILE, before, after, set(range(5))) == {3, 4}


@pytest.mark.parametrize(("before", "after"), [
    ("x < 1 < y", "x <= 1 < y"),
    ("x == a", "x == b"),
    ("(x < 1, y < 1)", "(x <= 1, y <= 1)"),
    ("(x < 1, True)", "(x <= 1, 1)"),
    ("x == 1", "x is 1"),
    ("x == 'a'", "x in 'a'"),
    ('f"{x < 1=}"', 'f"{x <= 1=}"'),
])
def test_unhandled_syntax_falls_back(before, after):
    assert comparison_edit(before, after) is None


def instrumented(tmp_path, source):
    path = tmp_path / "comparison.py"
    path.write_text(source, encoding="utf-8")
    recorder = Recorder(tmp_path, ())
    scope = {FIRST: recorder.left, SECOND: recorder.right, SINGLETON: recorder.singleton}
    exec(recorder.source_code(path), scope)
    return recorder, scope["check"]


def test_trace_preserves_custom_dispatch_order_and_result(tmp_path):
    recorder, check = instrumented(tmp_path, "def check(lhs, rhs):\n    return lhs() == rhs()\n")
    recorder.current = {}
    events = []

    class Left:
        def __eq__(self, other):
            events.append(other)
            return self

    left = Left()

    def lhs():
        events.append("left")
        return left

    def rhs():
        events.append("right")
        return 2

    assert check(lhs, rhs) is left
    assert events == ["left", "right", 2]
    assert list(recorder.current.values()) == [None]


def test_trace_classifies_the_left_operand_after_rhs_mutations(tmp_path):
    recorder, check = instrumented(tmp_path, "def check(lhs, rhs):\n    return lhs == rhs()\n")
    recorder.current = {}

    class Left:
        pass

    def rhs():
        Left.__eq__ = lambda self, other: True
        return None

    assert check(Left(), rhs) is True
    assert list(recorder.current.values()) == [None]


def test_trace_pairs_reentrant_operands_even_if_nested_rhs_raises(tmp_path):
    recorder, check = instrumented(tmp_path, "def check(lhs, rhs):\n    return lhs < rhs()\n")
    recorder.current = {}

    def fail():
        raise ValueError

    def rhs():
        with pytest.raises(ValueError):
            check(100, fail)
        return 10

    assert check(1, rhs) is True
    assert list(recorder.current.values()) == [{pair(1, 10)}]


def test_export_omits_outside_call_sites_workers_and_unpassed_tests(tmp_path):
    source = "def check(value):\n    return value is None and value is False\n"
    recorder, check = instrumented(tmp_path, source)
    check(0)  # first comparison runs outside the call phase
    comparison_sites = list(recorder.sites)
    assert recorder.blocked == {0}
    workers, failed, passed = 0, 1, 2
    recorder.catalog = {f"test_{i}": i for i in range(3)}
    recorder.external.add(f"test_{workers}")
    recorder.stages = {f"test_{i}": {"setup", "call", "teardown"} for i in (workers, passed)}
    for nodeid in recorder.catalog:
        recorder.current = {}
        check(None)
        recorder.results[nodeid] = recorder.current
    data = recorder.export()
    assert data["files"]["comparison.py"] == [[comparison_sites[1][1], [(pair(None, False), [passed])]]]
