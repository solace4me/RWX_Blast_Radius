"""Experimental all-or-nothing replay of small Blast Radius schedules.

The selector first produces its usual JSONL. This tool takes those schedules,
test metadata and patches, then uses a local locked SQLGlot checkout to check
eligible groups. It receives no expected answers and is separate from the
submission image.
"""

import argparse
from dataclasses import dataclass
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path, PurePosixPath
import signal
import sys
import tempfile
import threading
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from score import _duration_milliseconds
from selector import apply_hunks, change_regions, original_source, parse_diff, scheduled_cost_ms


PHASES = {"setup", "call", "teardown"}
EXTERNAL_EVENTS = {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec"}


class PassingGroup:
    """Only a genuine pass of the entire selected group can clear it."""

    def __init__(self, expected, original_path=None):
        self.expected = set(expected)
        self.collected = []
        self.phases = {}
        self.bad = False
        self.original_path = original_path

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.expected or not report.passed or hasattr(report, "wasxfail"):
            self.bad = True
        else:
            self.phases.setdefault(report.nodeid, set()).add(report.when)

    def unsupported(self, message="test uses work outside the replay process"):
        self.bad = True
        raise RuntimeError(message)

    def audit(self, event, args):
        if event in EXTERNAL_EVENTS:
            self.unsupported()
        if event == "open" and self.original_path and isinstance(args[0], (str, bytes)):
            name = os.path.basename(os.fsdecode(args[0]))
            if name == self.original_path.name or (
                name.endswith(".pyc") and name.startswith(self.original_path.stem + ".")
            ):
                path = Path(os.fsdecode(args[0])).resolve()
                if path == self.original_path or path.parent == self.original_path.parent / "__pycache__":
                    self.unsupported("test reads the original copy of the modified module")

    def confirmed(self):
        return (
            not self.bad
            and len(self.collected) == len(self.expected)
            and set(self.collected) == self.expected
            and all(self.phases.get(nodeid) == PHASES for nodeid in self.expected)
        )


@dataclass(frozen=True)
class Offer:
    file: str
    original: str
    patched: str
    nodeids: tuple[str, ...]
    simulated_ms: int


def valid_nodeid(nodeid):
    file, separator, name = nodeid.partition("::")
    return (
        bool(separator and name) and file.startswith("tests/") and file.endswith(".py")
        and ".." not in PurePosixPath(file).parts and "\\" not in file
    )


def offer_group(workers, patch, ids, ms, source, *, group_limit, total_ms):
    """Offer only an entire small group after one validated runtime source edit."""
    if len(workers) != 1 or not 0 < len(workers[0]) <= group_limit:
        return None
    numbers = workers[0]
    if len(numbers) != len(set(numbers)) or any(n not in ids or n not in ms for n in numbers):
        return None
    if sum(ms[n] for n in numbers) > total_ms:
        return None
    nodeids = tuple(ids[n] for n in numbers)
    if not all(valid_nodeid(nodeid) for nodeid in nodeids):
        return None
    changes = parse_diff(patch)
    if len(changes) != 1:
        return None
    [change] = changes
    file = change.path
    if (
        change.added or change.deleted or change.renamed or change.binary or not change.hunks
        or not file.startswith("sqlglot/") or not file.endswith(".py")
        or ".." in PurePosixPath(file).parts or "\\" in file
    ):
        return None
    old = source(file)
    if old is None:
        return None
    new = apply_hunks(old, change.hunks)
    if new is None:
        return None
    try:
        compile(new, file, "exec", dont_inherit=True)
        regions = change_regions(old, new, change.hunks)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if not regions or any(kind != "runtime" for kind, _ in regions):
        return None
    return Offer(file, old, new, nodeids, scheduled_cost_ms(workers, ms))


@dataclass(frozen=True)
class Replay:
    status: str
    child_cpu: float
    wall_time: float


class PatchedModule(importlib.abc.MetaPathFinder):
    """Load the one changed module from the patch in a fresh import graph."""

    def __init__(self, file, pinned, patched):
        self.module = file[:-3].replace("/", ".").removesuffix(".__init__")
        self.path = (pinned / file).resolve()
        self.patched = patched
        self.loaded = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.module:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or Path(spec.origin).resolve() != self.path:
            return None
        overlay = self

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, fullname):
                code = compile(overlay.patched, self.path, "exec", dont_inherit=True)
                overlay.loaded = True
                return code

        spec.loader = Loader(fullname, spec.origin)
        return spec


def child_passed(pytest, pinned, directory, offer):
    overlay = PatchedModule(offer.file, pinned, offer.patched)
    sys.meta_path.insert(0, overlay)
    tracker = PassingGroup(offer.nodeids, original_path=overlay.path)
    # The audit hook stays local to this forked child.
    sys.addaudithook(tracker.audit)
    threading.Thread.start = lambda thread, *args, **kwargs: tracker.unsupported()
    os.chdir(pinned)
    # Neither the warm parent nor a prior replay imports SQLGlot.
    sys.path.insert(0, str(pinned))
    sys.dont_write_bytecode = True
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    code = pytest.main([
        "-q", "-x", "-p", "no:cacheprovider", "--basetemp", str(directory / "pytest"),
        *offer.nodeids,
    ], plugins=[tracker])
    return code == pytest.ExitCode.OK and tracker.confirmed() and overlay.loaded


def replay_group(pytest, pinned, offer, cpu_limit, wall_limit):
    start = time.monotonic()
    if (
        not hasattr(os, "wait4") or not hasattr(signal, "ITIMER_PROF")
        or threading.active_count() != 1
        or any(name == "sqlglot" or name.startswith(("sqlglot.", "tests.")) for name in sys.modules)
    ):
        return Replay("unsupported parent state", 0.0, 0.0)
    try:
        if (pinned / offer.file).read_text("utf-8") != offer.original:
            return Replay("source mismatch", 0.0, time.monotonic() - start)
    except OSError:
        return Replay("missing source", 0.0, time.monotonic() - start)
    with tempfile.TemporaryDirectory(prefix="blast-replay-", dir="/tmp") as directory:
        read_fd, write_fd = os.pipe()
        try:
            pid = os.fork()
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            return Replay("fork failed", 0.0, time.monotonic() - start)
        if pid == 0:
            os.close(read_fd)
            try:
                os.setsid()
                with open(os.devnull, "wb") as silent:
                    os.dup2(silent.fileno(), 1)
                    os.dup2(silent.fileno(), 2)
                signal.signal(signal.SIGPROF, signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF, cpu_limit)
                if child_passed(pytest, pinned, Path(directory), offer):
                    os.write(write_fd, b"passed")
                    os._exit(0)
            except BaseException:
                pass
            os._exit(1)
        os.close(write_fd)
        try:
            deadline = start + wall_limit
            timed_out = False
            while True:
                finished, status, usage = os.wait4(pid, os.WNOHANG)
                if finished:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        if os.getpgid(pid) == pid:
                            os.killpg(pid, signal.SIGKILL)
                        else:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, status, usage = os.wait4(pid, 0)
                    break
                time.sleep(0.005)
            os.set_blocking(read_fd, False)
            try:
                verified = os.read(read_fd, 6) == b"passed"
            except BlockingIOError:
                verified = False
            if verified and not timed_out and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
                outcome = "pass"
            elif timed_out:
                outcome = "wall limit"
            elif os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGPROF:
                outcome = "CPU limit"
            else:
                outcome = "unconfirmed"
        finally:
            os.close(read_fd)
    return Replay(outcome, usage.ru_utime + usage.ru_stime, time.monotonic() - start)


def experiment(args):
    start = time.process_time()
    catalog = json.loads(args.tests.read_text("utf-8"))["tests"]
    ids = {t["number"]: t["id"] for t in catalog}
    ms = {t["number"]: _duration_milliseconds(t["seconds"]) for t in catalog}
    pytest = None
    total_child_cpu = 0.0
    for row in map(json.loads, args.selection.read_text("utf-8").splitlines()):
        workers = row["workers"]
        commit = row["commit"]
        patch = (args.patches / f"{commit}.patch").read_text("utf-8")
        offer = offer_group(workers, patch, ids, ms, original_source,
                            group_limit=args.group_limit, total_ms=args.total_ms)
        if offer is not None:
            if pytest is None:
                import pytest
            replay = replay_group(pytest, args.pinned.resolve(), offer,
                                  min(args.cpu_limit, offer.simulated_ms / 1000), args.wall_limit)
            total_child_cpu += replay.child_cpu
            print(f"{commit}: {replay.status}, {replay.child_cpu:.3f}s CPU, "
                  f"{replay.wall_time:.3f}s wall, {offer.simulated_ms / 1000:.3f}s at stake",
                  file=sys.stderr, flush=True)
            if replay.status == "pass":
                workers = []
        print(json.dumps({"commit": commit, "workers": workers}), flush=True)
    print(f"REPLAY_LOOP_CPU_SECONDS {total_child_cpu + time.process_time() - start:.6f}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pinned", type=Path)
    parser.add_argument("patches", type=Path)
    parser.add_argument("tests", type=Path)
    parser.add_argument("selection", type=Path)
    parser.add_argument("--group-limit", type=int, default=4)
    parser.add_argument("--total-ms", type=int, default=200)
    parser.add_argument("--cpu-limit", type=float, default=1.5)
    parser.add_argument("--wall-limit", type=float, default=5.0)
    args = parser.parse_args()
    if min(args.group_limit, args.total_ms, args.cpu_limit, args.wall_limit) <= 0:
        parser.error("limits must be positive")
    experiment(args)


if __name__ == "__main__":
    main()
