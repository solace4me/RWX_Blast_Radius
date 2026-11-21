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
   query, including startup separately. The selector can test a late query
   early when coverage points to it. Only a difference ends the probe early.
   A match, timeout or unusable result still triggers the complete comparison
   in the original query order.

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
