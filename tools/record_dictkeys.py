"""Pytest plugin + merger: which tests read which key of each literal dict
setting in sqlglot's dialect classes (nested classes included).

While a sqlglot dialect-family module is imported, every class-level
`UPPER_NAME = {"literal": immutable, ...}` is wrapped (via an AST rewrite of
that module) in KeyTrackingDict, *before* any code can read it. Reads:
  - d[k], d.get(k)                   -> a read of key k's value
  - k in d, len(d), truthiness        -> ignored: they depend only on which keys
                                         exist, which a value-only edit can't change
  - anything else (iteration, copy, ==, |, pickling, mutation) -> "whole"
A value read outside a test's call/setup/teardown (import, class creation,
collection, another thread) blocks that key; a whole read there blocks the
setting.
"""
import ast
import builtins
import concurrent.futures as _cf
import importlib.machinery
import json
import os
import sys
import threading

import pytest

FAMILIES = ("dialects", "parsers", "generators", "tokenizers")
STATE = {"ctx": None, "thread": threading.get_ident(), "importing": 0}
READS = {}      # ident -> {"keys": {k: set(ctx)}, "whole": set(ctx)}
BLOCKED = set()
PASSED, FAILED = {}, set()


def _note(ident, key):
    ctx = STATE["ctx"]
    # sqlglot loads dialects lazily: a module imported during a test runs its
    # class bodies and metaclasses then, and what they derive is shared by every
    # later test. Such reads count as import-time, whoever triggered the import.
    if ctx is None or STATE["importing"] or threading.get_ident() != STATE["thread"]:
        BLOCKED.add((ident, "*" if key is _WHOLE else repr(key)))
        return
    entry = READS.setdefault(ident, {"keys": {}, "whole": set()})
    if key is _WHOLE:
        entry["whole"].add(ctx)
    else:
        entry["keys"].setdefault(key, set()).add(ctx)


_WHOLE = object()


class KeyTrackingDict(dict):
    __slots__ = ("_br_ident",)

    def __getitem__(self, k):
        _note(self._br_ident, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _note(self._br_ident, k)
        return dict.get(self, k, default)



def _whole(name):
    base = getattr(dict, name)

    def method(self, *a, **k):
        _note(self._br_ident, _WHOLE)
        return base(self, *a, **k)

    method.__name__ = name
    return method


for _name in ("__iter__", "keys", "values", "items", "copy", "__eq__", "__ne__", "__or__",
              "__ror__", "__ior__", "__repr__", "__reversed__", "__reduce__", "__reduce_ex__",
              "__setitem__", "__delitem__", "pop", "popitem", "setdefault", "update", "clear",
              "__getstate__", "__sizeof__", "__format__", "__str__"):
    setattr(KeyTrackingDict, _name, _whole(_name))
KeyTrackingDict.__hash__ = None


def _br_track(value, ident):
    d = KeyTrackingDict(value)
    d._br_ident = ident
    return d


def _immutable(v):
    return type(v) in (bool, int, str, float, type(None)) or (
        type(v) is tuple and all(_immutable(x) for x in v))


def literal_dict(node):
    if not isinstance(node, ast.Dict) or not node.keys or None in node.keys:
        return False
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return False
    return (type(value) is dict and len(value) == len(node.keys)
            and all(type(k) is str and _immutable(v) for k, v in value.items()))


class Wrap(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel, self.stack = rel, []

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        for i, st in enumerate(node.body):
            if isinstance(st, ast.ClassDef):
                self.visit_ClassDef(st)
            elif (isinstance(st, ast.Assign) and len(st.targets) == 1
                  and isinstance(st.targets[0], ast.Name) and st.targets[0].id.isupper()
                  and literal_dict(st.value)):
                ident = f"{self.rel}:{'.'.join(self.stack)}:{st.targets[0].id}"
                st.value = ast.Call(func=ast.Name("_br_track", ast.Load()),
                                    args=[st.value, ast.Constant(ident)], keywords=[])
        self.stack.pop()
        return node

    def visit_FunctionDef(self, node):  # don't descend into functions
        return node


def install(root):
    builtins._br_track = _br_track
    loader = importlib.machinery.SourceFileLoader
    get_code = loader.get_code

    def traced_get_code(self, fullname):
        parts = fullname.split(".")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILIES:
            path = self.get_filename(fullname)
            src = open(path, encoding="utf-8").read()
            tree = Wrap(os.path.relpath(path, root)).visit(ast.parse(src))
            ast.fix_missing_locations(tree)
            return compile(tree, path, "exec", dont_inherit=True)
        return get_code(self, fullname)

    loader.get_code = traced_get_code
    exec_module = loader.exec_module

    def traced_exec_module(self, module):
        sqlglot = module.__name__.split(".")[0] == "sqlglot"
        STATE["importing"] += sqlglot
        try:
            return exec_module(self, module)
        finally:
            STATE["importing"] -= sqlglot

    loader.exec_module = traced_exec_module


class _SyncExecutor:
    """Run pool work in-process so reads are attributed to the test."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        import pickle
        f = _cf.Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


_cf.ProcessPoolExecutor = _SyncExecutor


def pytest_configure(config):
    install(os.getcwd())


def _class_scope(nodeid):
    parts = nodeid.split("::")
    return "scope:" + ("::".join(parts[:-1]) if len(parts) > 2 else parts[0])


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    STATE["ctx"] = _class_scope(item.nodeid)
    yield
    STATE["ctx"] = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    STATE["ctx"] = item.nodeid
    yield
    STATE["ctx"] = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    STATE["ctx"] = _class_scope(item.nodeid)
    yield
    STATE["ctx"] = None


def pytest_runtest_logreport(report):
    if report.passed:
        PASSED.setdefault(report.nodeid, set()).add(report.when)
    elif not report.skipped:
        FAILED.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ["BR_DICT_OUT"]
    os.makedirs(out, exist_ok=True)
    passed = sorted(n for n, w in PASSED.items() if w == {"setup", "call", "teardown"} and n not in FAILED)
    reads = {i: {"keys": {repr(k): sorted(v) for k, v in e["keys"].items()}, "whole": sorted(e["whole"])}
             for i, e in READS.items()}
    with open(os.path.join(out, f"{os.getpid()}.json"), "w") as f:
        json.dump({"passed": passed, "blocked": sorted(map(list, BLOCKED)), "reads": reads}, f)


def merge(raw_dir, catalog_file, src_root, output):
    """Combine per-process recordings into dict_keys.json.gz."""
    import glob, gzip, hashlib
    catalog = [t["id"] for t in json.load(open(catalog_file))["tests"]]
    ids = set(catalog)

    def expand(ctx):
        if ctx in ids:
            return {ctx}
        if ctx.startswith("scope:"):
            prefix = ctx[len("scope:"):]
            return {t for t in catalog if t.startswith(prefix + "::")}
        return set()

    passed, blocked, reads = set(), {}, {}
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.json"))):
        data = json.load(open(path))
        passed.update(data["passed"])
        for ident, key in data["blocked"]:
            blocked.setdefault(ident, set()).add(key)
        for ident, e in data["reads"].items():
            r = reads.setdefault(ident, {"keys": {}, "whole": set()})
            for ctx in e["whole"]:
                r["whole"] |= expand(ctx)
            for k, ctxs in e["keys"].items():
                for ctx in ctxs:
                    r["keys"].setdefault(k, set()).update(expand(ctx))
    verified = passed & ids
    if len(verified) < len(ids) * 0.75:
        raise SystemExit("recording looks incomplete")
    files = {i.split(":", 1)[0] for i in set(reads) | set(blocked)}
    source = {f: hashlib.sha256(open(os.path.join(src_root, f), "rb").read()).hexdigest() for f in files}
    settings = {}
    for ident in set(reads) | set(blocked):
        r = reads.get(ident, {"keys": {}, "whole": set()})
        settings[ident] = {"keys": {k: sorted(v) for k, v in r["keys"].items()},
                           "whole": sorted(r["whole"]), "blocked": sorted(blocked.get(ident, ()))}
    out = {"version": 1, "catalog": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
           "verified": sorted(verified), "source": source, "settings": settings}
    with gzip.open(output, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
    print(len(verified), "verified;", len(settings), "settings")


if __name__ == "__main__":
    merge(*sys.argv[1:])
