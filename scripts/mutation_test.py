#!/usr/bin/env python3
"""Check that a guard is load-bearing by breaking it and watching a test fail.

A guard nothing exercises is decoration. The way to know the difference is to
remove it and see whether the suite notices - so this edits a file in place, runs
a command, and reports CAUGHT (the tests failed, so the guard is real) or
SURVIVED (they passed without it, so it is untested).

    ./scripts/mutation_test.py mutations/bulk.json

The spec is JSON so that needles can span lines without shell quoting:

    {
      "file": "src/klipper_updater/agent/methods.py",
      "command": ["python", "-m", "pytest", "tests/test_agent_bulk.py", "-q"],
      "mutations": [
        {
          "name": "offline exclusion",
          "find": "                if state == STATE_OFFLINE:\\n                    continue\\n",
          "replace": ""
        }
      ]
    }

WHY THIS IS A SCRIPT AND NOT SIX LINES INLINE
---------------------------------------------
Because the inline version left a deliberately broken file on disk. An ad-hoc
version of this crashed between mutating and restoring - not in the mutation, but
while *decoding the test runner's output*, on a Windows console defaulting to
cp1252 against a runner that emits UTF-8 check marks. The file stayed mutated,
and the only thing standing between that and a committed sabotage of a guard was
noticing.

Three properties follow from that, and they are the entire reason this file
exists:

* **The restore is in a `finally`.** Not at the end of the happy path. It runs
  through exceptions, through Ctrl-C, through anything short of SIGKILL.
* **Bytes in, bytes out.** The file is read and written as bytes and the command's
  output is decoded with `errors="replace"`. No decoding step can touch the source
  file's contents or throw while a mutation is live. That is what actually broke.
* **The restore is verified.** A hash is taken before and re-checked after, and a
  mismatch is a loud non-zero exit naming the backup - because a silent failure to
  restore is the one outcome worse than not running this at all.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from typing import Any


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


@contextlib.contextmanager
def mutated(path: str, find: str, replace: str) -> Iterator[None]:
    """Apply one edit for the duration of the block, then put the file back.

    Restores through any exception, including KeyboardInterrupt - a partially
    mutated tree is the failure mode this whole module exists to prevent.
    """
    with open(path, "rb") as fh:
        original = fh.read()
    before = _digest(original)

    needle = find.encode("utf-8")
    if needle not in original:
        raise LookupError(f"anchor not found in {path}: {find!r}")

    # A backup outside the tree, so even a crash between the write and the
    # restore leaves a recoverable copy that no editor or formatter will touch.
    with tempfile.NamedTemporaryFile(
        prefix="mutation-", suffix=".bak", delete=False
    ) as backup:
        backup.write(original)
        backup_path = backup.name

    with open(path, "wb") as fh:
        fh.write(original.replace(needle, replace.encode("utf-8"), 1))

    try:
        yield
    finally:
        # Unconditional, and the last thing to touch the file.
        with open(path, "wb") as fh:
            fh.write(original)

        with open(path, "rb") as fh:
            after = _digest(fh.read())
        if after != before:
            # Never swallowed: leaving a mutation on disk is worse than any
            # result this script could report.
            raise RuntimeError(
                f"FAILED TO RESTORE {path} (was {before}, now {after}). "
                f"The original is at {backup_path} - restore it before committing."
            )


def run_once(command: list[str], cwd: str | None) -> tuple[int, str]:
    """Run the test command, tolerating any bytes it emits.

    `text=True` would decode with the console's encoding, which on Windows is
    cp1252 and chokes on a UTF-8 tick from vitest or pytest. Decoding here, with
    replacement, means the runner's output can never crash the harness.
    """
    proc = subprocess.run(command, capture_output=True, cwd=cwd)
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, output


def summarise(output: str) -> str:
    """The last line that looks like a test-runner tally."""
    for line in reversed(output.strip().splitlines()):
        lowered = line.lower()
        if ("passed" in lowered or "failed" in lowered) and any(c.isdigit() for c in line):
            return line.strip()
    return "(no recognisable summary)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", help="JSON file describing the file, command and mutations")
    parser.add_argument(
        "--cwd", default=None, help="working directory for the test command (default: here)"
    )
    parser.add_argument(
        "-k", dest="only", default=None, help="run only mutations whose name contains this"
    )
    args = parser.parse_args(argv)

    with open(args.spec, encoding="utf-8") as fh:
        spec: dict[str, Any] = json.load(fh)

    path = spec["file"]
    command = spec["command"]
    mutations = spec["mutations"]
    if args.only:
        mutations = [m for m in mutations if args.only in m["name"]]

    # A baseline first. If the suite is already red, every mutation reads as
    # CAUGHT and the whole run means nothing.
    code, output = run_once(command, args.cwd)
    if code != 0:
        print(f"baseline FAILED before any mutation: {summarise(output)}", file=sys.stderr)
        print("fix the suite first - a red baseline makes every result meaningless.", file=sys.stderr)
        return 2
    print(f"baseline green | {summarise(output)}\n")

    survivors = []
    for mutation in mutations:
        name = mutation["name"]
        try:
            with mutated(path, mutation["find"], mutation["replace"]):
                code, output = run_once(command, args.cwd)
        except LookupError as exc:
            # The guard moved or was reworded: the mutation is stale, which is a
            # result too - it is not evidence the guard is tested.
            print(f"STALE    | {name:38} | {exc}")
            survivors.append(name)
            continue

        caught = code != 0
        if not caught:
            survivors.append(name)
        print(f"{'CAUGHT  ' if caught else 'SURVIVED'} | {name:38} | {summarise(output)}")

    print()
    if survivors:
        print(f"{len(survivors)} guard(s) not covered by the suite:", file=sys.stderr)
        for name in survivors:
            print(f"  - {name}", file=sys.stderr)
        return 1

    print(f"all {len(mutations)} guard(s) are load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
