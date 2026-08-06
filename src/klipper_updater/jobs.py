"""Long-running operations, run one at a time.

A build takes minutes and a flash stops the printer's firmware, so this is a
**single slot, not a queue**. Submitting while something is running fails
immediately with `BusyError` naming the incumbent. A queue would be worse: the
user asks for one thing, walks away, and comes back to find a second operation
they'd forgotten they asked for started by itself.

The exclusive file lock is taken in `submit`, before the worker starts, so a CLI
build already in progress makes the RPC call fail synchronously with a useful
message rather than producing a job that dies a moment later.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import threading
import time
import traceback
from typing import Any, Callable, Optional

from .errors import BusyError, OperationCancelled, UpdaterError
from .lock import ExclusiveLock
from .paths import Paths
from .settings import Settings

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

#: Kinds whose in-flight work can be interrupted at any moment. A compile can:
#: killing make costs at worst a half-written object file that make will redo.
#: Anything that writes to a board cannot - interrupting flashtool leaves half an
#: image on it - so those kinds are cancellable only between boards, which their
#: job bodies check for themselves.
IMMEDIATELY_CANCELLABLE = ("build", "build_all")


@dataclasses.dataclass
class LogLine:
    seq: int
    stream: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        # Short keys: a build emits several hundred of these and they go out over
        # a websocket to every connected client.
        return {"i": self.seq, "s": self.stream, "t": self.text}


@dataclasses.dataclass
class Progress:
    step: str = ""
    index: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "index": self.index, "total": self.total}


class Job:
    """One unit of work, plus its log."""

    def __init__(self, job_id: str, kind: str, params: dict, log_size: int = 2000) -> None:
        self.id = job_id
        self.kind = kind
        self.params = params
        self.state: str = QUEUED
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.progress = Progress()
        self.result: Optional[dict] = None
        self.error: Optional[dict] = None
        self.cancel_requested = False

        self._lock = threading.Lock()
        self._log: collections.deque[LogLine] = collections.deque(maxlen=log_size)
        self._next_seq = 0
        #: Lines evicted by the ring buffer. The oldest index still retrievable.
        self._dropped = 0

    # -- log ---------------------------------------------------------------

    def append(self, stream: str, text: str) -> LogLine:
        with self._lock:
            line = LogLine(self._next_seq, stream, text)
            self._next_seq += 1
            if len(self._log) == self._log.maxlen:
                self._dropped += 1
            self._log.append(line)
            return line

    @property
    def log_next(self) -> int:
        with self._lock:
            return self._next_seq

    def log_since(self, from_seq: int = 0) -> tuple[list[LogLine], int, int]:
        """Lines from `from_seq` onward.

        Returns (lines, first_seq_served, next_seq). `first_seq_served` may be
        greater than `from_seq` when the ring buffer has already evicted what was
        asked for - the caller needs to know that so it can tell the user lines
        were dropped rather than silently renumbering.
        """
        with self._lock:
            snapshot = list(self._log)
            next_seq = self._next_seq
        if not snapshot:
            return [], max(from_seq, self._dropped), next_seq
        oldest = snapshot[0].seq
        start = max(from_seq, oldest)
        lines = [line for line in snapshot if line.seq >= start]
        return lines, start, next_seq

    @property
    def dropped(self) -> int:
        return self._dropped

    def tail(self, limit: int = 40) -> list[LogLine]:
        """The last few lines, for a post-mortem in the agent log.

        A failure message names the exit code, not the reason - `pio exited 2`
        says nothing about which file would not compile. That detail only ever
        existed in this ring buffer, which is in memory and gone on the next
        restart, so on failure the tail gets copied somewhere durable.
        """
        with self._lock:
            return list(self._log)[-max(limit, 0) :] if limit > 0 else []

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The job without its log - logs travel separately, and are large."""
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "state": self.state,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "duration": (
                (self.finished or time.time()) - self.started if self.started else None
            ),
            "progress": self.progress.to_dict(),
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "log_next": self.log_next,
            "log_dropped": self._dropped,
        }

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


@dataclasses.dataclass
class JobContext:
    """Handed to the function doing the work."""

    job: Job
    reporter: Callable[[str, str], None]
    #: Set when cancellation is requested. Build steps honour it immediately;
    #: flash steps must only check it between devices.
    cancel: threading.Event
    #: Never set. Passed to steps that must not be interrupted part-way.
    never_cancel: threading.Event
    step: Callable[..., None]

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise OperationCancelled("cancelled")


class JobRunner:
    def __init__(
        self,
        paths: Paths,
        settings_getter: Callable[[], Settings],
        *,
        on_job_change: Optional[Callable[[Job], None]] = None,
        on_log_line: Optional[Callable[[Job, LogLine], None]] = None,
        logger: Any = None,
        history: int = 10,
    ) -> None:
        self.paths = paths
        self._settings_getter = settings_getter
        self.on_job_change = on_job_change
        self.on_log_line = on_log_line
        self._log = logger

        self._ids = itertools.count(1)
        self._slot_lock = threading.Lock()
        self._current: Optional[Job] = None
        self._cancel = threading.Event()
        self._never_cancel = threading.Event()
        self._recent: collections.deque[Job] = collections.deque(maxlen=history)
        self._by_id: dict[str, Job] = {}
        self._thread: Optional[threading.Thread] = None

    # -- queries -----------------------------------------------------------

    def current(self) -> Optional[Job]:
        with self._slot_lock:
            return self._current

    def get(self, job_id: str) -> Optional[Job]:
        return self._by_id.get(job_id)

    def recent(self, limit: int = 10) -> list[Job]:
        return list(self._recent)[: max(limit, 0)]

    @property
    def busy(self) -> bool:
        return self.current() is not None

    # -- failure reporting -------------------------------------------------

    def _log_failure(self, job: Job, detail: str) -> None:
        """Write a failed job to the agent log, with enough of its output to act on.

        One record rather than a line per log line: the agent log is shared with
        everything else the daemon does, and a 40-line build failure interleaved
        with status polling is unreadable.
        """
        if self._log is None:
            return
        lines = job.tail()
        body = "\n".join(f"    {line.text}" for line in lines)
        self._log.error(
            "job %s (%s) failed: %s%s",
            job.id,
            job.kind,
            detail,
            f"\n  last {len(lines)} log line(s):\n{body}" if lines else "",
        )

    # -- submission --------------------------------------------------------

    def submit(
        self,
        kind: str,
        params: dict,
        fn: Callable[[JobContext], Optional[dict]],
    ) -> Job:
        """Start a job, or raise BusyError. Returns immediately.

        The lock is acquired here rather than in the worker so the caller learns
        about a competing CLI build straight away, in the reply to their request.
        """
        settings = self._settings_getter()

        with self._slot_lock:
            if self._current is not None:
                raise BusyError(
                    f"a {self._current.kind} job is already running ({self._current.id}).",
                    current=self._current.to_dict(),
                )

            lock = ExclusiveLock(self.paths)
            # Raises BusyError naming the holder if the CLI is mid-build.
            lock.acquire(f"{kind} ({', '.join(f'{k}={v}' for k, v in params.items())})")

            job = Job(f"job-{next(self._ids)}", kind, dict(params), settings.log_ring_size)
            job.state = RUNNING
            job.started = time.time()
            self._current = job
            self._by_id[job.id] = job
            self._cancel.clear()

        self._emit_change(job)

        def reporter(stream: str, text: str) -> None:
            line = job.append(stream, text)
            if self.on_log_line is not None:
                try:
                    self.on_log_line(job, line)
                except Exception:  # noqa: BLE001 - telemetry must not break work
                    pass

        def step(label: str, index: int = 0, total: int = 0) -> None:
            job.progress = Progress(label, index, total)
            reporter("info", label)
            self._emit_change(job)

        ctx = JobContext(
            job=job,
            reporter=reporter,
            cancel=self._cancel,
            never_cancel=self._never_cancel,
            step=step,
        )

        def worker() -> None:
            try:
                result = fn(ctx)
                job.result = result or {}
                job.state = SUCCEEDED
            except OperationCancelled as exc:
                job.state = CANCELLED
                job.error = {"code": exc.code, "message": str(exc), "data": exc.data}
                reporter("warn", "cancelled")
            except UpdaterError as exc:
                job.state = FAILED
                job.error = exc.to_dict()
                reporter("error", str(exc))
                # An expected failure is still a failure, and this is the one
                # that needs reading: a build that will not compile leaves
                # nothing behind but the job's ring buffer, which is in memory
                # and gone on the next restart. Only internal crashes used to be
                # logged, so the failures users actually hit were the ones
                # absent from mcu-updater.log.
                self._log_failure(job, str(exc))
            except BaseException as exc:  # noqa: BLE001
                job.state = FAILED
                job.error = {
                    "code": "internal",
                    "message": f"{type(exc).__name__}: {exc}",
                    "data": {},
                }
                reporter("error", f"internal error: {type(exc).__name__}: {exc}")
                self._log_failure(job, traceback.format_exc())
            finally:
                job.finished = time.time()
                try:
                    lock.release()
                except Exception:  # noqa: BLE001
                    pass
                with self._slot_lock:
                    self._current = None
                    self._recent.appendleft(job)
                self._emit_change(job)

        thread = threading.Thread(target=worker, name=f"job-{job.id}", daemon=True)
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            # Never leave the slot or the lock held if the thread won't start.
            lock.release()
            with self._slot_lock:
                self._current = None
            job.state = FAILED
            raise

        return job

    # -- cancellation ------------------------------------------------------

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cancellation.

        Whether it takes effect immediately depends on the kind - see
        IMMEDIATELY_CANCELLABLE. A flash is only interruptible between devices,
        because stopping a write part-way leaves a board with half an image.
        """
        job = self._by_id.get(job_id)
        if job is None:
            return {"cancelling": False, "reason": "unknown_job"}
        if job.is_terminal:
            return {"cancelling": False, "reason": "already_finished", "state": job.state}

        job.cancel_requested = True
        self._cancel.set()
        immediate = job.kind in IMMEDIATELY_CANCELLABLE
        self._emit_change(job)
        return {"cancelling": True, "immediate": immediate}

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the current job finishes. For tests and shutdown."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _emit_change(self, job: Job) -> None:
        if self.on_job_change is None:
            return
        try:
            self.on_job_change(job)
        except Exception:  # noqa: BLE001
            pass
