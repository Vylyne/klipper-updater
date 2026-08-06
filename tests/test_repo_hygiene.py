"""Things that are true of the repo rather than of the code.

Small, but this is the second "documented and broken" bug of its kind: the README
says to run `./scripts/mutation_test.py` and the file was not executable, so the
command in the docs failed with Permission denied for anyone who copied it.

The Windows dev box cannot notice - `os.access(path, os.X_OK)` is meaningless
there and the working tree carries no mode bits - so the check has to ask git,
which stores the bit either way.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tracked_modes() -> dict[str, str]:
    """Path -> git mode, e.g. "100755". Empty if this isn't a git checkout."""
    git = shutil.which("git")
    if git is None:
        return {}
    try:
        out = subprocess.run(
            [git, "ls-files", "-s"], cwd=REPO_ROOT, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    modes = {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        # "100755 <sha> 0\tpath"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if path and parts:
            modes[path] = parts[0]
    return modes


def test_every_script_with_a_shebang_is_executable():
    """A shebang is a promise that `./the/script` works. Keep it."""
    modes = _tracked_modes()
    if not modes:
        pytest.skip("not a git checkout, or git unavailable")

    offenders = []
    for path, mode in sorted(modes.items()):
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        try:
            first = full.open("rb").readline()
        except OSError:
            continue
        if first.startswith(b"#!") and mode != "100755":
            offenders.append(f"{path} (mode {mode})")

    assert offenders == [], (
        "these declare a shebang but are not executable, so `./<path>` fails:\n  "
        + "\n  ".join(offenders)
        + "\nFix with: git update-index --chmod=+x <path>"
    )


def test_the_check_can_actually_see_the_repo():
    """Guards the test above: an empty mode map would make it vacuously pass, and
    it is skipped rather than failed when git is missing - so assert that on a
    normal checkout it really did find files."""
    modes = _tracked_modes()
    if not modes:
        pytest.skip("not a git checkout, or git unavailable")
    assert len(modes) > 20
    assert any(path.endswith(".py") for path in modes)
