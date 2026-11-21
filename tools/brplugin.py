"""pytest plugin: per-test line coverage of sqlglot + files each test opens."""
import builtins, io, json, os, pytest, coverage

ROOT = os.path.abspath(os.environ.get("BR_ROOT", "."))
OUT = os.environ.get("BR_OUT", "/tmp/br")
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

_cov = coverage.Coverage(data_file=os.path.join(OUT, ".coverage"),
                         source=[os.path.join(ROOT, "sqlglot")], branch=False)
_current = [None]
_opened = {}
_orig_open = builtins.open

def _open(file, *a, **k):
    t = _current[0]
    if t is not None and isinstance(file, (str, bytes, os.PathLike)):
        try:
            p = os.path.abspath(os.fsdecode(file))
            if p.startswith(ROOT):
                _opened.setdefault(t, set()).add(os.path.relpath(p, ROOT))
        except Exception:
            pass
    return _orig_open(file, *a, **k)

def pytest_sessionstart(session):
    os.makedirs(OUT, exist_ok=True)
    builtins.open = _open
    io.open = _open
    _cov.start()

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    _cov.switch_context(item.nodeid)
    yield
    _cov.switch_context("")
    _current[0] = None

def pytest_sessionfinish(session, exitstatus):
    _cov.stop()
    _cov.save()
    builtins.open = _orig_open
    io.open = _orig_open
    with _orig_open(os.path.join(OUT, "opened.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opened.items()}, f)
