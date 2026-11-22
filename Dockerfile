# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY sqlglot_src.tgz tpcds_probe.py tpcds_inputs.json /app/
COPY tools/record_lazy_import.py /app/
# Pinned sqlglot sources for the probes, precompiled with checked-hash
# bytecode: Python re-validates each .pyc against its source on every import.
# tpcds_base.json: the pinned commit's fingerprints of what test_execute_tpcds
# feeds the executor (see tpcds_probe.py).
RUN mkdir -p /app/src \
 && tar -xzf /app/sqlglot_src.tgz -C /app/src \
 && python -m compileall -q --invalidation-mode checked-hash /app/src \
 && cd /tmp && python -B /app/record_lazy_import.py /app/src > /app/cold_imports.json \
 && cd /tmp && PYTHONHASHSEED=0 PYTHONPATH=/app/src python -B /app/tpcds_probe.py /app/tpcds_inputs.json > /app/tpcds_base.json \
 && python -c "import json; assert len(json.load(open('/app/tpcds_base.json'))) == 99" \
 && chmod -R a+rX /app
COPY selector.py map.json.gz tpcds_types.json setting_index.py setting_reads.json.gz \
     tpcds_query_index.py tpcds_query_lines.json.gz dictkey_index.py dict_keys.json.gz \
     compare_index.py comparison_reads.json.gz \
     expr_validation_index.py expr_validation_reads.json.gz /app/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
