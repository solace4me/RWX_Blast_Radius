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
import tokenize
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP_PATH = Path(__file__).with_name("map.json.gz")
SRC_PATH = Path(__file__).with_name("sqlglot_src.tgz")
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
        self.children = m["children"]
        self.bodies = m["bodies"]
        self.body_starts = {f: [r[0] for r in rs] for f, rs in self.bodies.items()}
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

    def import_time_fallback(self, f):
        sel = set(self.file_tests[f]) | self.blind
        parts = f.split("/")
        if len(parts) == 3 and parts[1] in FAMILIES and parts[2][:-3] in self.mention:
            for n in self.family(parts[2][:-3]):
                sel |= self.mention.get(n, set())
        return sel

    def select_regions(self, f, regions):
        """regions: list of ('import', None) or ('runtime', set_of_old_lines)."""
        d = self.lines.get(f)
        if d is None:
            return self.all, f"unmapped {f} -> all"
        imports = self.import_lines.get(f, set())
        sel = set()
        import_time = False
        for kind, lines in regions:
            if kind == "import" or lines & imports:
                import_time = True
                continue
            for ln in lines:
                s = d.get(ln)
                if s:
                    if -1 in s:
                        import_time = True
                    sel.update(n for n in s if n >= 0)
        if import_time:
            return sel | self.import_time_fallback(f), f"import-time* {f}"
        return sel | self.blind, f"statement-level {f} ({len(regions)} regions)"

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

    def select(self, patch_text):
        sel = set()
        notes = []
        hunks = split_patch(patch_text)
        for f, changed in parse_patch(patch_text).items():
            if f.startswith("sqlglot/") and f.endswith(".py") and f in hunks:
                kind, regions = analyse_source_change(f, hunks[f])
                if kind == "code" and regions is not None and f in self.lines:
                    s, note = self.select_regions(f, regions)
                    sel |= s
                    notes.append(note)
                    continue
                if kind == "noop":
                    notes.append(f"comment-only {f}")
                    continue
                if kind == "syntax":
                    sel |= self.all
                    notes.append(f"syntax error {f} -> all")
                    continue
            s, note = self.select_file(f, changed)
            sel |= s
            notes.append(note)
        return sel, notes


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


def split_patch(text):
    """file -> list of hunks; a hunk is (old_start, [(tag, text)]) with tag in ' -+'."""
    files = {}
    cur = None
    minus_path = None
    hunk = None
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = None
            hunk = None
            continue
        if line.startswith("--- ") and hunk is None or (line.startswith("--- ") and cur is None):
            p = line[4:].strip()
            minus_path = p[2:] if p.startswith("a/") else p
            continue
        if line.startswith("+++ ") and (hunk is None or cur is None):
            p = line[4:].strip()
            p = p[2:] if p.startswith("b/") else p
            cur = minus_path if p == "/dev/null" else p
            files.setdefault(cur, [])
            hunk = None
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m and cur is not None:
            hunk = (int(m.group(1)), [])
            files[cur].append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("\\"):
            continue
        tag = line[:1] if line[:1] in "+- " else " "
        hunk[1].append((tag, line[1:]))
    return files


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


def analyse_source_change(path, hunks):
    """-> (kind, regions). kind: 'noop' (only comments/blank lines changed),
    'syntax' (new file doesn't compile), 'code', or None (can't tell).
    regions (for 'code'): list of ('import', None) / ('runtime', old_lines)."""
    old = original_source(path)
    if old is None:
        return None, None
    new = apply_hunks(old, hunks)
    if new is None:
        return None, None
    try:
        compile(new, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return "syntax", None
    head_old = old.split("\n")[:2]
    head_new = new.split("\n")[:2]
    coding_changed = any(CODING_RE.match(l) for l in head_old + head_new) and head_old != head_new
    try:
        if not coding_changed and code_tokens(old) == code_tokens(new):
            return "noop", None
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None, None
    if coding_changed:
        return "code", [("import", None)]
    try:
        return "code", change_regions(old, new, hunks)
    except Exception:
        return "code", None


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

    parents_old = {id(e[0]): e for e in old_idx.nodes}
    regions = []

    def header_lines(idx, node):
        b = idx.body_start(node)
        end = (b - 1) if b is not None else node.end_lineno
        return set(range(StmtIndex.first_line(node), end + 1))

    def old_entry_at(old_line, like):
        """The old statement starting at old_line of the same type as `like`."""
        for e in old_idx.nodes:
            if StmtIndex.first_line(e[0]) == old_line and type(e[0]) is type(like):
                return e
        return None

    for L in removed:
        e = old_idx.innermost(L)
        if e is None:
            continue
        node, parent, in_func = e
        if not old_idx.is_header_line(node, L):
            continue  # comment/blank line between statements
        if not in_func:
            regions.append(("import", None))
        else:
            regions.append(("runtime", header_lines(old_idx, node)))

    parents_new = {id(e[0]): e for e in new_idx.nodes}
    for N in added:
        e = new_idx.innermost(N)
        if e is None:
            continue
        node, parent, in_func = e
        if not new_idx.is_header_line(node, N):
            continue
        if not in_func:
            regions.append(("import", None))
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
            regions.append(("import", None))
            continue
        (onode, oparent, oin), same_stmt = found
        if same_stmt:
            # an existing statement was edited in place
            regions.append(("runtime" if oin else "import", header_lines(old_idx, onode) if oin else None))
        elif isinstance(onode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            b = old_idx.body_start(onode)
            regions.append(("runtime", set(range(b, onode.end_lineno + 1))))
        elif oin:
            regions.append(("runtime", header_lines(old_idx, onode)))
        else:
            regions.append(("import", None))
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

    def is_header_line(self, node, line):
        """True if line is in node's own text (not in a nested body)."""
        b = self.body_start(node)
        return b is None or line < b


def parse_patch(text):
    """file -> set of old-file line numbers touched (removed lines, and the
    lines on both sides of every insertion point)."""
    files = {}
    cur = None
    old = 0
    minus_path = None
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = None
            continue
        if line.startswith("--- "):
            p = line[4:].strip()
            minus_path = p[2:] if p.startswith("a/") else p
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            p = p[2:] if p.startswith("b/") else p
            if p == "/dev/null":
                p = minus_path
            cur = p
            files.setdefault(cur, set())
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+", line)
        if m:
            old = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("-"):
            files[cur].add(old)
            old += 1
        elif line.startswith("+"):
            files[cur].add(old - 1)
            files[cur].add(old)
        elif line.startswith(" ") or line == "":
            old += 1
    # a rename or mode change with no hunks still touches the file
    return files


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
