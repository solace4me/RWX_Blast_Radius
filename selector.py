"""Blast Radius selector, v0: run every test, but split them optimally.

For each worker count 1..8, pack tests longest-first onto the least-loaded
worker, and keep the count with the lowest cost (busiest worker + 2 s/worker).
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
WORKER_COST_MS = 2000
MAX_WORKERS = 8


def pack(tests, count):
    workers = [[] for _ in range(count)]
    loads = [0] * count
    for number, ms in tests:
        i = loads.index(min(loads))
        workers[i].append(number)
        loads[i] += ms
    return workers, max(loads)


def best_split(tests):
    """tests: list of (number, ms). Returns the cheapest list of workers."""
    if not tests:
        return []
    tests = sorted(tests, key=lambda t: t[1], reverse=True)
    best = None
    for count in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers, busiest = pack(tests, count)
        cost = busiest + WORKER_COST_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    timed = [(t["number"], round(t["seconds"] * 1000)) for t in tests]
    workers = best_split(timed)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"all {len(tests)} tests on {len(workers)} workers", file=sys.stderr)


if __name__ == "__main__":
    main()
