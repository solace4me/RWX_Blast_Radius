"""Record types at existing `value is None` checks during the pinned TPC-DS test.

Run with the scorer's Python and requirements from a pristine pinned checkout:
    python /path/to/tools/record_tpcds_types.py /path/to/tpcds_types.json
"""

import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


class RecordChecks(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Is):
            return node
        right = node.comparators[0]
        left = node.left
        if not (isinstance(right, ast.Constant) and right.value is None):
            return node
        if left.end_lineno != right.lineno:
            return node
        site = (self.path, right.lineno, left.end_col_offset, right.col_offset)
        node.left = ast.copy_location(
            ast.Call(
                func=ast.Name(id="_blast_radius_record_type", ctx=ast.Load()),
                args=[left, ast.Constant(value=site)],
                keywords=[],
            ),
            left,
        )
        return node


def install():
    """Loaded via sitecustomize in pytest and in its spawned workers."""
    root = Path.cwd()
    output = Path(os.environ["BR_TPCDS_TRACE_DIR"])
    observed = {}

    def record(value, site):
        cls = type(value)
        # Record the method that was available during this execution, even
        # if the class changes before the process writes its observations.
        default_eq = (
            type(cls) is type
            and cls.__eq__ is object.__eq__
            and inspect.getattr_static(cls, "__eq__") is object.__eq__
        )
        observed.setdefault(site, set()).add((cls, default_eq))
        return value

    def save():
        facts = []
        for (path, line, start, end), types in observed.items():
            pairs = []
            for cls, default_eq in types:
                pairs.append([cls.__module__ + "." + cls.__qualname__,
                              "builtins.object" if default_eq else "custom"])
            facts.append([path, line, start, end, sorted(pairs)])
        (output / f"{os.getpid()}.json").write_text(json.dumps(facts))

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = Path(self.path)
            source = path.read_bytes()
            tree = ast.parse(source, filename=self.path)
            tree = RecordChecks(path.relative_to(root).as_posix()).visit(tree)
            return compile(ast.fix_missing_locations(tree), self.path, "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                if Path(spec.origin).is_relative_to(root / "sqlglot"):
                    spec.loader = Loader(fullname, spec.origin)
                    return spec
            return None

    builtins._blast_radius_record_type = record
    atexit.register(save)
    sys.meta_path.insert(0, Finder())


def main(output):
    with tempfile.TemporaryDirectory() as tmp:
        shim = Path(tmp) / "sitecustomize.py"
        shim.write_text("from record_tpcds_types import install\ninstall()\n")
        search_path = [tmp, str(Path(__file__).resolve().parent), str(Path.cwd())]
        if os.environ.get("PYTHONPATH"):
            search_path.append(os.environ["PYTHONPATH"])
        env = dict(os.environ, BR_TPCDS_TRACE_DIR=tmp, SKIP_INTEGRATION="0",
                   PYTHONPATH=os.pathsep.join(search_path))
        subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", TEST], env=env, check=True)
        records = [json.loads(p.read_text()) for p in sorted(Path(tmp).glob("*.json"))]
        if sum(bool(record) for record in records) < 2:
            raise RuntimeError("no spawned worker observations")
        observed = {}
        for record in records:
            for path, line, start, end, types in record:
                observed.setdefault((path, line, start, end), set()).update(map(tuple, types))

    files = {}
    for (path, line, start, end), types in sorted(observed.items()):
        files.setdefault(path, {}).setdefault(str(line), []).append([start, end, sorted(types)])
    Path(output).write_text(json.dumps(files, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
