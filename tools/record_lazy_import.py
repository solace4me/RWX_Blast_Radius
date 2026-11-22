"""Record the source dependencies of the pinned lazy-loading test's subprocess."""

import sys


opened = []


def record(event, arguments):
    if event == "open" and isinstance(arguments[0], (str, bytes)):
        opened.append(arguments[0])


sys.path.insert(0, sys.argv[1])
sys.addaudithook(record)
import sqlglot

if len(sqlglot.Dialect._classes) != 1:
    raise RuntimeError("pinned lazy-loading check did not pass")

modules = [vars(module).get("__file__") for module in tuple(sys.modules.values()) if module]
paths = opened.copy() + modules

import importlib.util
import json
import os
from pathlib import Path


root = Path(sys.argv[1]).resolve()
files = set()
for path in paths:
    if not path:
        continue
    path = Path(os.fsdecode(path)).resolve()
    if not path.is_relative_to(root):
        continue
    if path.suffix == ".pyc":
        try:
            path = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            # Retain the conservative fallback for an unrecognized read.
            print("[]")
            break
    files.add(path.relative_to(root).as_posix())
else:
    print(json.dumps(sorted(files)) if "sqlglot/__init__.py" in files else "[]")
