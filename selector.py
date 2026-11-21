"""Blast Radius selector, v1: coverage-based test selection.

map.json.gz is built once from sqlglot at the pinned commit (see tools/):
  lines    source file -> groups of [lines, tests that executed them]
  opened   fixture/data file -> tests that opened it
  bodies   source file -> line ranges inside function bodies
  mention  dialect name -> tests whose code or fixtures mention it
  children dialect name -> dialects built on top of it

For each patch:
  - fixture/data file under tests/  -> the tests that read it
  - a test .py file                 -> every test in that file
  - a line inside a function body   -> the tests that executed that line
  - a line that runs at import time -> every test touching the file, plus,
    for dialect files, every test mentioning that dialect or its children
  - anything we cannot map          -> all tests (never risk a 300 s miss)
Tests are then packed longest-first onto the cheapest number of workers.
"""

import ast
import bisect
import gzip
import io
import tarfile
import os
import subprocess
import tokenize
import json
import re
import shutil
import sys
import zlib
from pathlib import Path

from setting_index import SettingIndex, edited_setting
from tpcds_query_index import QueryIndex

DATA = Path("/data")
MAP_PATH = Path(__file__).with_name("map.json.gz")
SETTING_READS = Path(__file__).with_name("setting_reads.json.gz")
SRC_PATH = Path(__file__).with_name("sqlglot_src.tgz")
TPCDS_PROBE = Path(__file__).with_name("tpcds_probe.py")
TPCDS_INPUTS = Path(__file__).with_name("tpcds_inputs.json")
TPCDS_BASE = Path(__file__).with_name("tpcds_base.json")  # written at image build
TPCDS_TYPES = Path(__file__).with_name("tpcds_types.json")
TPCDS_QUERY_LINES = Path(__file__).with_name("tpcds_query_lines.json.gz")
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_SCREEN_MIN_QUERY = 8
WORKER_COST_MS = 2000
MAX_WORKERS = 8
FAMILIES = ("dialects", "parsers", "generators", "typing", "tokenizers")
IGNORED_PREFIXES = (".github/", "docs/", "posts/", "benchmarks/", "pdoc/")
IGNORED_SUFFIXES = (".md", ".rst", ".txt", ".png", ".cfg", ".toml", ".in")


def log(*a):
    print(*a, file=sys.stderr)


class Selector:
    def __init__(self, tests, m):
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.all = set(self.ms)
        self.by_test_file = {}
        for t in tests:
            self.by_test_file.setdefault(t["file"], set()).add(t["number"])
        self.lines = {}
        self.file_tests = {}
        for f, groups in m["lines"].items():
            d = {}
            ft = set()
            for lns, ts in groups:
                s = frozenset(ts)
                ft.update(n for n in s if n >= 0)
                for ln in lns:
                    d[ln] = s
            self.lines[f] = d
            self.file_tests[f] = ft
        self.opened = {k: set(v) for k, v in m["opened"].items()}
        self.mention = {k: set(v) for k, v in m["mention"].items()}
        # dialect name -> tests that used it at run time (through inheritance
        # or lookups by name such as Dialect["teradata"])
        self.uses = {k: set(v) for k, v in m.get("uses", {}).items()}
        # expression class name -> tests that created an instance of it or a subclass
        self.inst = {k: set(v) for k, v in m.get("inst", {}).items()}
        self.children = m["children"]
        self.bodies = m["bodies"]
        self.body_starts = {f: [r[0] for r in rs] for f, rs in self.bodies.items()}
        self.tpcds_exec = {}
        for f, rs in m.get("tpcds_exec", {}).items():
            st = set()
            for a, b in rs:
                st.update(range(a, b + 1))
            self.tpcds_exec[f] = st
        self.tpcds = {t["number"] for t in tests if t["id"] == TPCDS_ID}
        self.tpcds_queries = QueryIndex.load(TPCDS_QUERY_LINES, TPCDS_INPUTS) if self.tpcds else None
        self.tpcds_types = {}
        if self.tpcds and TPCDS_TYPES.is_file():
            self.tpcds_types = json.loads(TPCDS_TYPES.read_text("utf-8"))
        self.setting_index = None
        if SETTING_READS.is_file():
            try:
                self.setting_index = SettingIndex.load(SETTING_READS, tests)
            except (AttributeError, EOFError, KeyError, OSError, TypeError, ValueError, zlib.error) as ex:
                log("setting read map disabled:", repr(ex))
        self.import_lines = {}
        for f, rs in m.get("importlines", {}).items():
            st = set()
            for a, b in rs:
                st.update(range(a, b + 1))
            self.import_lines[f] = st
        # tests the map never saw run (e.g. Python-version-specific ones): run
        # them whenever library code changes, since we can't tell what they need
        seen = set().union(*self.file_tests.values(), *self.opened.values())
        self.blind = self.all - seen
        self.test_id = {t["number"]: t["id"] for t in tests}
        self.generator_memory = {
            n for n, tid in self.test_id.items() if tid.endswith("::test_generator_memory")
        }

    def in_body(self, f, ln):
        starts = self.body_starts.get(f)
        if not starts:
            return False
        i = bisect.bisect_right(starts, ln) - 1
        return i >= 0 and self.bodies[f][i][0] <= ln <= self.bodies[f][i][1]

    def family(self, name):
        out, stack = {name}, [name]
        while stack:
            for c in self.children.get(stack.pop(), []):
                if c not in out:
                    out.add(c)
                    stack.append(c)
        return out

    def blind_for(self, f, import_time):
        """Tests the map never saw run, narrowed by what each one checks."""
        out = set()
        for n in self.blind:
            tid = self.test_id[n]
            if tid.startswith("tests/test_sqlglotc.py::"):
                # exercises sqlglotc/setup.py build plumbing, not sqlglot/ code
                continue
            if tid.endswith("::test_lazy_load"):
                # runs `import sqlglot` in a subprocess: only import-time code matters
                if import_time or f in (
                    "sqlglot/__init__.py",
                    "sqlglot/dialects/__init__.py",
                    "sqlglot/dialects/dialect.py",
                ):
                    out.add(n)
                continue
            if tid.endswith("::test_no_redundant_expression_metadata"):
                # reads sqlglot/typing sources and their EXPRESSION_METADATA
                if import_time or f.startswith("sqlglot/typing/"):
                    out.add(n)
                continue
            out.add(n)  # anything unknown: keep
        return out

    def import_time_fallback(self, f, classes=frozenset({"?"})):
        sel = set(self.file_tests[f]) | self.blind_for(f, True)
        parts = f.split("/")
        name = parts[2][:-3] if len(parts) == 3 else None
        if parts[1:2] and parts[1] in FAMILIES and (name in self.mention or name in self.uses):
            for n in self.family(name):
                sel |= self.mention.get(n, set())
                sel |= self.uses.get(n, set())
        if f.startswith("sqlglot/expressions/"):
            # an edit to an expression class body (arg_types, flags...) matters
            # to every test that builds that expression or a subclass of it
            for c in classes:
                sel |= self.inst.get(c, set())
        if not self.generator_related(f, classes):
            # test_generator_memory only measures memory retained by
            # repeatedly creating each dialect's Generator
            sel -= self.generator_memory
        return sel

    def select_regions(self, f, regions):
        """regions: list of ('import', class_names) or ('runtime', old_lines)."""
        d = self.lines.get(f)
        if d is None:
            return self.all, f"unmapped {f} -> all"
        imports = self.import_lines.get(f, set())
        sel = set()
        import_time = False
        classes = set()
        for kind, info in regions:
            if kind == "import":
                import_time = True
                classes |= set(info or {"?"})
                continue
            if info & imports:
                import_time = True
                classes.add("?")  # module-level code calls this: unknown scope
                continue
            for ln in info:
                s = d.get(ln)
                if s:
                    if -1 in s:
                        import_time = True
                        classes.add("?")
                    sel.update(n for n in s if n >= 0)
        if import_time:
            return sel | self.import_time_fallback(f, classes), f"import-time* {f}"
        return sel | self.blind_for(f, False), f"statement-level {f} ({len(regions)} regions)"

    def generator_related(self, f, classes):
        """Could this import-time change alter what creating a Generator allocates?"""
        if f.startswith("sqlglot/generators/") or f in (
            "sqlglot/generator.py",
            "sqlglot/dialects/dialect.py",
        ):
            return True
        return any(c == "?" or "Generator" in c for c in classes)

    def select_file(self, f, changed):
        if f.startswith("tests/"):
            if f.endswith(".py"):
                # a test file -> its own tests; a helper (helpers.py, ...) -> all
                return self.by_test_file.get(f, self.all), f"test code {f}"
            if f in self.opened:
                return self.opened[f], f"fixture {f}"
            return self.all, f"unknown test data {f} -> all"
        if f.startswith("sqlglot/") and f.endswith(".py"):
            d = self.lines.get(f)
            if d is None:
                return self.all, f"unmapped {f} -> all"
            sel = set()
            import_time = False
            for ln in changed:
                if not self.in_body(f, ln):
                    import_time = True
                s = d.get(ln)
                if s:
                    if -1 in s:
                        import_time = True
                    sel.update(n for n in s if n >= 0)
            sel |= self.blind
            if import_time:
                sel |= self.file_tests[f]
                parts = f.split("/")
                if len(parts) == 3 and parts[1] in FAMILIES and parts[2][:-3] in self.mention:
                    for n in self.family(parts[2][:-3]):
                        sel |= self.mention.get(n, set())
                return sel, f"import-time {f}"
            return sel, f"line-level {f}"
        if f.startswith(IGNORED_PREFIXES) or f.endswith(IGNORED_SUFFIXES):
            return set(), f"ignored {f}"
        return self.all, f"other {f} -> all"

    def select_removed(self, f):
        """A file that no longer exists at this path (deleted or renamed away)."""
        if f.startswith("sqlglot/"):
            return self.all, f"removed {f} -> all"
        if f.startswith("tests/"):
            if f.endswith(".py"):
                return self.by_test_file.get(f, self.all), f"removed test code {f}"
            if f in self.opened:
                return self.opened[f], f"removed fixture {f}"
            return self.all, f"removed unknown test data {f} -> all"
        if f.startswith(IGNORED_PREFIXES) or f.endswith(IGNORED_SUFFIXES):
            return set(), f"removed ignored {f}"
        return self.all, f"removed {f} -> all"

    def select_added(self, f):
        """A file that didn't exist before."""
        if f.startswith(IGNORED_PREFIXES) or f.endswith(IGNORED_SUFFIXES):
            return set(), f"added ignored {f}"
        if f.startswith(("sqlglot/", "tests/")) or not "/" in f:
            # new modules can shadow or be discovered; new test files can
            # change collection. Rare, so stay safe.
            return self.all, f"added {f} -> all"
        return self.all, f"added {f} -> all"

    def select(self, patch_text):
        sel = set()
        notes = []
        entries = parse_diff(patch_text)
        if not entries and patch_text.strip():
            return set(self.all), ["unparsed patch -> all"]
        patched = {}  # sqlglot files whose code changed -> new text (None = gone)
        import_modules = set()
        runtime_lines = {}  # file -> old lines of runtime-only changes
        tpcds_import_files = set()  # dialect files whose class bodies changed
        tpcds_import_blocked = False  # an import-time change the tpcds check can't vouch for
        tpcds_ok = True  # every change is a runtime change in sqlglot/*.py
        for e in entries:
            if not (e.path.startswith("sqlglot/") and e.path.endswith(".py")) or (
                e.binary or e.deleted or e.renamed or e.added or not e.hunks
            ):
                tpcds_ok = False
            if e.binary:
                f = e.path
                if f.startswith(IGNORED_PREFIXES) or f.endswith(IGNORED_SUFFIXES):
                    notes.append(f"binary ignored {f}")
                    continue
                sel |= self.all
                notes.append(f"binary {f} -> all")
                continue
            if e.deleted or e.renamed:
                s, note = self.select_removed(e.old)
                sel |= s
                notes.append(note)
                if e.renamed:
                    s, note = self.select_added(e.new)
                    sel |= s
                    notes.append(note)
                continue
            if e.added:
                s, note = self.select_added(e.new)
                sel |= s
                notes.append(note)
                continue
            f = e.path
            if not e.hunks:
                notes.append(f"metadata-only {f}")  # e.g. a mode change
                continue
            changed = e.changed_old_lines()
            if f.startswith("sqlglot/") and f.endswith(".py"):
                # Tests that inspect source can be affected by comments too.
                sel.update(self.opened.get(f, ()))
                kind, regions, new_text = analyse_source_change(f, e.hunks)
                if kind == "noop":
                    notes.append(f"comment-only {f}")
                    continue
                if kind == "code" and regions is not None and all(k == "runtime" for k, _ in regions):
                    runtime_lines.setdefault(f, set()).update(*(x for _, x in regions))
                elif kind == "code" and regions is not None and self.tpcds_import_allowed(f):
                    # class-body edits in a SQL dialect the executor never uses:
                    # the probe preloads the patched module, so any effect on
                    # parsing, optimizing or the DuckDB side shows up as DIFF
                    runtime_lines.setdefault(f, set()).update(
                        *(x for k, x in regions if k == "runtime")
                    )
                    tpcds_import_files.add(f)
                else:
                    tpcds_ok = False
                if kind == "syntax":
                    sel |= self.all
                    notes.append(f"syntax error {f} -> all")
                    continue
                if new_text is not None:
                    patched[f] = new_text
                if kind == "code" and regions is not None and f in self.lines:
                    s, note = self.select_regions(f, regions)
                    sel |= s
                    notes.append(note)
                    if note.startswith("import-time"):
                        import_modules |= self.modules_to_import(f)
                        if f not in tpcds_import_files:
                            tpcds_import_blocked = True
                    continue
                if kind is None:
                    # the patch doesn't apply to our copy: be safe
                    sel |= self.all
                    notes.append(f"patch does not apply to {f} -> all")
                    continue
                if f in self.lines:
                    import_modules |= self.modules_to_import(f)
                    tpcds_import_blocked = True
            s, note = self.select_file(f, changed)
            sel |= s
            notes.append(note)
        if import_modules and patched and sel != self.all:
            try:
                broken = imports_break(patched, sorted(import_modules))
            except Exception as ex:
                broken = True
                notes.append(f"import check failed {ex!r}")
            if broken:
                sel = set(self.all)
                notes.append("import raises -> all")
            else:
                notes.append("imports ok")
                if len(entries) == 1 and self.setting_index:
                    f = entries[0].path
                    before = original_source(f)
                    if f in patched and before is not None and self.setting_index.matches_source(f, before):
                        setting = edited_setting(before, patched[f])
                        narrowed = self.setting_index.narrow(f, setting, sel)
                        if narrowed is not None:
                            # Source inspections and tests outside the line map retain
                            # the original selector's conservative treatment.
                            narrowed |= set(self.opened.get(f, ())) | (sel & self.blind)
                            if len(narrowed) < len(sel):
                                notes.append(f"setting reads: {len(sel)} -> {len(narrowed)}")
                                sel = narrowed
        if (
            tpcds_ok
            and not tpcds_import_blocked
            and not any("import raises" in n for n in notes)
            and self.tpcds
            and self.tpcds <= sel
            and sel != self.all
            and patched
            and (any(runtime_lines.values()) or tpcds_import_files)
        ):
            if len(entries) == 1:
                f = entries[0].path
                facts = self.tpcds_types.get(f)
                if facts and equivalent_none_test(original_source(f), patched[f], facts):
                    sel -= self.tpcds
                    notes.append("tpcds: equivalent None comparison on observed types")
                    return sel, notes
            touched = [
                f for f, ls in runtime_lines.items()
                if ls & self.tpcds_exec.get(f, set()) or ls & self.import_lines.get(f, set())
            ]
            if touched:
                notes.append("tpcds: executor-side lines changed, keep")
            else:
                try:
                    queries = self.tpcds_queries.affected_queries(runtime_lines) if self.tpcds_queries else None
                    screen = queries[:1] if queries and queries[0] >= TPCDS_SCREEN_MIN_QUERY else ()
                    preload = sorted(
                        f[:-3].replace("/", ".") for f in tpcds_import_files
                    )
                    same = tpcds_inputs_unchanged(patched, screen, preload)
                except Exception as ex:
                    same = False
                    notes.append(f"tpcds probe failed {ex!r}")
                if same:
                    sel = sel - self.tpcds
                    notes.append("tpcds: identical inputs and executor code -> dropped")
                else:
                    notes.append("tpcds: inputs differ, keep")
        return sel, notes

    def tpcds_import_allowed(self, f):
        """A SQL dialect's own file (dialects/parsers/generators/typing/tokenizers
        <name>.py). The TPC-DS executor runs on the Python dialect and the base
        classes, so these only reach it through parsing, optimizing and the
        DuckDB transpile, all of which the probe fingerprints."""
        parts = f.split("/")
        if len(parts) != 3 or parts[0] != "sqlglot" or parts[1] not in FAMILIES:
            return False
        name = parts[2][:-3]
        if name in ("__init__", "dialect", "base", "python"):
            return False
        return "python" not in self.family(name)

    def modules_to_import(self, f):
        mods = {f[:-3].replace("/", ".").removesuffix(".__init__")}
        parts = f.split("/")
        if len(parts) == 3 and parts[1] in FAMILIES:
            for n in self.family(parts[2][:-3]):
                for fam in FAMILIES:
                    mods.add(f"sqlglot.{fam}.{n}")
        else:
            # core code (tokens, parser, generator, expressions...) is subclassed
            # by every dialect: class creation can fail anywhere, so load it all
            mods.add("ALL")
        mods.add("sqlglot")
        return mods


_SOURCES = None


def original_source(path):
    """sqlglot's own source at the pinned commit (sqlglot_src.tgz)."""
    global _SOURCES
    if _SOURCES is None:
        _SOURCES = {}
        with tarfile.open(SRC_PATH, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    _SOURCES[member.name] = tar.extractfile(member).read().decode("utf-8")
    return _SOURCES.get(path)


def equivalent_none_test(old, new, facts):
    """Can the single edit only change an identity check to default equality?"""
    old_lines = old.encode("utf-8").split(b"\n")
    new_lines = new.encode("utf-8").split(b"\n")
    if len(old_lines) != len(new_lines):
        return False
    changed = [(i, a, b) for i, (a, b) in enumerate(zip(old_lines, new_lines), 1) if a != b]
    if len(changed) != 1:
        return False
    line, before, after = changed[0]
    for start, end, types in facts.get(str(line), []):
        # AST column offsets are UTF-8 byte offsets. Every recorded operand
        # must be None itself or use object.__eq__, so equality with None
        # cannot call code that changes the comparison's result or state.
        if not types or any(
            cls != "builtins.NoneType" and eq_owner != "builtins.object"
            for cls, eq_owner in types
        ):
            continue
        if before[start:end].strip() != b"is":
            continue
        prefix, suffix = before[:start], before[end:]
        if after.startswith(prefix) and after.endswith(suffix):
            operator = after[start:len(after) - len(suffix)].strip()
            if operator == b"==":
                return comparison_shape(old) == comparison_shape(new)
    return False


def comparison_shape(source):
    """Compare syntax apart from the operator, preserving debug f-string text."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
        ):
            node.ops[0] = ast.Is()
    return ast.dump(tree, include_attributes=False)


_BASE = None
IMPORT_CHECK = """
import importlib, importlib.util, importlib.machinery, os, sys
base, overlay = sys.argv[1], sys.argv[2]


class Overlay:
    # serves patched modules from `overlay`; everything else comes from `base`
    def find_spec(self, fullname, path=None, target=None):
        rel = fullname.replace(".", "/")
        for cand, pkg in ((rel + ".py", False), (rel + "/__init__.py", True)):
            f = os.path.join(overlay, cand)
            if os.path.exists(f + ".deleted"):
                raise ModuleNotFoundError(fullname, name=fullname)
            if os.path.exists(f):
                locs = [os.path.join(base, rel)] if pkg else None
                return importlib.util.spec_from_file_location(
                    fullname, f, submodule_search_locations=locs
                )
        return None


sys.meta_path.insert(0, Overlay())
sys.path.insert(0, base)
names = sys.argv[3:]
if "ALL" in names:
    import pkgutil
    import sqlglot

    names = [n for n in names if n != "ALL"] + [
        i.name for i in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot.")
        if i.name != "sqlglot.__main__"
    ]
bad = []
for name in names:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name and name.startswith(e.name) and not os.path.exists(
            os.path.join(overlay, e.name.replace(".", "/") + ".py.deleted")
        ):
            continue  # module doesn't exist in this family
        bad.append(name)
    except BaseException:
        bad.append(name)
print("BAD" if bad else "OK")
"""
SRC_TREE = Path(__file__).with_name("src")  # built into the image, with checked-hash .pyc


def base_tree():
    """Pinned sqlglot sources. The image ships them precompiled with
    checked-hash bytecode (validated against the source on every import);
    otherwise extract them into /tmp."""
    global _BASE
    if _BASE is None:
        if (SRC_TREE / "sqlglot" / "__init__.py").exists():
            _BASE = SRC_TREE
        else:
            root = Path("/tmp/br_src")
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            with tarfile.open(SRC_PATH, "r:gz") as tar:
                tar.extractall(root, filter="data")
            _BASE = root
    return _BASE


_PROBES = [0]


def imports_break(patched, modules):
    """Import `modules` in a fresh interpreter where the patched files (None =
    deleted) shadow the pinned tree. The patched files live in a new overlay
    directory and are never compiled to disk (-B). True unless the probe
    exits 0 and prints exactly OK."""
    base = base_tree()
    _PROBES[0] += 1
    overlay = Path(f"/tmp/br_overlay_{os.getpid()}_{_PROBES[0]}")
    shutil.rmtree(overlay, ignore_errors=True)
    try:
        for rel, text in patched.items():
            target = overlay / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                (overlay / (rel + ".deleted")).write_text("")
            else:
                target.write_text(text, "utf-8")
        r = subprocess.run(
            [sys.executable, "-I", "-B", "-c", IMPORT_CHECK, str(base), str(overlay), *modules],
            cwd="/tmp", capture_output=True, text=True, timeout=60,
        )
        return not (r.returncode == 0 and r.stdout.strip() == "OK")
    finally:
        shutil.rmtree(overlay, ignore_errors=True)


TPCDS_RUN = "import sys\nsys.path[:] = [p for p in sys.path if p not in ('', '.')]\n" + IMPORT_CHECK.split("sys.meta_path.insert(0, Overlay())")[0] + """
sys.meta_path.insert(0, Overlay())
sys.path.insert(0, base)
import importlib, runpy
for _name in filter(None, os.environ.get("BR_PRELOAD", "").split(",")):
    importlib.import_module(_name)  # load patched dialect modules first; errors -> nonzero exit
probe = sys.argv[3]
sys.argv = [probe] + sys.argv[4:]
runpy.run_path(probe, run_name="__main__")
"""


def check_tpcds_inputs(command, env, screen):
    runtime = dict(cwd="/tmp", env=env, capture_output=True, text=True)
    if screen:
        subset = [*command, "--queries", ",".join(map(str, screen))]
        try:
            probe = subprocess.run(subset, timeout=2, **runtime)
        except (OSError, subprocess.TimeoutExpired):
            pass
        else:
            if probe.returncode == 0 and probe.stdout.strip() == "DIFF":
                return False
    # A matching subset supplies no evidence about the remaining queries or
    # their shared state. Make the removal decision in a fresh ordered run.
    probe = subprocess.run(command, timeout=120, **runtime)
    return probe.returncode == 0 and probe.stdout.strip() == "SAME"


def tpcds_inputs_unchanged(patched, screen=(), preload=()):
    """Run tpcds_probe.py on the patched tree and compare with the pinned
    commit's fingerprints. Any doubt (missing files, errors) -> False."""
    if not (TPCDS_BASE.exists() and TPCDS_PROBE.exists() and TPCDS_INPUTS.exists()):
        return False
    base = base_tree()
    _PROBES[0] += 1
    overlay = Path(f"/tmp/br_overlay_{os.getpid()}_{_PROBES[0]}")
    shutil.rmtree(overlay, ignore_errors=True)
    try:
        for rel, text in patched.items():
            if text is None:
                return False
            target = overlay / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, "utf-8")
        env = {"PYTHONHASHSEED": "0", "PATH": os.environ.get("PATH", ""),
               "BR_PRELOAD": ",".join(preload)}
        # -I would ignore PYTHONHASHSEED. TPCDS_RUN removes the working directory
        # from sys.path explicitly.
        command = [sys.executable, "-s", "-B", "-c", TPCDS_RUN, str(base), str(overlay),
                   str(TPCDS_PROBE), str(TPCDS_INPUTS), str(TPCDS_BASE)]
        return check_tpcds_inputs(command, env, screen)
    finally:
        shutil.rmtree(overlay, ignore_errors=True)


class FileDiff:
    """One file's part of a patch, with Git's metadata."""

    def __init__(self, old=None, new=None):
        self.old = old  # path before (None if the file is new)
        self.new = new  # path after (None if the file is deleted)
        self.added = False
        self.deleted = False
        self.binary = False
        self.hunks = []  # [(old_start, [(tag, text)])]

    @property
    def path(self):
        return self.new or self.old

    @property
    def renamed(self):
        return self.old is not None and self.new is not None and self.old != self.new

    def changed_old_lines(self):
        """Old-file line numbers touched: removed lines, and the lines on both
        sides of every insertion point."""
        out = set()
        for start, body in self.hunks:
            old = start
            for tag, _ in body:
                if tag == "-":
                    out.add(old)
                    old += 1
                elif tag == "+":
                    out.add(old - 1)
                    out.add(old)
                else:
                    old += 1
        return out


def _strip_prefix(p, prefix):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    return p[len(prefix):] if p.startswith(prefix) else p


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT_RE = re.compile(r"^diff --git (?:\"?a/)(.*?)\"? (?:\"?b/)(.*?)\"?$")


def parse_diff(text):
    """Parse a unified/Git diff into FileDiff entries. Hunk bodies are read
    by their line counts, so content lines like '--- x' are never mistaken
    for headers."""
    entries = []
    e = None
    hunk = None
    rem_old = rem_new = 0
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            if line.startswith("\\"):
                continue
            tag = line[:1] if line else " "
            if tag in " -+":
                if tag == " ":
                    rem_old -= 1
                    rem_new -= 1
                elif tag == "-":
                    rem_old -= 1
                else:
                    rem_new -= 1
                hunk[1].append((tag, line[1:]))
                continue
        hunk = None
        if line.startswith("\\"):
            continue
        m = GIT_RE.match(line)
        if m:
            e = FileDiff(m.group(1), m.group(2))
            entries.append(e)
            continue
        if line.startswith("--- ") and (e is None or e.hunks):
            e = FileDiff()
            entries.append(e)
        if e is None:
            continue
        if line.startswith("rename from ") or line.startswith("copy from "):
            e.old = line.split(" from ", 1)[1].strip()
            if line.startswith("copy"):
                e.added = True
        elif line.startswith("rename to ") or line.startswith("copy to "):
            e.new = line.split(" to ", 1)[1].strip()
        elif line.startswith("deleted file mode"):
            e.deleted = True
        elif line.startswith("new file mode"):
            e.added = True
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            e.binary = True
        elif line.startswith("--- "):
            e.old = _strip_prefix(line[4:], "a/")
            if e.old is None:
                e.added = True
        elif line.startswith("+++ "):
            e.new = _strip_prefix(line[4:], "b/")
            if e.new is None:
                e.deleted = True
        else:
            m = HUNK_RE.match(line)
            if m:
                rem_old = int(m.group(2)) if m.group(2) is not None else 1
                rem_new = int(m.group(4)) if m.group(4) is not None else 1
                # Git describes a zero-length old range by the line before it.
                hunk = (int(m.group(1)) + (rem_old == 0), [])
                e.hunks.append(hunk)
    for e in entries:
        if e.deleted:
            e.new = None
        if e.added and not e.renamed:
            e.old = None
    return [e for e in entries if e.path]


def apply_hunks(old, hunks):
    """Return the new text, or None if the hunks don't apply cleanly."""
    old_lines = old.split("\n")
    out = []
    pos = 0  # 0-based index into old_lines
    for start, body in hunks:
        start0 = max(start - 1, 0)
        if start0 < pos:
            return None
        out.extend(old_lines[pos:start0])
        pos = start0
        for tag, text in body:
            if tag in " -":
                if pos >= len(old_lines) or old_lines[pos].rstrip("\r") != text.rstrip("\r"):
                    return None
                if tag == " ":
                    out.append(old_lines[pos])
                pos += 1
            else:
                out.append(text)
    out.extend(old_lines[pos:])
    return "\n".join(out)


_SKIP_TOKENS = {tokenize.COMMENT, tokenize.NL}


def code_tokens(text):
    toks = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in _SKIP_TOKENS:
            continue
        toks.append((tok.type, tok.string))
    return toks


CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=]")
CLAUSE_RE = re.compile(r"^(?P<indent>[ \t\f]*)(?P<keyword>else|finally)[ \t]*:")


def analyse_source_change(path, hunks):
    """-> (kind, regions, new_text). kind: 'noop' (only comments/blank lines changed),
    'syntax' (new file doesn't compile), 'code', or None (can't tell).
    regions (for 'code'): list of ('import', None) / ('runtime', old_lines)."""
    old = original_source(path)
    if old is None:
        return None, None, None
    new = apply_hunks(old, hunks)
    if new is None:
        return None, None, None
    try:
        compile(new, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return "syntax", None, new
    head_old = old.split("\n")[:2]
    head_new = new.split("\n")[:2]
    coding_changed = any(CODING_RE.match(l) for l in head_old + head_new) and head_old != head_new
    try:
        if not coding_changed and code_tokens(old) == code_tokens(new):
            return "noop", None, new
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None, None, new
    if coding_changed:
        return "code", [("import", frozenset({"?"}))], new
    try:
        return "code", change_regions(old, new, hunks), new
    except Exception:
        return "code", None, new


def change_regions(old, new, hunks):
    old_idx = StmtIndex(old)
    new_idx = StmtIndex(new)
    new_to_old = {}
    removed = []  # old line numbers
    added = []  # new line numbers
    o = n = 1
    pos_old = 1
    pos_new = 1
    for start, body in hunks:
        # unchanged stretch before the hunk
        while pos_old < start:
            new_to_old[pos_new] = pos_old
            pos_old += 1
            pos_new += 1
        for tag, _ in body:
            if tag == " ":
                new_to_old[pos_new] = pos_old
                pos_old += 1
                pos_new += 1
            elif tag == "-":
                removed.append(pos_old)
                pos_old += 1
            else:
                added.append(pos_new)
                pos_new += 1
    total_new = new.count("\n") + 1
    while pos_new <= total_new:
        new_to_old[pos_new] = pos_old
        pos_old += 1
        pos_new += 1

    old_text_lines = old.split("\n")
    new_text_lines = new.split("\n")
    regions = []

    def header_lines(idx, node):
        b = idx.body_start(node)
        end = (b - 1) if b is not None else node.end_lineno
        return set(range(StmtIndex.first_line(node), end + 1))

    parents_old_map = {id(x[0]): x for x in old_idx.nodes}
    parents_new_map = {id(x[0]): x for x in new_idx.nodes}

    def classes_of(entry, pmap):
        names = set()
        cur = entry
        while cur is not None:
            if isinstance(cur[0], ast.ClassDef):
                names.add(cur[0].name)
            cur = pmap.get(id(cur[1])) if cur[1] is not None else None
        return frozenset(names)

    def old_entry_at(old_line, like):
        """The old statement starting at old_line of the same type as `like`."""
        for e in old_idx.nodes:
            if StmtIndex.first_line(e[0]) == old_line and type(e[0]) is type(like):
                return e
        return None

    for L in removed:
        clause = old_idx.clause_owner(L, old_text_lines[L - 1])
        e = clause or old_idx.innermost(L)
        if e is None:
            continue
        node, parent, in_func = e
        if not clause and not old_idx.is_header_line(node, L):
            continue  # comment/blank line between statements
        if not in_func:
            regions.append(("import", classes_of(e, parents_old_map)))
        else:
            lines = set(range(node.lineno, node.end_lineno + 1)) if clause else header_lines(old_idx, node)
            regions.append(("runtime", lines))

    parents_new = {id(e[0]): e for e in new_idx.nodes}
    for N in added:
        clause = new_idx.clause_owner(N, new_text_lines[N - 1])
        e = clause or new_idx.innermost(N)
        if e is None:
            continue
        node, parent, in_func = e
        if not clause and not new_idx.is_header_line(node, N):
            continue
        if not in_func:
            regions.append(("import", classes_of(e, parents_new_map)))
            continue
        # climb to the nearest statement that also exists in the old file
        cur = e
        found = None
        first = True
        while cur is not None:
            cnode, cparent, cin = cur
            old_line = new_to_old.get(StmtIndex.first_line(cnode))
            if old_line is not None:
                oe = old_entry_at(old_line, cnode)
                if oe is not None:
                    found = (oe, first)
                    break
            first = False
            cur = parents_new.get(id(cparent)) if cparent is not None else None
        if found is None:
            regions.append(("import", classes_of(e, parents_new_map)))
            continue
        (onode, oparent, oin), same_stmt = found
        if clause and oin:
            regions.append(("runtime", set(range(onode.lineno, onode.end_lineno + 1))))
        elif same_stmt:
            # an existing statement was edited in place
            if oin:
                regions.append(("runtime", header_lines(old_idx, onode)))
            else:
                regions.append(("import", classes_of(found[0], parents_old_map)))
        elif isinstance(onode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            b = old_idx.body_start(onode)
            regions.append(("runtime", set(range(b, onode.end_lineno + 1))))
        elif oin:
            regions.append(("runtime", header_lines(old_idx, onode)))
        else:
            regions.append(("import", classes_of(found[0], parents_old_map)))
    return regions


class StmtIndex:
    """Statement structure of one version of a file."""

    def __init__(self, text):
        self.tree = ast.parse(text)
        self.nodes = []  # (stmt, parent_stmt_or_None, inside_function_body)
        self._walk(self.tree.body, None, False)

    def _walk(self, body, parent, in_func):
        for node in body:
            self.nodes.append((node, parent, in_func))
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                sub = getattr(node, field, None)
                if not sub:
                    continue
                inner = in_func or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if field == "handlers" or field == "cases":
                    for h in sub:
                        if hasattr(h, "lineno"):
                            self.nodes.append((h, node, inner))
                            self._walk(h.body, h, inner)
                        else:  # match_case has no position of its own
                            self._walk(h.body, node, inner)
                else:
                    self._walk(sub, node, inner)

    @staticmethod
    def first_line(node):
        decos = getattr(node, "decorator_list", None)
        if decos:
            return min(d.lineno for d in decos)
        return node.lineno

    @staticmethod
    def body_start(node):
        starts = []
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            sub = getattr(node, field, None)
            if sub and isinstance(sub, list) and hasattr(sub[0], "lineno"):
                starts.append(min(StmtIndex.first_line(x) for x in sub))
        return min(starts) if starts else None

    def innermost(self, line):
        """Smallest statement whose span contains line -> (node, parent, in_func)."""
        best = None
        for entry in self.nodes:
            node = entry[0]
            if self.first_line(node) <= line <= node.end_lineno:
                if best is None or (node.end_lineno - self.first_line(node)) <= (
                    best[0].end_lineno - self.first_line(best[0])
                ):
                    best = entry
        return best

    def clause_owner(self, line, text):
        """Find the compound statement even when the clause has an inline body."""
        match = CLAUSE_RE.match(text)
        if match is None:
            return None
        field = "orelse" if match["keyword"] == "else" else "finalbody"
        indent = len(match["indent"])
        owner = None
        for entry in self.nodes:
            node = entry[0]
            body = getattr(node, field, ())
            if node.col_offset == indent and body and node.lineno < line <= body[0].lineno:
                if owner is None or node.lineno >= owner[0].lineno:
                    owner = entry
        return owner

    def is_header_line(self, node, line):
        """True if line is in node's own text (not in a nested body)."""
        b = self.body_start(node)
        return b is None or line < b


def split(sel, ms):
    if not sel:
        return []
    ts = sorted(sel, key=lambda n: -ms[n])
    best = None
    for count in range(1, min(MAX_WORKERS, len(ts)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for n in ts:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += ms[n]
        cost = max(loads) + WORKER_COST_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    all_tests = {t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    try:
        selector = Selector(tests, json.loads(gzip.decompress(MAP_PATH.read_bytes())))
    except Exception as e:  # no map -> safe fallback
        log("map failed to load:", repr(e))
        selector = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            if selector is None:
                raise RuntimeError("no map")
            sel, notes = selector.select(patch.read_text(errors="replace"))
            sel &= all_tests
        except Exception as e:
            sel, notes = all_tests, [f"error {e!r} -> all"]
        print(json.dumps({"commit": patch.stem, "workers": split(sel, ms)}))
        log(patch.stem, len(sel), "; ".join(notes))


if __name__ == "__main__":
    main()
