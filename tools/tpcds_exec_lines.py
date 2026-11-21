"""Record every sqlglot line test_execute_tpcds runs *after* optimization:
building tables, planning, executing in PythonExecutor, pickling results.
Run from a sqlglot checkout at the pinned commit with SKIP_INTEGRATION=0."""
import ast, csv, gzip, json, os, pickle, sys
import coverage

cov = coverage.Coverage(data_file=None, source=["sqlglot"])
from sqlglot import find_tables, parse_one
from sqlglot.executor.table import Table, ensure_tables
from sqlglot.executor.python import PythonExecutor
from sqlglot.optimizer import optimize
from sqlglot.planner import Plan
from sqlglot.schema import ensure_schema
from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

DIR = FIXTURES_DIR + "/optimizer/tpc-ds/"


def open_file(name):
    with open(name, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    return gzip.open(name, "rt", newline="") if gz else open(name, encoding="utf-8", newline="")


cov.start()
tables = {}
for table, columns in TPCDS_SCHEMA.items():
    reader = csv.reader(open_file(f"{DIR}{table}.csv.gz"), delimiter="|")
    rows, ctypes = [], []
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    tables[table] = Table(columns=columns, rows=rows)
cov.stop()

for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    tree = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        continue
    cov.start()
    t = {x.name: tables[x.name] for x in find_tables(tree)}
    tables_ = ensure_tables(t)
    schema = ensure_schema(TPCDS_SCHEMA)
    cov.stop()
    optimized = optimize(tree, schema, leave_tables_isolated=True)
    cov.start()
    result = PythonExecutor(tables=tables_).execute(Plan(optimized))
    result = pickle.loads(pickle.dumps(result))
    _ = (result.columns, list(result.rows))
    cov.stop()

data = cov.get_data()
out = {}
for f in data.measured_files():
    rel = os.path.relpath(f, os.getcwd())
    out[rel] = sorted(data.lines(f))
json.dump(out, open(sys.argv[1], "w"))
print(len(out), "files", sum(map(len, out.values())), "lines", file=sys.stderr)
