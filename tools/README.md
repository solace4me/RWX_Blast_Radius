# How map.json.gz is built

Everything comes from sqlglot at the pinned commit `eb11aec4`. No sample answers are used.

1. Run sqlglot's test suite with `SKIP_INTEGRATION=0` and the `brplugin.py` pytest plugin.
   The plugin records per-test line coverage of `sqlglot/` and every file each test opens.
   It runs the work that the tests send to process pools in-process (with a pickle round-trip),
   so coverage also sees it.
2. `buildmap.py` combines that into: line → tests, fixture → tests, function-body line ranges
   (from the AST), dialect inheritance, and which tests mention each dialect.
