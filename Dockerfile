# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py map.json.gz sqlglot_src.tgz /app/
# Pinned sqlglot sources for the import probe, precompiled with checked-hash
# bytecode: Python re-validates each .pyc against its source on every import.
RUN mkdir -p /app/src \
 && tar -xzf /app/sqlglot_src.tgz -C /app/src \
 && python -m compileall -q --invalidation-mode checked-hash /app/src \
 && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
