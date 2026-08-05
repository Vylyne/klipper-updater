"""The mutation harness must never leave a mutation on disk.

This is cover for a real incident rather than a hypothetical one. An ad-hoc
version of `scripts/mutation_test.py` crashed *between* mutating a file and
restoring it - not in the mutation, but while decoding the test runner's output
on a console defaulting to cp1252 against a runner emitting UTF-8. The file
stayed broken, and only noticing stood between that and committing a silently
sabotaged guard.

So the property under test is not "does it mutate" but **"is the file always put
back"** - through exceptions, through Ctrl-C, and through a stale anchor.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "mutation_test.py"


def _load():
    """Import the script by path - scripts/ is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("mutation_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutation_test = _load()


ORIGINAL = "if state == OFFLINE:\n    continue\n"


def write(path: pathlib.Path, text: str) -> None:
    """Byte-exact, because `Path.write_text` translates \\n to \\r\\n on Windows.

    Which would make every LF anchor in this file miss - and is worth knowing
    about for real specs too: the harness matches bytes, so a CRLF checkout needs
    CRLF anchors.
    """
    path.write_bytes(text.encode("utf-8"))


def read(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.fixture
def target(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "guard.py"
    write(path, ORIGINAL)
    return path


def test_the_edit_is_applied_inside_the_block(target):
    with mutation_test.mutated(str(target), "    continue\n", ""):
        assert read(target) == "if state == OFFLINE:\n"
    assert read(target) == ORIGINAL


def test_the_file_is_restored_after_an_exception(target):
    """The exact shape of the incident: something throws mid-block."""
    with pytest.raises(UnicodeDecodeError):
        with mutation_test.mutated(str(target), "    continue\n", ""):
            b"\x9d".decode("cp1252")

    assert read(target) == ORIGINAL


def test_the_file_is_restored_after_a_keyboard_interrupt(target):
    """KeyboardInterrupt is a BaseException, so `except Exception` would miss it.

    Ctrl-C during a slow suite is the likeliest way to interrupt a real run.
    """
    with pytest.raises(KeyboardInterrupt):
        with mutation_test.mutated(str(target), "    continue\n", ""):
            raise KeyboardInterrupt

    assert read(target) == ORIGINAL


def test_a_stale_anchor_is_refused_without_touching_the_file(target):
    """A guard that was reworded must not silently mutate nothing and report
    CAUGHT - and must certainly not damage the file on the way out."""
    with pytest.raises(LookupError):
        with mutation_test.mutated(str(target), "text that is not there", ""):
            pytest.fail("the block must not run")

    assert read(target) == ORIGINAL


def test_only_the_first_occurrence_is_replaced(target):
    """Mirrors what a hand-written patch does, so a spec's anchors stay honest:
    an ambiguous anchor should be fixed in the spec, not silently applied twice.
    """
    write(target, "x = 1\nx = 1\n")
    with mutation_test.mutated(str(target), "x = 1\n", "x = 2\n"):
        assert read(target) == "x = 2\nx = 1\n"


def test_a_failed_restore_is_loud_and_names_the_backup(target, monkeypatch):
    """The one outcome worse than not running this at all is restoring wrongly
    and saying nothing."""
    calls = []

    def flaky_digest(data: bytes) -> str:
        calls.append(data)
        # Truthful first (the "before" hash), a lie afterwards - so the
        # post-restore comparison fails the way a real bad restore would.
        return "before" if len(calls) == 1 else "different"

    monkeypatch.setattr(mutation_test, "_digest", flaky_digest)

    with pytest.raises(RuntimeError) as exc:
        with mutation_test.mutated(str(target), "    continue\n", ""):
            pass

    assert "FAILED TO RESTORE" in str(exc.value)
    assert ".bak" in str(exc.value), "the message must say where the original is"
    # The write itself still happened; it is the verification that objected.
    assert read(target) == ORIGINAL


def test_runner_output_that_is_not_console_encodable_does_not_raise():
    """The actual crash. A tick from pytest/vitest is UTF-8; a Windows console is
    cp1252; `subprocess.run(text=True)` would decode with the latter and throw.
    """
    code, output = mutation_test.run_once(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write('✓ ok\n'.encode('utf-8'))"],
        None,
    )
    assert code == 0
    assert "ok" in output


def test_undecodable_bytes_are_replaced_rather_than_fatal():
    code, output = mutation_test.run_once(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\x9d\xff raw\n')"],
        None,
    )
    assert code == 0
    assert "raw" in output


def test_a_red_baseline_refuses_to_report_anything(tmp_path, capsys):
    """Every mutation reads as CAUGHT against an already-failing suite, so the
    whole run would be a green light built on nothing."""
    spec = tmp_path / "spec.json"
    target = tmp_path / "guard.py"
    write(target, ORIGINAL)
    # json.dumps, not %r: a python repr uses single quotes and backslash-escapes
    # a Windows path, neither of which is JSON.
    write(
        spec,
        json.dumps(
            {
                "file": str(target),
                "command": [sys.executable, "-c", "raise SystemExit(1)"],
                "mutations": [{"name": "x", "find": "continue", "replace": ""}],
            }
        ),
    )

    assert mutation_test.main([str(spec)]) == 2
    assert "baseline FAILED" in capsys.readouterr().err
    assert read(target) == ORIGINAL


def test_the_script_runs_as_a_command():
    """It is invoked as ./scripts/mutation_test.py, so it has to work that way."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=60
    )
    assert proc.returncode == 0
    assert b"mutation" in proc.stdout.lower()
