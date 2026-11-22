"""Collect comparison receipts from real spawned ProcessPoolExecutor tasks."""

from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import _ExecutorManagerThread
from dataclasses import dataclass, field
from multiprocessing.queues import Queue
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
import pickle
import threading
import weakref


_recorder = None


def _initialize_worker(root):
    from tools.record_comparisons import Recorder

    global _recorder
    _recorder = Recorder(Path(root), ())
    _recorder.install()


class _Call:
    def __init__(self, fn, args, kwargs):
        self.payload = fn, args, kwargs

    def __reduce__(self):
        # Let the pool's feeder serialize the original arguments. Delay loading
        # them in the child until the task's comparison recorder is active.
        return bytes, (bytes(ForkingPickler.dumps(self.payload)),)


class _Result:
    def __init__(self, payload):
        self.payload = payload

    def __reduce__(self):
        # Future.result() still receives the original value.
        return pickle.loads, (self.payload,)


def _record_call(payload, receipt):
    recorder = _recorder
    recorder.running = receipt
    recorder.current = {}
    try:
        fn, args, kwargs = pickle.loads(payload)
        value = fn(*args, **kwargs)
        result = _Result(bytes(ForkingPickler.dumps(value)))
        del value, fn, args, kwargs
        if not recorder.external:
            try:
                with open(receipt, "xb") as handle:
                    pickle.dump(recorder.worker_report(), handle)
            except OSError:
                pass
        return result
    finally:
        recorder.running = recorder.current = None
        recorder.pending.clear()
        recorder.external.clear()


@dataclass
class _Pool:
    jobs: list = field(default_factory=list)
    processes: set = field(default_factory=set)
    complete: bool = True


class WorkerRecordings:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.pools = weakref.WeakKeyDictionary()
        self.groups = []
        self.job_count = 0

    def submit(self, recorder, submit, pool, fn, args, kwargs):
        """Preserve stock scheduling and keep any pool we cannot fully observe."""
        group = self.pools.get(pool)
        in_test = recorder.current is not None and threading.get_ident() == recorder.main_thread
        if group is None and in_test and self.can_trace(pool):
            group = self.pools[pool] = _Pool()
            self.groups.append(group)
            pool._initializer = _initialize_worker
            pool._initargs = (str(recorder.root),)
        if group is None or not in_test or not group.complete:
            if group is not None:
                group.complete = False
            recorder.mark_external()
            return submit(pool, fn, *args, **kwargs)

        receipt = self.directory / f"{self.job_count}.pickle"
        self.job_count += 1
        group.jobs.append((recorder.running, receipt))
        try:
            return submit(pool, _record_call, _Call(fn, args, kwargs), str(receipt))
        finally:
            group.processes.update((pool._processes or {}).values())

    @staticmethod
    def can_trace(pool):
        # A started worker, custom initializer or replacement worker can run
        # application code outside the receipts.
        return (
            type(pool) is ProcessPoolExecutor
            and pool._processes == {}
            and pool._mp_context.get_start_method() == "spawn"
            and pool._initializer is None
            and pool._max_tasks_per_child is None
        )

    def is_pool_thread(self, thread):
        if type(thread) is _ExecutorManagerThread:
            return thread.executor_reference() in self.pools
        manager = threading.current_thread()
        return (
            type(manager) is _ExecutorManagerThread
            and manager.executor_reference() in self.pools
            and thread._target is Queue._feed
        )

    def merge(self, recorder):
        confirmed = 0
        for group in self.groups:
            complete = group.complete and all(process.exitcode == 0 for process in group.processes)
            for nodeid, receipt in group.jobs:
                try:
                    with receipt.open("rb") as handle:
                        report = pickle.load(handle)
                except (OSError, EOFError, pickle.UnpicklingError):
                    complete = False
                else:
                    recorder.merge_worker_report(nodeid, report)
                    confirmed += 1
            if not complete:
                # Tests can share workers and their cached state.
                recorder.external.update(nodeid for nodeid, _ in group.jobs)
        print(f"Read {confirmed} of {self.job_count} spawned task recordings")
