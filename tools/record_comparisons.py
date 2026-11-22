"""Record operands in the pristine pinned SQLGlot test suite.

Run in the pinned checkout with the locked dependencies. Add the kit root
to PYTHONPATH, set SKIP_INTEGRATION=0, and pass the test catalog and destination:

    python -B "$KIT/tools/record_comparisons.py" "$KIT/sample/tests.json" \
        "$KIT/comparison_reads.json.gz" tests -k 'not test_max_nodes and not test_generator_memory'

Excluded, failed, skipped, and incompletely observed tests are absent from the recording.
The trace touches only original imports and tests and takes no patch inputs.
"""

import argparse
import ast
import builtins
from concurrent.futures import ProcessPoolExecutor
from enum import EnumType
import gzip
import importlib.abc
import importlib.machinery
import json
from pathlib import Path
import sys
import tempfile
import threading
import weakref

import pytest

from compare_index import (
    OPERATORS,
    VALUE_ENUMS,
    builtin_fact,
    compare_site,
    identity_fact,
    operand_fact,
    source_digest,
)
from setting_index import catalog_digest
from tools.comparison_pool import WorkerRecordings


FIRST = "_br_comparison_left"
SECOND = "_br_comparison_right"
SINGLETON = "_br_comparison_singleton"
MAX_CASES = 32
PROCESS_EVENTS = {"os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "subprocess.Popen"}


def tap(name, node, site, *extra):
    return ast.copy_location(ast.Call(
        func=ast.Name(id=name, ctx=ast.Load()),
        args=[ast.Constant(site), node, *extra],
        keywords=[],
    ), node)


class Instrument(ast.NodeTransformer):
    def __init__(self, locate):
        self.locate = locate
        self.in_function = False

    def visit_body(self, node, in_function):
        old, self.in_function = self.in_function, in_function
        node.body = [self.visit(statement) for statement in node.body]
        self.in_function = old
        return node

    def visit_FunctionDef(self, node):
        return self.visit_body(node, True)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        return self.visit_body(node, False)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not self.in_function or len(node.ops) != 1 or type(node.ops[0]) not in OPERATORS:
            return node
        site = self.locate(compare_site(node))
        right = node.comparators[0]
        if isinstance(right, ast.Constant) and (right.value is None or type(right.value) is bool):
            node.left = tap(SINGLETON, node.left, site, ast.Constant(right.value))
        else:
            # Keep the real comparison in the application frame. In particular,
            # opaque objects still use Python's original rich comparison dispatch.
            node.left = tap(FIRST, node.left, site)
            node.comparators[0] = tap(SECOND, right, site)
        return node


class Recorder:
    def __init__(self, root, catalog, workers=None):
        self.root = root
        self.catalog = {nodeid: index for index, nodeid in enumerate(sorted(catalog))}
        self.sites = {}
        self.source = {}
        self.main_thread = threading.get_ident()
        self.import_depth = 0
        self.running = None
        self.current = None
        self.pending = {}
        self.blocked = set()
        self.external = set()
        self.stages = {}
        self.results = {}
        self.workers = workers

    def source_code(self, path):
        source = path.read_text("utf-8")
        file = path.relative_to(self.root).as_posix()
        digest = source_digest(source)
        if self.source.setdefault(file, digest) != digest:
            raise RuntimeError(f"source changed during recording: {file}")

        def locate(position):
            return self.sites.setdefault((file, position), len(self.sites))

        tree = Instrument(locate).visit(ast.parse(source, str(path)))
        return compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)

    def recording(self, site):
        if self.current is None or self.import_depth or threading.get_ident() != self.main_thread:
            self.blocked.add(site)
            return False
        return site not in self.blocked and self.current.get(site, ()) is not None

    def save(self, site, left, right, same, same_type):
        if left is None or right is None:
            self.current[site] = None
            return
        cases = self.current.setdefault(site, set())
        cases.add((left, right, same, same_type))
        if len(cases) > MAX_CASES:
            self.current[site] = None

    def singleton(self, site, left, right):
        if self.recording(site):
            self.save(site, operand_fact(left), builtin_fact(right), left is right, type(left) is type(right))
        return left

    def left(self, site, value):
        if self.recording(site):
            fact = builtin_fact(value)
            live = None
            if fact is None:
                if type(value) is object:
                    fact = ("identity",)
                elif type(type(value)) is EnumType and issubclass(type(value), (int, str)):
                    # Canonical enum members already live in their class's member
                    # map. Recheck methods and membership after the right operand.
                    fact = identity_fact(value)
                    if fact is not None and fact[0] in VALUE_ENUMS:
                        live = value
                else:
                    try:
                        live = weakref.ref(value)
                    except TypeError:
                        pass
                if fact is None and live is None:
                    self.current[site] = None
                    return value
            key = (id(sys._getframe(1)), site)
            self.pending[key] = (fact, id(value), id(type(value)), live)
        return value

    def right(self, site, value):
        if self.recording(site):
            key = (id(sys._getframe(1)), site)
            previous = self.pending.pop(key, None)
            if previous is None:
                self.current[site] = None
            else:
                fact, identity, type_id, live = previous
                if live is not None:
                    left = live() if type(live) is weakref.ReferenceType else live
                    fact = operand_fact(left) if left is not None else None
                    type_id = id(type(left))
                self.save(site, fact, operand_fact(value), identity == id(value), type_id == id(type(value)))
        return value

    def install(self):
        if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
            raise RuntimeError("install the recorder before importing SQLGlot")
        recorder = self

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, fullname):
                return recorder.source_code(Path(self.path))

            def exec_module(self, module):
                recorder.import_depth += 1
                try:
                    super().exec_module(module)
                finally:
                    recorder.import_depth -= 1

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                    return None
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                    if Path(spec.origin).is_relative_to(recorder.root / "sqlglot"):
                        spec.loader = Loader(fullname, spec.origin)
                return spec

        hooks = {FIRST: self.left, SECOND: self.right, SINGLETON: self.singleton}
        for name, hook in hooks.items():
            if hasattr(builtins, name):
                raise RuntimeError(f"instrumentation name already in use: {name}")
            setattr(builtins, name, hook)
        sys.meta_path.insert(0, Finder())
        sys.addaudithook(self.audit)
        submit = ProcessPoolExecutor.submit
        start = threading.Thread.start

        def tracked_submit(pool, fn, /, *args, **kwargs):
            if self.workers is not None:
                return self.workers.submit(self, submit, pool, fn, args, kwargs)
            self.mark_external()
            return submit(pool, fn, *args, **kwargs)

        def tracked_start(thread, *args, **kwargs):
            if self.workers is None or not self.workers.is_pool_thread(thread):
                self.mark_external()
            return start(thread, *args, **kwargs)

        ProcessPoolExecutor.submit = tracked_submit
        threading.Thread.start = tracked_start

    def mark_external(self):
        if self.running is not None:
            self.external.add(self.running)

    def audit(self, event, args):
        if event in PROCESS_EVENTS:
            self.mark_external()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.running = item.nodeid
        try:
            yield
        finally:
            self.running = None

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.current = {}
        try:
            yield
        finally:
            if item.nodeid in self.catalog:
                self.results[item.nodeid] = self.current
            self.current = None
            self.pending.clear()

    def pytest_runtest_logreport(self, report):
        if report.passed and not hasattr(report, "wasxfail"):
            self.stages.setdefault(report.nodeid, set()).add(report.when)

    def worker_report(self):
        # A lazy import need not assign the same numbers in different workers.
        blocked = {key for key, number in self.sites.items() if number in self.blocked}
        cases = {key: self.current[number] for key, number in self.sites.items() if number in self.current}
        sources = {key[0]: self.source[key[0]] for key in blocked | cases.keys()}
        return sources, blocked, cases

    def merge_worker_report(self, nodeid, report):
        sources, blocked, cases = report
        for file, digest in sources.items():
            if self.source.setdefault(file, digest) != digest:
                raise RuntimeError(f"source changed during worker recording: {file}")
        for key in blocked:
            self.blocked.add(self.sites.setdefault(key, len(self.sites)))
        result = self.results.get(nodeid)
        if result is None:
            return
        for key, worker_cases in cases.items():
            number = self.sites.setdefault(key, len(self.sites))
            main_cases = result.get(number, set())
            if worker_cases is None or main_cases is None:
                result[number] = None
            else:
                combined = main_cases | worker_cases
                result[number] = combined if len(combined) <= MAX_CASES else None

    def export(self):
        if self.workers is not None:
            self.workers.merge(self)
        grouped = {}
        count = 0
        for nodeid, observations in sorted(self.results.items()):
            if nodeid in self.external or self.stages.get(nodeid) != {"setup", "call", "teardown"}:
                continue
            count += 1
            ordinal = self.catalog[nodeid]
            for site, cases in observations.items():
                if site in self.blocked or not cases:
                    continue
                facts = grouped.setdefault(site, {})
                for case in cases:
                    facts.setdefault(case, []).append(ordinal)
        files = {}
        for (file, pos), site in sorted(self.sites.items()):
            if site in grouped:
                groups = grouped[site]
                files.setdefault(file, []).append([pos, sorted(groups.items(), key=repr)])
        print(f"Recorded {count} eligible tests and {len(grouped)} comparison sites in {len(files)} files")
        return {"version": 1, "catalog": catalog_digest(self.catalog),
                "source": {file: self.source[file] for file in files}, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text("utf-8"))["tests"]
    tests = {test["id"] for test in catalog}
    with tempfile.TemporaryDirectory(prefix="comparison-workers-") as trace:
        recorder = Recorder(Path.cwd(), tests, WorkerRecordings(trace))
        recorder.install()
        result = pytest.main(["-q", "-p", "no:cacheprovider", *args.pytest_args], plugins=[recorder])
        if result != pytest.ExitCode.OK:
            return result
        serialized = json.dumps(recorder.export(), separators=(",", ":")).encode("utf-8")
    args.output.write_bytes(gzip.compress(serialized, mtime=0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
