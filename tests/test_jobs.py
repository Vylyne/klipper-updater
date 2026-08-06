"""The single-slot job runner and its log ring buffer."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from klipper_updater.errors import BusyError, UpdaterError
from klipper_updater.jobs import CANCELLED, FAILED, RUNNING, SUCCEEDED, Job, JobRunner
from klipper_updater.lock import exclusive
from klipper_updater.settings import Settings


@pytest.fixture
def runner(paths, settings):
    changes: list[tuple[str, str]] = []
    lines: list = []
    r = JobRunner(
        paths,
        lambda: settings,
        on_job_change=lambda job: changes.append((job.id, job.state)),
        on_log_line=lambda job, line: lines.append((job.id, line)),
    )
    r.changes = changes  # type: ignore[attr-defined]
    r.lines = lines  # type: ignore[attr-defined]
    yield r
    r._cancel.set()
    r.wait(timeout=10)


# --------------------------------------------------------------------------
# the log ring buffer
# --------------------------------------------------------------------------


def test_log_lines_are_numbered_from_zero():
    job = Job("job-1", "build", {}, log_size=10)
    assert [job.append("stdout", str(i)).seq for i in range(3)] == [0, 1, 2]
    assert job.log_next == 3


def test_log_since_returns_the_requested_tail():
    job = Job("job-1", "build", {}, log_size=100)
    for i in range(10):
        job.append("stdout", f"line {i}")

    lines, served_from, log_next = job.log_since(7)
    assert [line.text for line in lines] == ["line 7", "line 8", "line 9"]
    assert served_from == 7
    assert log_next == 10


def test_log_since_zero_returns_everything():
    job = Job("job-1", "build", {}, log_size=100)
    for i in range(5):
        job.append("stdout", str(i))
    lines, served_from, _ = job.log_since(0)
    assert len(lines) == 5
    assert served_from == 0


def test_the_ring_buffer_evicts_and_reports_how_much(paths):
    """A long build overflows the buffer; the client must be told rather than
    shown a silently renumbered log."""
    job = Job("job-1", "build", {}, log_size=5)
    for i in range(12):
        job.append("stdout", f"line {i}")

    assert job.dropped == 7
    lines, served_from, log_next = job.log_since(0)
    # Only the last 5 survive, and they keep their original sequence numbers.
    assert [line.seq for line in lines] == [7, 8, 9, 10, 11]
    assert served_from == 7, "must report the oldest sequence it could actually serve"
    assert log_next == 12


def test_log_since_past_the_end_is_empty_not_an_error():
    job = Job("job-1", "build", {}, log_size=10)
    job.append("stdout", "only line")
    lines, served_from, log_next = job.log_since(50)
    assert lines == []
    assert log_next == 1


def test_sequence_numbers_survive_eviction():
    """Sequence continuity is what lets the panel detect a gap at all."""
    job = Job("job-1", "build", {}, log_size=3)
    for i in range(6):
        job.append("stdout", str(i))
    lines, _, _ = job.log_since(0)
    seqs = [line.seq for line in lines]
    assert seqs == sorted(seqs)
    assert seqs[-1] == job.log_next - 1


# --------------------------------------------------------------------------
# single slot
# --------------------------------------------------------------------------


def test_a_job_runs_and_reports_its_result(runner):
    job = runner.submit("build", {"name": "a"}, lambda ctx: {"ok": True})
    assert runner.wait(timeout=10)
    assert job.state == SUCCEEDED
    assert job.result == {"ok": True}
    assert job.finished is not None


def test_submitting_while_busy_raises_rather_than_queueing(runner):
    """Deliberately not a queue: a second firmware operation is never intended,
    and a silent queue makes 'why is my printer stopped' unexplainable."""
    release = threading.Event()
    first = runner.submit("build", {"name": "a"}, lambda ctx: release.wait(10) and None)

    try:
        with pytest.raises(BusyError) as exc:
            runner.submit("build", {"name": "b"}, lambda ctx: None)
        assert exc.value.data["current"]["id"] == first.id
    finally:
        release.set()
    assert runner.wait(timeout=10)


def test_the_slot_frees_up_after_completion(runner):
    runner.submit("build", {"name": "a"}, lambda ctx: None)
    assert runner.wait(timeout=10)
    assert runner.current() is None
    # A second job now succeeds.
    second = runner.submit("build", {"name": "b"}, lambda ctx: None)
    assert runner.wait(timeout=10)
    assert second.state == SUCCEEDED


def test_the_slot_frees_up_after_a_failure(runner):
    def boom(ctx):
        raise UpdaterError("nope")

    runner.submit("build", {}, boom)
    assert runner.wait(timeout=10)
    assert runner.current() is None
    assert runner.submit("build", {}, lambda ctx: None) is not None
    assert runner.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is unavailable on Windows, so the lock degrades to a no-op there. "
    "This is verified on Linux CI, which is the only place the agent runs.",
)
def test_a_cli_build_holding_the_lock_blocks_submission(runner, paths):
    """The lock is taken in submit, so the caller is told immediately instead of
    getting a job that dies a moment later."""
    with exclusive(paths, "build klipper/bttebb36"):
        with pytest.raises(BusyError) as exc:
            runner.submit("build", {"name": "a"}, lambda ctx: None)
    assert "bttebb36" in str(exc.value)


def test_the_lock_is_released_when_the_job_ends(runner, paths):
    runner.submit("build", {}, lambda ctx: None)
    assert runner.wait(timeout=10)
    # Nothing holds it now, so the CLI can build again.
    with exclusive(paths, "cli build"):
        pass


def test_the_lock_is_released_even_when_the_job_crashes(runner, paths):
    def boom(ctx):
        raise RuntimeError("unexpected")

    runner.submit("build", {}, boom)
    assert runner.wait(timeout=10)
    with exclusive(paths, "cli build"):
        pass


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------


def test_a_typed_error_keeps_its_stable_code(runner):
    from klipper_updater.errors import BuildError

    runner.submit("build", {}, lambda ctx: (_ for _ in ()).throw(BuildError("make failed")))
    assert runner.wait(timeout=10)
    job = runner.recent()[0]
    assert job.state == FAILED
    assert job.error["code"] == "build_failed"


def test_an_unexpected_exception_is_captured_not_lost(runner):
    """A crashed job must still finish, free the slot, and report something."""

    def boom(ctx):
        raise ZeroDivisionError("oops")

    runner.submit("build", {}, boom)
    assert runner.wait(timeout=10)
    job = runner.recent()[0]
    assert job.state == FAILED
    assert job.error["code"] == "internal"
    assert "ZeroDivisionError" in job.error["message"]


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------


def test_cancelling_a_build_sets_the_event_and_reports_immediate(runner):
    started = threading.Event()

    def slow(ctx):
        started.set()
        for _ in range(200):
            ctx.check_cancelled()
            time.sleep(0.02)
        return {}

    job = runner.submit("build", {}, slow)
    assert started.wait(5)
    res = runner.cancel(job.id)
    assert res == {"cancelling": True, "immediate": True}
    assert runner.wait(timeout=10)
    assert job.state == CANCELLED
    assert job.cancel_requested is True


def test_a_flash_reports_cancellation_as_deferred(runner):
    """Interrupting a flashtool write leaves a board with half an image, so a
    flash may only be cancelled between devices - and the UI has to say so."""
    release = threading.Event()
    job = runner.submit("flash", {}, lambda ctx: release.wait(10) and None)
    try:
        res = runner.cancel(job.id)
        assert res == {"cancelling": True, "immediate": False}
    finally:
        release.set()
    assert runner.wait(timeout=10)


def test_cancelling_an_unknown_job_is_reported_not_raised(runner):
    assert runner.cancel("job-999") == {"cancelling": False, "reason": "unknown_job"}


def test_cancelling_a_finished_job_is_reported(runner):
    job = runner.submit("build", {}, lambda ctx: None)
    assert runner.wait(timeout=10)
    res = runner.cancel(job.id)
    assert res["cancelling"] is False
    assert res["reason"] == "already_finished"


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


def test_state_transitions_are_notified(runner):
    runner.submit("build", {}, lambda ctx: None)
    assert runner.wait(timeout=10)
    states = [state for _, state in runner.changes]
    assert states[0] == RUNNING
    assert states[-1] == SUCCEEDED


def test_progress_steps_are_notified_and_logged(runner):
    def with_steps(ctx):
        ctx.step("one", 1, 2)
        ctx.step("two", 2, 2)
        return {}

    job = runner.submit("build", {}, with_steps)
    assert runner.wait(timeout=10)
    assert job.progress.step == "two"
    assert job.progress.index == 2
    assert any("one" in line.text for _, line in runner.lines)


def test_log_lines_are_forwarded_to_the_listener(runner):
    def chatty(ctx):
        for i in range(5):
            ctx.reporter("stdout", f"line {i}")
        return {}

    runner.submit("build", {}, chatty)
    assert runner.wait(timeout=10)
    texts = [line.text for _, line in runner.lines]
    assert "line 0" in texts and "line 4" in texts


def test_a_broken_log_listener_cannot_break_the_job(paths, settings):
    def bad_listener(job, line):
        raise RuntimeError("listener exploded")

    r = JobRunner(paths, lambda: settings, on_log_line=bad_listener)
    r.submit("build", {}, lambda ctx: ctx.reporter("stdout", "hi") or {})
    assert r.wait(timeout=10)
    assert r.recent()[0].state == SUCCEEDED


def test_recent_keeps_finished_jobs_newest_first(runner):
    for name in ("a", "b", "c"):
        runner.submit("build", {"name": name}, lambda ctx: None)
        assert runner.wait(timeout=10)
    assert [j.params["name"] for j in runner.recent()] == ["c", "b", "a"]


def test_log_ring_size_comes_from_settings(paths):
    r = JobRunner(paths, lambda: Settings(log_ring_size=4))
    job = r.submit("build", {}, lambda ctx: [ctx.reporter("stdout", str(i)) for i in range(20)])
    assert r.wait(timeout=10)
    lines, _, _ = job.log_since(0)
    assert len(lines) == 4
    assert job.dropped == 16


# --------------------------------------------------------------------------
# failures reaching the agent log
#
# A build that will not compile leaves nothing behind but the job's ring
# buffer, which lives in memory and is gone on the next restart. Only internal
# crashes used to be logged, so the failures a user actually hits - a bad
# .config, a PlatformIO error - were precisely the ones absent from
# mcu-updater.log.
# --------------------------------------------------------------------------


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, fmt: str, *args) -> None:
        self.errors.append(fmt % args if args else fmt)


@pytest.fixture
def logged_runner(paths, settings):
    log = RecordingLogger()
    r = JobRunner(paths, lambda: settings, logger=log)
    r.log = log  # type: ignore[attr-defined]
    yield r
    r._cancel.set()
    r.wait(timeout=10)


def test_an_expected_failure_is_written_to_the_agent_log(logged_runner):
    def boom(ctx):
        raise UpdaterError("PlatformIO build failed: pio exited 2.")

    logged_runner.submit("display_build", {"name": "knomi"}, boom)
    logged_runner.wait(timeout=10)

    assert len(logged_runner.log.errors) == 1
    record = logged_runner.log.errors[0]
    assert "display_build" in record
    assert "pio exited 2" in record


def test_the_log_record_carries_the_output_that_explains_it(logged_runner):
    """`pio exited 2` names the exit code, not the file that would not build."""

    def boom(ctx):
        ctx.reporter("stdout", "Compiling src/main.cpp")
        ctx.reporter("stderr", "src/main.cpp:42:3: error: 'fooo' was not declared")
        raise UpdaterError("PlatformIO build failed: pio exited 2.")

    logged_runner.submit("display_build", {"name": "knomi"}, boom)
    logged_runner.wait(timeout=10)

    record = logged_runner.log.errors[0]
    assert "'fooo' was not declared" in record


def test_a_successful_job_logs_nothing(logged_runner):
    logged_runner.submit("display_build", {}, lambda ctx: {"ok": True})
    logged_runner.wait(timeout=10)
    assert logged_runner.log.errors == []


def test_a_cancelled_job_is_not_reported_as_a_failure(logged_runner):
    """The user asked for it; it is not something to go and read about."""
    from klipper_updater.errors import OperationCancelled

    def cancelled(ctx):
        raise OperationCancelled("cancelled")

    logged_runner.submit("build", {}, cancelled)
    logged_runner.wait(timeout=10)
    assert logged_runner.log.errors == []


def test_an_internal_crash_still_logs_its_traceback(logged_runner):
    def boom(ctx):
        raise RuntimeError("unexpected")

    logged_runner.submit("build", {}, boom)
    logged_runner.wait(timeout=10)

    record = logged_runner.log.errors[0]
    assert "Traceback" in record
    assert "RuntimeError" in record


def test_the_tail_is_bounded_so_one_failure_cannot_flood_the_log():
    job = Job("job-1", "build", {}, log_size=2000)
    for i in range(500):
        job.append("stdout", f"line {i}")
    tail = job.tail()
    assert len(tail) == 40
    assert tail[-1].text == "line 499"


def test_a_runner_with_no_logger_still_completes_the_job(paths, settings):
    """The CLI constructs one without a logger; failing there must not crash."""
    r = JobRunner(paths, lambda: settings)
    job = r.submit("build", {}, lambda ctx: (_ for _ in ()).throw(UpdaterError("nope")))
    r.wait(timeout=10)
    assert job.state == FAILED
