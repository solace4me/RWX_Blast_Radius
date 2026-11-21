"""Fingerprint everything test_execute_tpcds feeds into sqlglot's Python executor.

test_execute_tpcds, for each TPC-DS query: parses it, pickles it to a worker
process, and (for queries marked execute) optimizes it against TPCDS_SCHEMA,
builds a Plan and runs the plan in PythonExecutor; the expected result comes
from DuckDB running transpile(sql, write="duckdb").

This prints one hash per query covering: the unpickled parse tree, the DuckDB
transpilation, the tables found and the optimized tree. Planning and execution
are covered separately: tools/tpcds_exec_lines.py records every line that runs
from Plan(...) onwards. If all hashes match the pinned commit's and none of
those lines changed, the test runs identical code on identical inputs.

usage: PYTHONHASHSEED=0 python tpcds_probe.py INPUTS_JSON [BASE_HASHES_JSON] [--queries 0,4,17]
(with a base file it prints SAME or DIFF, stopping at the first difference)
Query indices are zero-based. A subset comparison only describes the selected
queries. It can be used to screen for a difference before the full replay.
"""
import hashlib
import json
import pickle



def _h(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


class Digest:
    """Content hash of an object graph; shared objects are hashed once."""

    def __init__(self):
        from sqlglot import exp

        self.expr_type = getattr(exp, "Expr", None) or exp.Expression
        self.cache = {}
        self.active = set()

    def __call__(self, obj):
        if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
            return _h(type(obj).__name__ + ":" + repr(obj))
        key = id(obj)
        if key in self.cache:
            return self.cache[key][0]
        if key in self.active:
            return _h("<cycle " + type(obj).__qualname__ + ">")
        self.active.add(key)
        try:
            if isinstance(obj, self.expr_type):
                text = "E:" + repr(obj)
            elif isinstance(obj, dict):
                text = "{" + ",".join(sorted(self(k) + ":" + self(v) for k, v in obj.items())) + "}"
            elif isinstance(obj, (list, tuple)):
                text = type(obj).__name__ + "[" + ",".join(self(x) for x in obj) + "]"
            elif isinstance(obj, (set, frozenset)):
                text = "set[" + ",".join(sorted(self(x) for x in obj)) + "]"
            elif getattr(obj, "__dict__", None) is not None:
                text = type(obj).__qualname__ + self(dict(obj.__dict__))
            elif getattr(type(obj), "__slots__", None):
                slots = type(obj).__slots__
                text = type(obj).__qualname__ + self({s: getattr(obj, s, None) for s in slots})
            else:
                text = type(obj).__qualname__ + ":" + repr(obj)
            digest = _h(text)
            self.cache[key] = (digest, obj)  # keep obj alive so its id isn't reused
            return digest
        finally:
            self.active.discard(key)


DEBUG = False


def main(inputs_path, base_path=None, query_indices=None, *, on_query=None):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    with open(inputs_path, encoding="utf-8") as stream:
        data = json.load(stream)
    base = None
    if base_path is not None:
        with open(base_path, encoding="utf-8") as stream:
            base = json.load(stream)
    schema = data["schema"]
    queries = data["queries"]
    if base is not None and len(base) != len(queries):
        print("DIFF")
        return
    if query_indices is None:
        query_indices = range(len(queries))
    else:
        query_indices = sorted(set(query_indices))
        if not query_indices or query_indices[0] < 0 or query_indices[-1] >= len(queries):
            raise ValueError("query indices must select at least one query within the input fixture")

    out = []
    for index in query_indices:
        if on_query is not None:
            on_query(index)
        sql, execute = queries[index]
        parts = []
        try:
            tree = pickle.loads(pickle.dumps(parse_one(sql)))
            deep = Digest()
            parts.append(deep(tree))
            if execute:
                parts.append(transpile(sql, write="duckdb")[0])
                parts.append(deep(sorted(t.name for t in find_tables(tree))))
                optimized = optimize(
                    tree, ensure_schema(schema), leave_tables_isolated=True, dialect=None
                )
                parts.append(deep(optimized))
        except BaseException as e:  # a different error is a different fingerprint
            parts.append(f"ERROR {type(e).__name__}: {e}")
        if DEBUG:
            out.append([p if len(p) < 80 else _h(p) for p in parts])
            continue
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        if base is not None and base[index] != digest:
            print("DIFF")  # stop at the first difference
            return
        out.append(digest)
    if base is not None:
        print("SAME")
    else:
        print(json.dumps(out))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fingerprint the TPC-DS inputs to the executor.")
    parser.add_argument("inputs_path")
    parser.add_argument("base_path", nargs="?")
    parser.add_argument("--queries", metavar="I,J,...", help="zero-based query indices to compare")
    args = parser.parse_args()
    try:
        indices = [int(index) for index in args.queries.split(",")] if args.queries is not None else None
        main(args.inputs_path, args.base_path, indices)
    except ValueError as exc:
        parser.error(str(exc))
