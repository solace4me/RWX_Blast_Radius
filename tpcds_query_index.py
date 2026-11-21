"""Coverage of the pinned TPC-DS probe, for choosing an early difference check."""

import gzip
import hashlib
import json
from pathlib import Path
import zlib


INDEX_VERSION = 1


class QueryIndex:
    def __init__(self, lines, query_count):
        self.lines = lines
        self.query_count = query_count
        self.startup_bit = 1 << query_count

    @classmethod
    def load(cls, index_path, inputs_path):
        """Read the optional index if it matches the exact ordered query fixture."""
        try:
            with gzip.open(index_path, "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            inputs = Path(inputs_path).read_bytes()
            if data["version"] != INDEX_VERSION or data["inputs_sha256"] != hashlib.sha256(inputs).hexdigest():
                return None

            query_count = len(json.loads(inputs)["queries"])
            if not query_count or data["query_count"] != query_count:
                return None
            lines = {}
            max_mask = (1 << (query_count + 1)) - 1
            for path, hits in data["lines"].items():
                file_lines = {int(line): mask for line, mask in hits.items()}
                if any(line < 1 or type(mask) is not int or not 0 < mask <= max_mask
                       for line, mask in file_lines.items()):
                    return None
                lines[path] = file_lines
            return cls(lines, query_count) if lines else None
        except (AttributeError, EOFError, KeyError, OSError, TypeError, ValueError, zlib.error):
            return None

    def affected_queries(self, runtime_lines):
        """Return queries covering changed regions, or None to go straight to full replay.

        A subset omits earlier query state. Only a difference can short-circuit the
        full ordered comparison.
        """
        hits = 0
        for path, changed in runtime_lines.items():
            file_lines = self.lines.get(path, {})
            file_hits = 0
            for line in changed:
                file_hits |= file_lines.get(line, 0)
            if not file_hits or file_hits & self.startup_bit:
                return None
            hits |= file_hits
        return tuple(index for index in range(self.query_count) if hits & (1 << index)) if hits else None
