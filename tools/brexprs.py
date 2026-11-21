"""pytest plugin: which sqlglot classes (by MRO, simple name) each test instantiates
(expressions, tokenizers, parsers, generators, dialects...)."""
import json, os, pytest

import concurrent.futures as _cf

class _SyncExecutor:
    """Runs submitted work in-process so coverage sees it under the test's context."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            import pickle
            initializer(*pickle.loads(pickle.dumps(initargs)))
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

OUT = os.environ.get("BR_OUT", "/tmp/bre")
_current = [None]
_used = {}
_mro_cache = {}


def _names(cls):
    r = _mro_cache.get(cls)
    if r is None:
        r = _mro_cache[cls] = tuple(
            b.__qualname__ for b in cls.__mro__ if b.__module__.startswith("sqlglot")
        )
    return r


def pytest_sessionstart(session):
    os.makedirs(OUT, exist_ok=True)
    import sqlglot
    from sqlglot.expressions import core
    from sqlglot import tokens, parser, generator
    from sqlglot.dialects import dialect

    targets = [c for c in (getattr(core, "Expr", None), core.Expression) if c is not None] + [tokens.Tokenizer,
               parser.Parser, generator.Generator, dialect.Dialect]
    for base in targets:
        orig = base.__init__

        def make(orig):
            def init(self, *a, **k):
                t = _current[0]
                if t is not None:
                    s = _used.get(t)
                    if s is None:
                        s = _used[t] = set()
                    s.update(_names(type(self)))
                return orig(self, *a, **k)
            return init

        if "__init__" in base.__dict__:
            base.__init__ = make(orig)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    _current[0] = "collect:" + collector.nodeid if collector.nodeid else None
    yield
    _current[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _current[0] = item.nodeid + "|setup"
    yield
    _current[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _current[0] = item.nodeid
    yield
    _current[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _current[0] = item.nodeid + "|teardown"
    yield
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    with open(os.path.join(OUT, f"used_{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _used.items()}, f)
