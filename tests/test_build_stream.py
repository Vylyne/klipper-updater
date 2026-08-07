"""run_streamed: the plumbing every build log depends on."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from mcu_updater.build import run_streamed
from mcu_updater.errors import OperationCancelled

from .conftest import cmd_tokens

CHILD_MANY = "for i in range(5000):\n    print(i)\n"
CHILD_SLOW = (
    "import time\n"
    "print('first', flush=True)\n"
    "time.sleep(1.0)\n"
    "print('last', flush=True)\n"
)
CHILD_FOREVER = (
    "import time\n"
    "i = 0\n"
    "while True:\n"
    "    print(i, flush=True)\n"
    "    i += 1\n"
    "    time.sleep(0.01)\n"
)
CHILD_BOTH_STREAMS = (
    "import sys\n"
    "print('to-stdout', flush=True)\n"
    "print('to-stderr', file=sys.stderr, flush=True)\n"
)


def _collect(lines: list[str]):
    def reporter(stream: str, line: str) -> None:
        if stream == "stdout":
            lines.append(line)

    return reporter


def test_every_line_is_delivered_in_order(tmp_path):
    got: list[str] = []
    rc = run_streamed(
        [sys.executable, "-c", CHILD_MANY],
        cwd=str(tmp_path),
        reporter=_collect(got),
    )
    assert rc == 0
    assert got == [str(i) for i in range(5000)]


def test_output_arrives_incrementally_not_at_exit(tmp_path):
    """A build log that only appears when make finishes is useless."""
    stamps: list[tuple[str, float]] = []

    def reporter(stream: str, line: str) -> None:
        if stream == "stdout":
            stamps.append((line, time.monotonic()))

    run_streamed([sys.executable, "-c", CHILD_SLOW], cwd=str(tmp_path), reporter=reporter)

    assert [s[0] for s in stamps] == ["first", "last"]
    # The child sleeps 1s between them; if we only got output at exit these
    # timestamps would be nearly identical.
    assert stamps[1][1] - stamps[0][1] > 0.5


def test_stderr_is_merged_into_the_stream(tmp_path):
    got: list[str] = []
    run_streamed(
        [sys.executable, "-c", CHILD_BOTH_STREAMS],
        cwd=str(tmp_path),
        reporter=_collect(got),
    )
    assert set(got) == {"to-stdout", "to-stderr"}


def test_nonzero_exit_is_reported(tmp_path):
    rc = run_streamed(
        [sys.executable, "-c", "raise SystemExit(3)"],
        cwd=str(tmp_path),
        reporter=lambda s, line: None,
    )
    assert rc == 3


def test_cancel_terminates_the_child_promptly(tmp_path):
    cancel = threading.Event()
    got: list[str] = []

    def reporter(stream: str, line: str) -> None:
        if stream != "stdout":
            return
        got.append(line)
        if len(got) >= 5:
            cancel.set()

    started = time.monotonic()
    with pytest.raises(OperationCancelled):
        run_streamed(
            [sys.executable, "-c", CHILD_FOREVER],
            cwd=str(tmp_path),
            reporter=reporter,
            cancel=cancel,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"cancel took {elapsed:.1f}s"
    assert len(got) >= 5


def test_cancel_is_responsive_even_when_the_child_is_silent(tmp_path):
    """The cancel check runs on a timer, not only when a line arrives.

    With a plain `for line in proc.stdout` loop a quiet child could never be
    cancelled at all.
    """
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()

    started = time.monotonic()
    with pytest.raises(OperationCancelled):
        run_streamed(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
            reporter=lambda s, line: None,
            cancel=cancel,
            poll=0.1,
        )
    assert time.monotonic() - started < 15


def test_dry_run_never_launches_the_command(tmp_path):
    got: list[str] = []
    cmds: list[str] = []

    def reporter(stream: str, line: str) -> None:
        if stream == "stdout":
            got.append(line)
        elif stream == "cmd":
            cmds.append(line)

    marker = tmp_path / "should-not-exist"
    rc = run_streamed(
        [sys.executable, "-c", f"open(r'{marker}', 'w').close()"],
        cwd=str(tmp_path),
        reporter=reporter,
        dry_run=True,
        fake_delay=0.0,
    )
    assert rc == 0
    assert not marker.exists()
    assert len(cmds) == 1
    # A realistic amount of log, so streaming/batching/autoscroll get exercised.
    assert len(got) > 100
    assert any("Linking" in line for line in got)


def test_the_command_is_echoed_before_running(tmp_path):
    cmds: list[str] = []
    run_streamed(
        [sys.executable, "-c", "pass"],
        cwd=str(tmp_path),
        reporter=lambda s, line: cmds.append(line) if s == "cmd" else None,
    )
    assert len(cmds) == 1
    assert "-c" in cmd_tokens(cmds[0])


def test_a_missing_executable_is_a_clean_tool_error(tmp_path):
    """A host without build-essential should get a sentence, not a traceback."""
    from mcu_updater.errors import ToolMissingError

    with pytest.raises(ToolMissingError) as exc:
        run_streamed(
            ["definitely-not-a-real-command-xyz"],
            cwd=str(tmp_path),
            reporter=lambda s, line: None,
        )
    assert exc.value.data["tool"] == "definitely-not-a-real-command-xyz"
    assert exc.value.code == "tool_missing"
