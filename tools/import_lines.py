import importlib, pkgutil, sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__": continue
    try: importlib.import_module(info.name)
    except Exception as e: print("skip", info.name, e)
from sqlglot.dialects.dialect import Dialect
for n in list(Dialect.classes) if hasattr(Dialect,'classes') else []:
    pass
import sqlglot.dialects as d
for name in dir(d):
    try: getattr(d, name)
    except Exception: pass
