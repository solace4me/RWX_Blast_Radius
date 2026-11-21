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

import bisect
import gzip
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
MAP_PATH = Path(__file__).with_name("map.json.gz")
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
        for f, changed in parse_patch(patch_text).items():
            s, note = self.select_file(f, changed)
            sel |= s
            notes.append(note)
        return sel, notes


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
