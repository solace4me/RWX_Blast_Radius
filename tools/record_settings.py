"""Pytest plugin and merger for reads of literal settings on stock dialects."""

import ast
import concurrent.futures
from contextlib import contextmanager
import gzip
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from setting_index import catalog_digest, setting_declarations


def file_scope(nodeid):
    file = nodeid.split("::", 1)[0]
    return ("file", file) if file.endswith(".py") else None


def literal_key(node, parents):
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript) and parent.value is node and isinstance(parent.ctx, ast.Load):
        key = parent.slice
    elif isinstance(parent, ast.Attribute) and parent.attr == "get" and parent.value is node:
        call = parents.get(parent)
        if not isinstance(call, ast.Call) or call.func is not parent or not call.args or call.keywords:
            return None
        key = call.args[0]
    else:
        return None
    return key.value if isinstance(key, ast.Constant) and type(key.value) is str else None


class Recorder:
    def __init__(self, root, catalog):
        self.root = root
        self.prefix = str(root / "sqlglot") + os.sep
        self.catalog = catalog
        self.context = None
        self.depth = 0
        self.thread = threading.get_ident()
        self.readers = {}
        self.blocked = set()
        self.stages = {}
        self.failed = set()
        self.external = set()
        self.declarations = {}
        self.source = {}
        self.mro = {}
        self.positions = {}
        self.source_facts = {}
        self.by_file = {}
        for nodeid in catalog:
            self.by_file.setdefault(nodeid.split("::", 1)[0], set()).add(nodeid)
        for path in sorted((root / "sqlglot" / "dialects").glob("*.py")):
            if path.stem in {"__init__", "dialect"}:
                continue
            file = path.relative_to(root).as_posix()
            source = path.read_text("utf-8")
            self.source[file] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            for (name, attr), (_, value) in setting_declarations(ast.parse(source)).items():
                owner = (f"sqlglot.dialects.{path.stem}", name)
                self.declarations.setdefault(owner, {})[attr] = ((file, name, attr), type(value) is dict)
        self.names = {name for attrs in self.declarations.values() for name in attrs}

    @contextmanager
    def in_context(self, value):
        previous, self.context = self.context, value
        try:
            yield
        finally:
            self.context = previous

    def facts(self, filename):
        result = self.source_facts.get(filename)
        if result is not None:
            return result
        sites, caches = {}, set()
        try:
            path = Path(filename)
            if path.is_relative_to(self.root):
                tree = ast.parse(path.read_text("utf-8"))
                parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
                for node in parents:
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                        key = literal_key(node, parents)
                        if key is not None:
                            pos = (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)
                            sites[(pos, node.attr)] = key
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for dec in node.decorator_list:
                            item = dec.func if isinstance(dec, ast.Call) else dec
                            name = item.id if isinstance(item, ast.Name) else getattr(item, "attr", "")
                            if name in {"cache", "cached_property", "lru_cache"}:
                                caches.add(min(d.lineno for d in node.decorator_list))
        except (OSError, SyntaxError):
            pass
        self.source_facts[filename] = (sites, caches)
        return sites, caches

    def key_at(self, frame, name):
        code = frame.f_code
        sites, _ = self.facts(code.co_filename)
        if not sites:
            return None
        if code not in self.positions:
            self.positions[code] = tuple(code.co_positions())
        positions = self.positions[code]
        instr = frame.f_lasti // 2
        return sites.get((positions[instr], name)) if 0 <= instr < len(positions) else None

    def shared_cache(self, frame):
        while frame is not None:
            code = frame.f_code
            if code.co_filename.startswith(self.prefix) and code.co_firstlineno in self.facts(code.co_filename)[1]:
                return True
            frame = frame.f_back
        return False

    def owners(self, cls):
        identity = id(cls)
        if identity not in self.mro:
            raw = type.__getattribute__
            owners = []
            for base in raw(cls, "__mro__"):
                key = (raw(base, "__module__"), raw(base, "__qualname__"))
                if key in self.declarations:
                    owners.append(self.declarations[key])
            self.mro[identity] = (cls, owners)
        return self.mro[identity][1]

    def record(self, cls, name, value, frame):
        namespace = name == "__dict__"
        owners = self.owners(cls)
        if not owners:
            return
        if namespace:
            field = self.key_at(frame, name)
            relevant = [fact for attrs in owners for attr, fact in attrs.items()
                        if field is None or field == attr]
        else:
            relevant = [attrs[name] for attrs in owners if name in attrs]
        relevant = [fact for fact in relevant if fact[0] not in self.blocked]
        if not relevant:
            return
        if (self.context is None or self.depth or threading.get_ident() != self.thread
                or self.shared_cache(frame)):
            self.blocked.update(identity for identity, _ in relevant)
            return
        for identity, mapping in relevant:
            key = self.key_at(frame, name) if mapping and not namespace and type(value) is dict else None
            self.readers.setdefault(identity, {}).setdefault(key, set()).add(self.context)

    def install(self):
        loader = importlib.machinery.SourceFileLoader
        execute = loader.exec_module

        def traced_import(instance, module):
            name = module.__name__
            if not name.startswith("sqlglot"):
                return execute(instance, module)
            self.depth += 1
            try:
                execute(instance, module)
                if name == "sqlglot.dialects.dialect":
                    self.hook_dialects(module)
            finally:
                self.depth -= 1

        loader.exec_module = traced_import
        for (module, _), attrs in self.declarations.items():
            if module in sys.modules:
                self.blocked.update(identity for identity, _ in attrs.values())
        if "sqlglot.dialects.dialect" in sys.modules:
            self.hook_dialects(sys.modules["sqlglot.dialects.dialect"])

        def audit(event, args):
            if self.context and event in {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec"}:
                self.external.add(self.context)

        sys.addaudithook(audit)
        pool = concurrent.futures.ProcessPoolExecutor
        original_submit = pool.submit

        def submit(instance, *args, **kwargs):
            if self.context:
                self.external.add(self.context)
            return original_submit(instance, *args, **kwargs)

        pool.submit = submit

    def hook_dialects(self, module):
        dialect, meta = module.Dialect, module._Dialect
        original_instance = dialect.__getattribute__
        original_new = meta.__new__

        def get_class_attr(cls, name):
            value = type.__getattribute__(cls, name)
            if name in self.names or name == "__dict__":
                self.record(cls, name, value, sys._getframe(1))
            return value

        def get_instance_attr(instance, name):
            value = original_instance(instance, name)
            if name in self.names:
                self.record(type(instance), name, value, sys._getframe(1))
            return value

        def new(cls, *args, **kwargs):
            self.depth += 1
            try:
                return original_new(cls, *args, **kwargs)
            finally:
                self.depth -= 1

        meta.__getattribute__ = get_class_attr
        meta.__new__ = staticmethod(new)
        dialect.__getattribute__ = get_instance_attr

    def ids(self, scopes):
        tests = set()
        for kind, name in scopes:
            if kind == "file":
                tests.update(self.by_file.get(name, ()))
            elif name in self.catalog:
                tests.add(name)
        return sorted(tests)

    def export(self):
        passed = {nodeid for nodeid, stages in self.stages.items()
                  if stages == {"setup", "call", "teardown"} and nodeid not in self.failed}
        observed = []
        for identity, readers in self.readers.items():
            observed.append([identity, self.ids(readers.get(None, ())), {
                key: self.ids(scopes) for key, scopes in readers.items() if key is not None
            }])
        return {"passed": sorted(passed & self.catalog), "external": self.ids(self.external),
                "blocked": sorted(self.blocked), "reads": observed, "source": self.source}


RECORDER = None


def pytest_sessionstart(session):
    global RECORDER
    catalog = json.loads(Path(os.environ["BR_SETTINGS_CATALOG"]).read_text("utf-8"))["tests"]
    RECORDER = Recorder(Path.cwd(), {test["id"] for test in catalog})
    RECORDER.install()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    with RECORDER.in_context(file_scope(collector.nodeid)):
        yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    with RECORDER.in_context(file_scope(item.nodeid)):
        yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    with RECORDER.in_context(("test", item.nodeid)):
        yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    with RECORDER.in_context(file_scope(item.nodeid)):
        yield


def pytest_runtest_logreport(report):
    if report.passed:
        RECORDER.stages.setdefault(report.nodeid, set()).add(report.when)
    else:
        RECORDER.failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if not hasattr(session.config, "workerinput") and session.config.getoption("numprocesses", 0):
        return
    output = Path(os.environ["BR_SETTINGS_TRACE"]) / f"{os.getpid()}.json"
    output.write_text(json.dumps(RECORDER.export(), sort_keys=True), encoding="utf-8")


def merge(raw_dir, catalog_file, output):
    catalog = {t["id"] for t in json.loads(Path(catalog_file).read_text("utf-8"))["tests"]}
    passed, external, blocked, reads, source = set(), set(), set(), {}, {}
    for path in sorted(Path(raw_dir).glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        source.update(data["source"])
        passed.update(data["passed"])
        external.update(data["external"])
        blocked.update(map(tuple, data["blocked"]))
        for identity, whole, keys in data["reads"]:
            all_readers, key_readers = reads.setdefault(tuple(identity), (set(), {}))
            all_readers.update(whole)
            for key, ids in keys.items():
                key_readers.setdefault(key, set()).update(ids)
    verified = passed - external
    if not catalog or len(verified) < len(catalog) * 0.75:
        raise ValueError("expected a recording from the stock test suite")
    files = {}
    for identity in sorted(reads.keys() - blocked):
        file, cls, attr = identity
        whole, keys = reads[identity]
        files.setdefault(file, {}).setdefault(cls, {})[attr] = {
            "whole": sorted(whole),
            "keys": {key: sorted(ids) for key, ids in sorted(keys.items())},
        }
    index = {"version": 1, "catalog": catalog_digest(catalog), "files": files,
             "verified": sorted(verified), "source": source}
    payload = json.dumps(index, separators=(",", ":"), sort_keys=True).encode("utf-8")
    Path(output).write_bytes(gzip.compress(payload, mtime=0))
    print(f"{len(verified)} verified tests; {len(reads.keys() - blocked)} settings; {len(blocked)} blocked")


if __name__ == "__main__":
    merge(*sys.argv[1:])
