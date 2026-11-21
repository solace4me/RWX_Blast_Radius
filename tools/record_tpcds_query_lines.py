"""Record per-query probe coverage from the repository's pinned source archive.

Run with the CPython 3.13 image pinned in the Dockerfile:
    PYTHONHASHSEED=0 python -B tools/record_tpcds_query_lines.py tpcds_query_lines.json.gz
"""

import argparse
from contextlib import redirect_stdout
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tpcds_probe import main as run_probe
from tpcds_query_index import INDEX_VERSION


def record_lines(source, inputs_path, query_count):
    monitoring = sys.monitoring
    tool_id = monitoring.COVERAGE_ID
    sqlglot_prefix = str(source / "sqlglot") + os.sep
    startup_bit = 1 << query_count
    query_bit = startup_bit
    lines = {}

    def line_hit(code, line):
        filename = code.co_filename
        if filename.startswith(sqlglot_prefix):
            path = Path(filename).relative_to(source).as_posix()
            file_lines = lines.setdefault(path, {})
            file_lines[line] = file_lines.get(line, 0) | query_bit
        return monitoring.DISABLE

    def start_query(index):
        nonlocal query_bit
        query_bit = 1 << index
        monitoring.restart_events()

    with redirect_stdout(io.StringIO()):
        sys.path.insert(0, str(source))
        monitoring.use_tool_id(tool_id, "tpcds probe coverage")
        try:
            monitoring.register_callback(tool_id, monitoring.events.LINE, line_hit)
            monitoring.set_events(tool_id, monitoring.events.LINE)
            run_probe(inputs_path, on_query=start_query)
        finally:
            monitoring.set_events(tool_id, 0)
            monitoring.register_callback(tool_id, monitoring.events.LINE, None)
            monitoring.free_tool_id(tool_id)
            sys.path.pop(0)

    observed = 0
    for file_lines in lines.values():
        for hits in file_lines.values():
            observed |= hits
    if observed != (startup_bit << 1) - 1:
        raise RuntimeError("coverage must include probe startup and every query")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13) or os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("use the Dockerfile's CPython 3.13 image with PYTHONHASHSEED=0")

    archive_path = ROOT / "sqlglot_src.tgz"
    inputs_path = ROOT / "tpcds_inputs.json"
    inputs = inputs_path.read_bytes()
    query_count = len(json.loads(inputs)["queries"])
    with TemporaryDirectory() as directory:
        source = Path(directory).resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(source, filter="data")
        lines = record_lines(source, inputs_path, query_count)

    data = {
        "version": INDEX_VERSION,
        "inputs_sha256": hashlib.sha256(inputs).hexdigest(),
        "source_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "query_count": query_count,
        "lines": lines,
    }
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    args.output.write_bytes(gzip.compress(encoded, mtime=0))
    print(f"{query_count} queries, {len(lines)} files, {sum(map(len, lines.values()))} lines")


if __name__ == "__main__":
    main()
