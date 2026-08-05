from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from klipper_updater.errors import BusyError
from klipper_updater.lock import ExclusiveLock, exclusive

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is unavailable on Windows; the lock degrades to a no-op there "
    "(dev machine only - the tool runs on Linux)",
)


def test_acquire_records_who_holds_it(paths):
    lock = ExclusiveLock(paths)
    lock.acquire("build klipper/bttebb36")
    try:
        held = lock.holder()
        assert held is not None
        assert held["pid"] == os.getpid()
        assert held["label"] == "build klipper/bttebb36"
        assert isinstance(held["since"], float)
    finally:
        lock.release()


def test_release_clears_the_holder_record(paths):
    lock = ExclusiveLock(paths)
    lock.acquire("build")
    lock.release()
    assert lock.holder() is None


def test_context_manager_releases(paths):
    with exclusive(paths, "flash board") as lock:
        assert lock.holder() is not None
    assert ExclusiveLock(paths).holder() is None


def test_releases_even_when_the_body_raises(paths):
    with pytest.raises(RuntimeError):
        with exclusive(paths, "flash board"):
            raise RuntimeError("boom")
    assert ExclusiveLock(paths).holder() is None


def test_holder_of_a_missing_file_is_none(paths):
    assert ExclusiveLock(paths).holder() is None


def test_holder_of_a_corrupt_file_is_none(paths):
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.lock_file, "w", encoding="utf-8") as fh:
        fh.write("not json at all")
    assert ExclusiveLock(paths).holder() is None


def test_entering_without_acquiring_is_a_programming_error(paths):
    with pytest.raises(RuntimeError):
        with ExclusiveLock(paths):
            pass


def test_double_release_is_harmless(paths):
    lock = ExclusiveLock(paths)
    lock.acquire("x")
    lock.release()
    lock.release()


@posix_only
def test_a_second_process_is_refused_and_told_who_holds_it(paths, tmp_path):
    """The point of a *file* lock: the CLI and the agent are separate processes
    and both can build. An in-process mutex would not catch this."""
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {os.path.join(os.getcwd(), "src")!r})
        from klipper_updater.paths import Paths
        from klipper_updater.lock import exclusive
        p = Paths.from_env(env={{"KLIPPER_UPDATER_HOME": {str(paths.home)!r}}})
        with exclusive(p, "child build"):
            print("HELD", flush=True)
            time.sleep(30)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "HELD"

        with pytest.raises(BusyError) as exc:
            exclusive(paths, "my build")
        assert exc.value.code == "busy"
        assert exc.value.data["holder"]["label"] == "child build"
        assert "child build" in str(exc.value)
    finally:
        proc.kill()
        proc.wait(timeout=10)


@posix_only
def test_the_lock_is_released_when_the_holder_dies(paths, tmp_path):
    """flock is freed by the kernel on process death, so there are no stale locks
    to clean up after a crash - the failure mode a pidfile gets wrong."""
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {os.path.join(os.getcwd(), "src")!r})
        from klipper_updater.paths import Paths
        from klipper_updater.lock import exclusive
        p = Paths.from_env(env={{"KLIPPER_UPDATER_HOME": {str(paths.home)!r}}})
        # Keep the reference. An unassigned ExclusiveLock is freed at once,
        # closing its fd and releasing the flock - so the child would hold
        # nothing and this test would pass without testing anything.
        held = exclusive(p, "doomed build")
        print("HELD", flush=True)
        time.sleep(30)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "HELD"
    proc.kill()
    proc.wait(timeout=10)

    # The gap this test used to leave open: it only proved a *new* acquire
    # worked, never what holder() said in the meantime. A SIGKILLed holder never
    # runs release(), so its {pid,label,since} record survives on disk even
    # though the kernel dropped the flock. Reporting that was a phantom
    # "a firmware operation is running on the host" that outlived both an agent
    # restart and a browser reload.
    assert ExclusiveLock(paths).holder() is None

    # No cleanup step, no stale-pid heuristic: it just works.
    with exclusive(paths, "next build") as lock:
        assert lock.holder()["label"] == "next build"


def test_lock_file_lives_with_the_runtime_state(paths):
    assert paths.lock_file.endswith(os.path.join("mcu-updater", ".updater.lock"))
    assert paths.lock_file.startswith(paths.data_dir), "state does not belong in config/"
    with exclusive(paths, "x"):
        assert os.path.exists(paths.lock_file)
        data = json.load(open(paths.lock_file, encoding="utf-8"))
        assert data["label"] == "x"


def test_the_busy_message_names_the_incumbent_and_its_age(paths):
    """Covered directly because the contention test can only run on POSIX, and a
    'resource busy' with no explanation is a bad experience on any platform."""
    import time as _time

    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.lock_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "label": "update-all", "since": _time.time() - 90}, fh)

    err = ExclusiveLock(paths)._busy()
    assert isinstance(err, BusyError)
    assert "update-all" in str(err)
    assert "90s ago" in str(err)
    assert err.data["holder"]["pid"] == 4242


def test_the_busy_message_copes_with_an_unidentifiable_holder(paths):
    err = ExclusiveLock(paths)._busy()
    assert "already running" in str(err)
    assert err.data["holder"] == {}


@posix_only
def test_a_leftover_record_with_no_lock_held_is_not_reported(paths):
    """Exactly what kill -9 leaves behind: the payload without the flock."""
    os.makedirs(os.path.dirname(paths.lock_file), exist_ok=True)
    with open(paths.lock_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "label": "flash 230048001750304158373620-if00",
                   "since": time.time()}, fh)

    assert ExclusiveLock(paths).holder() is None


@posix_only
def test_a_leftover_record_is_cleared_from_disk_not_just_hidden(paths):
    """Self-healing matters: the panel has to recover without a restart, and the
    next reader must not have to re-derive that it was stale."""
    os.makedirs(os.path.dirname(paths.lock_file), exist_ok=True)
    with open(paths.lock_file, "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "label": "flash abc", "since": time.time()}, fh)

    ExclusiveLock(paths).holder()

    with open(paths.lock_file, encoding="utf-8") as fh:
        assert fh.read().strip() == ""


@posix_only
def test_a_genuinely_held_lock_is_still_reported(paths):
    """The probe must not report a live holder as free - that would let two
    flashes run at once, which is worse than the phantom it fixes."""
    with exclusive(paths, "real build"):
        held = ExclusiveLock(paths).holder()
        assert held is not None
        assert held["label"] == "real build"


@posix_only
def test_a_probe_does_not_make_a_concurrent_acquire_report_busy(paths):
    """holder() takes the lock for microseconds to test it. acquire() retries
    once so that window cannot surface as a spurious refusal."""
    for _ in range(50):
        ExclusiveLock(paths).holder()
        with exclusive(paths, "build") as lock:
            assert lock.label == "build"
