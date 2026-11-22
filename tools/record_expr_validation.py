"""Pytest plugin and merger for expression argument requirements on pinned SQLGlot."""

import ast
import builtins
import concurrent.futures
from contextlib import contextmanager
import gzip
from hashlib import sha256
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import threading
from types import MappingProxyType, MemberDescriptorType

import pytest

from expr_validation_index import arg_type_declarations
from setting_index import catalog_digest


CORE = "sqlglot/expressions/core.py"
METADATA = {"arg_types", "required_args", "__dict__"}
DERIVATION = ast.dump(ast.parse(
    "cls.required_args = {k for k, v in cls.arg_types.items() if v}"
).body[0])
KEY_ONLY = {
    CORE: {"len(self.arg_types)", "tuple(cls.arg_types)", "zip(args, cls.arg_types)"},
    "sqlglot/optimizer/merge_subqueries.py": {"set(exp.Select.arg_types)"},
    "sqlglot/parsers/postgres.py": {"zip(exp.MakeInterval.arg_types, args)"},
}


def wrap(value, name, *args):
    return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), [value, *args], []), value)


class Instrument(ast.NodeTransformer):
    def __init__(self, file, tree, declarations):
        self.file = file
        self.parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.declarations = {node: name for name, node in declarations.items()}
        self.derivations = self.validations = 0

    def in_method(self, node, cls, name):
        parent = self.parents[node]
        return (isinstance(parent, ast.FunctionDef) and parent.name == name
                and isinstance(self.parents[parent], ast.ClassDef) and self.parents[parent].name == cls)

    def visit_Assign(self, node):
        if (self.file == CORE and self.in_method(node, "Expr", "__init_subclass__")
                and ast.dump(node) == DERIVATION):
            # Same comprehension and set type, while keeping the origin of its flags.
            metadata = ast.Attribute(ast.Name("cls", ast.Load()), "arg_types", ast.Load())
            node.value = ast.copy_location(wrap(metadata, "_br_expr_derive"), node.value)
            self.derivations += 1
            return node
        return self.generic_visit(node)

    def visit_Dict(self, node):
        if node in self.declarations:
            return wrap(node, "_br_expr_define", ast.Constant((self.file, self.declarations[node])))
        return self.generic_visit(node)

    def visit_Attribute(self, node):
        if not isinstance(node.ctx, ast.Load) or node.attr not in METADATA:
            return self.generic_visit(node)
        # Inspect the parent before rewriting any of its children.
        parent = self.parents[node]
        shape = node.attr == "arg_types" and (
            (isinstance(parent, ast.Compare) and parent.comparators == [node]
             and isinstance(parent.ops[0], (ast.In, ast.NotIn)))
            or (isinstance(parent, ast.Call) and ast.unparse(parent) in KEY_ONLY.get(self.file, ()))
        )
        self.generic_visit(node)
        if shape:
            return node
        if (self.file == CORE and node.attr == "required_args" and isinstance(node.value, ast.Name)
                and node.value.id == "self" and isinstance(parent, ast.For) and parent.iter is node
                and self.in_method(parent, "Expression", "error_messages")):
            self.validations += 1
            return wrap(node, "_br_expr_validate", ast.Name("self", ast.Load()))
        return wrap(node, "_br_expr_read")

    def visit_Call(self, node):
        self.generic_visit(node)
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in {"getattr", "__getattribute__", "vars"}:
            return wrap(node, "_br_expr_read")
        return node


class Recorder:
    def __init__(self, root, catalog):
        self.root, self.catalog = root, catalog
        self.test = None
        self.importing = 0
        self.thread = threading.get_ident()
        self.metadata, self.required = {}, {}
        self.source, self.cache_sites, self.layouts = {}, {}, {}
        self.reads, self.blocked = {}, {}
        self.stages, self.failed, self.external = {}, set(), set()

    def namespace(self):
        return {"_br_expr_define": self.define, "_br_expr_derive": self.derive,
                "_br_expr_validate": self.validate, "_br_expr_read": self.read}

    @contextmanager
    def in_test(self, nodeid):
        previous, self.test = self.test, nodeid
        try:
            yield
        finally:
            self.test = previous

    def compile(self, file, source, filename):
        tree = ast.parse(source, filename)
        self.cache_sites[filename] = {
            min(decorator.lineno for decorator in node.decorator_list)
            for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator).rsplit(".", 1)[-1]
            in {"cache", "cached_property", "lru_cache"}
        }
        declarations = {}
        if file.startswith("sqlglot/expressions/"):
            declarations = arg_type_declarations(tree)
            self.source[file] = sha256(source.encode("utf-8")).hexdigest()
        instrument = Instrument(file, tree, declarations)
        instrument.visit(tree)
        if file == CORE and (instrument.derivations, instrument.validations) != (1, 1):
            raise ValueError("SQLGlot no longer has the expected requirement derivation and validation")
        return compile(ast.fix_missing_locations(tree), filename, "exec", dont_inherit=True)

    def define(self, value, identity):
        self.metadata[id(value)] = (value, identity, value.copy())
        return value

    def derive(self, metadata):
        required = {k for k, v in metadata.items() if v}
        origin = self.metadata.get(id(metadata))
        if origin is not None:
            _, identity, flags = origin
            self.required[id(required)] = (required, identity, frozenset(required), flags)
        return required

    def shared(self, frame):
        if self.test is None or self.importing or threading.get_ident() != self.thread:
            return True
        while frame is not None:
            code = frame.f_code
            if code.co_firstlineno in self.cache_sites.get(code.co_filename, ()):
                return True
            frame = frame.f_back
        return False

    def note(self, identity, keys, frame):
        excluded = self.blocked.get(identity, ())
        if None in excluded:
            return
        relevant = [key for key in keys if key not in excluded]
        if not relevant:
            return
        if self.shared(frame):
            self.blocked.setdefault(identity, set()).update(relevant)
            return
        readers = self.reads.setdefault(identity, {})
        for key in relevant:
            readers.setdefault(key, set()).add(self.test)

    def read(self, value):
        # Direct metadata uses, dynamic getattr calls and class namespace access.
        if type(value) in (dict, set):
            origin = self.metadata.get(id(value)) or self.required.get(id(value))
            if origin is not None:
                self.note(origin[1], (None,), sys._getframe(1))
        if type(value) in (dict, MappingProxyType):
            for name in ("arg_types", "required_args"):
                member = value.get(name)
                origin = self.metadata.get(id(member)) or self.required.get(id(member))
                if origin is not None:
                    self.note(origin[1], (None,), sys._getframe(1))
        return value

    def plain_args(self, instance):
        cls = type(instance)
        if cls not in self.layouts:
            descriptor = next((base.__dict__["args"] for base in cls.__mro__ if "args" in base.__dict__), None)
            self.layouts[cls] = (cls.__getattribute__ is object.__getattribute__
                                 and (descriptor is None or type(descriptor) is MemberDescriptorType))
        if self.layouts[cls]:
            try:
                args = object.__getattribute__(instance, "args")
            except AttributeError:
                return None
            if type(args) is dict and all(type(key) is str for key in args):
                return args
        return None

    def validate(self, required, instance):
        origin = self.required.get(id(required))
        if origin is None or None in self.blocked.get(origin[1], ()):
            return required
        _, identity, _, flags = origin
        args = self.plain_args(instance)
        if args is None:
            self.note(identity, (None,), sys._getframe(1))
            return required
        # A supplied argument cannot change its missing-argument error. If
        # multiple other errors are possible, the set iteration order may change.
        missing = []
        for key in flags:
            value = args.get(key)
            if value is None or (type(value) is list and not value) or (
                type(value) is not list and issubclass(type(value), list)
            ):
                missing.append(key)
        if sum(key in required for key in missing) > 1:
            missing = (None,)
        if missing:
            self.note(identity, missing, sys._getframe(1))
        return required

    def install(self):
        if "sqlglot" in sys.modules:
            raise ValueError("record before importing SQLGlot")
        for name, function in self.namespace().items():
            setattr(builtins, name, function)
        loader = importlib.machinery.SourceFileLoader
        get_code, exec_module = loader.get_code, loader.exec_module

        def traced_get_code(obj, fullname):
            if fullname.split(".", 1)[0] in {"sqlglot", "tests"}:
                path = Path(obj.get_filename(fullname)).resolve()
                if path.is_relative_to(self.root):
                    return self.compile(path.relative_to(self.root).as_posix(), path.read_text("utf-8"), str(path))
            return get_code(obj, fullname)

        def traced_exec_module(obj, module):
            stock = module.__name__.split(".", 1)[0] in {"sqlglot", "tests"}
            self.importing += stock
            try:
                return exec_module(obj, module)
            finally:
                self.importing -= stock

        def audit(event, args):
            if self.test is not None and event in {
                "subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.system",
            }:
                self.external.add(self.test)

        def outside(function):
            def wrapped(*args, **kwargs):
                if self.test is not None:
                    self.external.add(self.test)
                return function(*args, **kwargs)
            return wrapped

        loader.get_code, loader.exec_module = traced_get_code, traced_exec_module
        sys.addaudithook(audit)
        for cls in (concurrent.futures.ProcessPoolExecutor, concurrent.futures.ThreadPoolExecutor):
            cls.submit = outside(cls.submit)
        threading.Thread.start = outside(threading.Thread.start)

    def export(self):
        for value, identity, snapshot, *_ in (*self.metadata.values(), *self.required.values()):
            if value != snapshot:
                self.blocked.setdefault(identity, set()).add(None)
        passed = {nodeid for nodeid, stages in self.stages.items()
                  if stages == {"setup", "call", "teardown"} and nodeid not in self.failed}
        definitions = {identity for _, identity, _ in self.metadata.values()}
        return {
            "passed": sorted(passed & self.catalog), "external": sorted(self.external & self.catalog),
            "source": self.source,
            "owners": [
                [identity, None in self.blocked.get(identity, ()),
                 sorted(key for key in self.blocked.get(identity, ()) if key is not None),
                 sorted(self.reads.get(identity, {}).get(None, set()) & self.catalog),
                 {key: sorted(tests & self.catalog) for key, tests in self.reads.get(identity, {}).items()
                  if key is not None}]
                for identity in sorted(definitions)
            ],
        }


RECORDER = None


def test_ids(catalog_file):
    return {test["id"] for test in json.loads(Path(catalog_file).read_text("utf-8"))["tests"]}


def pytest_sessionstart(session):
    global RECORDER
    if session.config.getoption("assertmode") != "plain":
        raise ValueError("run with --assert=plain so test modules use the tracing loader")
    RECORDER = Recorder(Path.cwd(), test_ids(os.environ["BR_EXPR_CATALOG"]))
    RECORDER.install()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    with RECORDER.in_test(item.nodeid):
        yield


def pytest_runtest_logreport(report):
    if report.passed:
        RECORDER.stages.setdefault(report.nodeid, set()).add(report.when)
    else:
        RECORDER.failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if not hasattr(session.config, "workerinput") and session.config.getoption("numprocesses", 0):
        return
    output = Path(os.environ["BR_EXPR_TRACE"]) / f"{os.getpid()}.json"
    output.write_text(json.dumps(RECORDER.export(), sort_keys=True), encoding="utf-8")


def merge(raw_dir, catalog_file, destination):
    catalog = test_ids(catalog_file)
    passed, external, excluded, blocked, reads, source = set(), set(), set(), {}, {}, {}
    for path in sorted(Path(raw_dir).glob("*.json")):
        trace = json.loads(path.read_text("utf-8"))
        for file, digest in trace["source"].items():
            if source.setdefault(file, digest) != digest:
                raise ValueError(f"source differs between traces for {file}")
        passed.update(trace["passed"])
        external.update(trace["external"])
        for identity, skip, shared, whole, keys in trace["owners"]:
            identity = tuple(identity)
            if skip:
                excluded.add(identity)
            blocked.setdefault(identity, set()).update(shared)
            readers, key_readers = reads.setdefault(identity, (set(), {}))
            readers.update(whole)
            for key, tests in keys.items():
                key_readers.setdefault(key, set()).update(tests)
    verified = passed - external
    if not catalog or len(verified) < len(catalog) * 0.75 or CORE not in source:
        raise ValueError("expected a recording from the pinned SQLGlot test suite")
    files = {}
    for identity in sorted(reads.keys() - excluded):
        file, owner = identity
        whole, keys = reads[identity]
        blocked_keys = blocked[identity]
        files.setdefault(file, {})[owner] = {
            "whole": sorted(whole),
            "keys": {key: None if key in blocked_keys else sorted(keys[key])
                     for key in sorted(keys.keys() | blocked_keys)},
        }
    index = {"version": 1, "catalog": catalog_digest(catalog), "files": files,
             "verified": sorted(verified), "source": source}
    payload = json.dumps(index, separators=(",", ":"), sort_keys=True).encode("utf-8")
    Path(destination).write_bytes(gzip.compress(payload, mtime=0))
    print(f"{len(verified)} verified tests; {len(reads.keys() - excluded)} owners; {len(excluded)} excluded")


if __name__ == "__main__":
    merge(*sys.argv[1:])
