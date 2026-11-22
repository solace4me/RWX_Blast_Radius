# How map.json.gz is built

Everything comes from sqlglot at the pinned commit `eb11aec4`. No sample answers are used.

1. Run sqlglot's test suite with `SKIP_INTEGRATION=0` and the `brplugin.py` pytest plugin.
   The plugin records per-test line coverage of `sqlglot/` and every file each test opens.
   It runs the work that the tests send to process pools in-process (with a pickle round-trip),
   so coverage also sees it.
2. `buildmap.py` combines that into: line → tests, fixture → tests, function-body line ranges
   (from the AST), dialect inheritance, and which tests mention each dialect.
3. `import_lines.py`, run under `coverage run --source=sqlglot`, records which lines run just from
   importing every sqlglot module. A change there affects every user of the module, not only the
   tests that happened to trigger the import.
4. `sqlglot_src.tgz` holds sqlglot's `*.py` files at the pinned commit. At run time the selector
   applies each patch to them in memory to:
   - recognise comment-only edits (identical token stream, so no tests),
   - recognise syntax errors (the module can't import, so all tests),
   - map every edited line to its whole enclosing statement (or the enclosing block for inserted
     statements) before looking up coverage.
5. `record_tpcds_types.py` runs the real TPC-DS integration test, including
   its worker processes, against the pinned checkout with `requirements.lock`.
   It saves the observed operand types and equality dispatch in
   `tpcds_types.json`. The selector can use these facts when the sole edit
   replaces `value is None` with `value == None`. Other edits follow the
   normal coverage and TPC-DS checks.
6. `record_settings.py` records reads of literal settings on dialect classes
   in the pinned suite. It distinguishes direct literal dictionary keys from
   reads of the whole setting. A read during import, class construction or
   known shared caching disables pruning for that setting. Tests with missing
   pass reports or unobserved subprocess work retain their baseline selection.
   The map includes the source hashes and the test catalog it was built for.
7. `record_tpcds_query_lines.py` records the pinned probe's source lines by
   query, including startup separately. The index binds the source archive,
   probe code and queries by hash. Only a difference ends the probe early.
   A match, timeout or unusable result triggers the complete comparison in
   order. The selector skips the probe if removing TPC-DS would save no
   simulated scheduling time.

To regenerate the setting map, create an empty trace directory and run from
the pinned checkout with CPython 3.13 and the locked test dependencies.
`KIT` is the absolute path to this repository and `TRACE` is the absolute
path to the trace directory.

```sh
PYTHONPATH="$KIT:$KIT/tools:${PYTHONPATH:-}" SKIP_INTEGRATION=0 \
  BR_SETTINGS_CATALOG="$KIT/sample/tests.json" BR_SETTINGS_TRACE="$TRACE" \
  python -B -m pytest -p record_settings -p no:cacheprovider -q tests
PYTHONPATH="$KIT:${PYTHONPATH:-}" python -B "$KIT/tools/record_settings.py" \
  "$TRACE" "$KIT/sample/tests.json" "$KIT/setting_reads.json.gz"
```
8. `record_dictkeys.py` (pytest plugin + merger) records which tests read the
   value under each key of every class-level literal dict setting in the
   dialect modules, nested classes included (e.g. `Snowflake.Tokenizer.NUMERIC_ESCAPES`).
   The dicts are wrapped while each module is compiled, before anything can
   read them. `d[k]` and `d.get(k)` count for key `k`; membership, `len` and
   truthiness are ignored, since a value-only edit can't change them. Any
   other use counts as reading every key. A read during a module import or
   class creation (sqlglot loads dialects lazily, so this can happen inside a
   test), outside a test, or on another thread blocks that key, or the whole
   setting. The selector narrows only an edit to one value under one existing
   key, after the import check passes; tests outside the recording keep the
   normal selection.

```sh
SKIP_INTEGRATION=0 BR_DICT_OUT=$TRACE PYTHONPATH="$KIT/tools:$PWD" python -B -m pytest -p record_dictkeys -q tests
python "$KIT/tools/record_dictkeys.py" "$TRACE" "$KIT/sample/tests.json" "$PWD" "$KIT/dict_keys.json.gz"
```

9. `record_lazy_import.py` captures the files read by the clean
   `import sqlglot` subprocess in the pinned lazy-load test. Docker builds
   this map from the pinned archive. The selector can omit the test when
   every change is a validated edit to an existing Python source outside
   that import's dependencies.
10. `record_comparisons.py` captures values and types observed at comparison
    sites during passing tests. The selector evaluates a single operator or
    literal change against those facts at runtime. Comparisons with
    unsupported operands, shared setup, imports or unobserved workers keep
    their normal selection.
11. `record_expr_validation.py` captures expression argument validation and
    metadata readers in the pinned suite. A single boolean change to an
    existing `arg_types` key can use this map after a successful import.
    Whole metadata readers, unverified tests and blocked observations keep
    their normal coverage.

Generate the comparison and validation maps from the pinned checkout with
CPython 3.13, the locked test dependencies and integration tests enabled.
Tests omitted from recording remain unverified.

```sh
SKIP_INTEGRATION=0 PYTHONPATH="$KIT:${PYTHONPATH:-}" \
  python -B "$KIT/tools/record_comparisons.py" "$KIT/sample/tests.json" \
  "$KIT/comparison_reads.json.gz" tests -k 'not test_max_nodes and not test_generator_memory'

SKIP_INTEGRATION=0 PYTHONPATH="$KIT:$KIT/tools:${PYTHONPATH:-}" \
  BR_EXPR_CATALOG="$KIT/sample/tests.json" BR_EXPR_TRACE="$TRACE" \
  python -B -m pytest -p record_expr_validation -p no:cacheprovider --assert=plain \
  --deselect=tests/test_parser.py::TestParser::test_max_nodes -q tests
PYTHONPATH="$KIT:${PYTHONPATH:-}" python -B "$KIT/tools/record_expr_validation.py" \
  "$TRACE" "$KIT/sample/tests.json" "$KIT/expr_validation_reads.json.gz"
```
