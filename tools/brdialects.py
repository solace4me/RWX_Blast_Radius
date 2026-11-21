"""pytest plugin: which dialect modules (incl. inherited ones) each test uses at run time."""
import json, os, pytest

OUT = os.environ.get("BR_OUT", "/tmp/brd")
FAMS = ("dialects", "parsers", "generators", "typing", "tokenizers")
_current = [None]
_used = {}


def _names_of(klass):
    names = set()
    try:
        classes = [klass]
        for attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
            c = getattr(klass, attr, None)
            if isinstance(c, type):
                classes.append(c)
        for c in classes:
            for base in c.__mro__:
                parts = base.__module__.split(".")
                if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMS:
                    names.add(parts[2])
    except Exception:
        pass
    return names


def _record(klass):
    t = _current[0]
    if t is None or not isinstance(klass, type):
        return
    _used.setdefault(t, set()).update(_names_of(klass))


def pytest_sessionstart(session):
    os.makedirs(OUT, exist_ok=True)
    from sqlglot.dialects import dialect as d

    meta = d._Dialect
    raw_getitem, raw_get = meta.__dict__["__getitem__"], meta.__dict__["get"]

    def getitem(cls, *a, **k):
        r = raw_getitem.__get__(cls, type(cls))(*a, **k)
        _record(r)
        return r

    def get(cls, *a, **k):
        r = raw_get.__get__(cls, type(cls))(*a, **k)
        _record(r)
        return r

    meta.__getitem__ = getitem
    meta.get = get
    orig_init = d.Dialect.__init__

    def init(self, *a, **k):
        _record(type(self))
        return orig_init(self, *a, **k)

    d.Dialect.__init__ = init


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    path = os.path.join(OUT, f"used_{os.getpid()}.json")
    with open(path, "w") as f:
        json.dump({k: sorted(v) for k, v in _used.items()}, f)
