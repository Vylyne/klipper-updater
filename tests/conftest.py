"""Shared fixtures.

Everything here leans on the single seam that makes this project testable:
``Paths.from_env`` honours ``KLIPPER_UPDATER_*`` env vars, so a fake root in a
tmp_path stands in for a whole printer host - no mocks, no monkeypatching of
``expanduser``, no hardware, and it all runs on Windows.
"""

from __future__ import annotations

import pathlib

import pytest

from klipper_updater.paths import Paths
from klipper_updater.settings import Settings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The real registry from the printer, committed at the repo root. Used directly
#: as a fixture so the test suite fails if that sample is ever broken.
LIVE_MCUS_CFG = REPO_ROOT / "mcu-updater.cfg"


@pytest.fixture(autouse=True)
def _instant_fake_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the dry-run log pacing.

    In production the fake build log replays at a realistic speed so the
    streaming UI is genuinely exercised. In tests that just makes the suite slow.
    """
    monkeypatch.setattr("klipper_updater.build.FAKE_BUILD_DELAY", 0.0)


@pytest.fixture
def fake_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A pretend ~ laid out the way a printer host is."""
    (tmp_path / "bus").mkdir()
    (tmp_path / "klipper" / "src").mkdir(parents=True)
    (tmp_path / "katapult" / "src").mkdir(parents=True)
    (tmp_path / "printer_data" / "comms").mkdir(parents=True)
    # Hand-edited config, and build artifacts, deliberately in separate trees.
    (tmp_path / "printer_data" / "config" / "mcu-updater").mkdir(parents=True)
    (tmp_path / "printer_data" / "mcu-updater").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def paths(fake_root: pathlib.Path) -> Paths:
    return Paths.from_env(
        env={
            "KLIPPER_UPDATER_HOME": str(fake_root),
            "KLIPPER_UPDATER_FAKE_BUS": str(fake_root / "bus"),
        }
    )


@pytest.fixture
def settings() -> Settings:
    """Defaults, but never touching a real service."""
    return Settings(service_backend="null", clean_before_build=False)


@pytest.fixture
def live_registry_text() -> str:
    return LIVE_MCUS_CFG.read_text(encoding="utf-8")


def cmd_tokens(cmd_line: str) -> list[str]:
    """Split an echoed command line into whole tokens.

    Never substring-match for a flag in one of these. The line contains absolute
    paths, and a temp directory can easily contain the characters you are looking
    for - GitHub's runners use ``/tmp/pytest-of-runner/...``, in which
    ``pytest-of-runner`` contains ``-r``. That made an
    ``assert not any("-r" in c)`` fail against the *directory name* while passing
    on Windows, where the path is ``pytest-of-Vi``.

    Whitespace splitting is enough: flags never contain spaces, so even a quoted
    path with a space in it cannot produce a false match.
    """
    return cmd_line.split()


def write_settings(paths: Paths, **values: object) -> None:
    """Set keys in the ``[updater]`` section of the shared config file.

    Settings and the registry live in one file, so this has to be a
    load-modify-write: a plain ``open(..., "w")`` deletes every ``[mcu ...]``
    section the fixture just wrote, and prepending a second ``[updater]`` block
    is refused as a duplicate section.
    """
    import os

    from klipper_updater.cfgdoc import CfgDocument

    text = ""
    if os.path.exists(paths.main_config):
        with open(paths.main_config, encoding="utf-8") as fh:
            text = fh.read()
    doc = CfgDocument(text)
    for key, value in values.items():
        doc.set("updater", key, value)
    os.makedirs(paths.config_dir, exist_ok=True)
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(doc.render())


def make_device(bus_dir: pathlib.Path, fw: str, chipset: str, serial: str) -> pathlib.Path:
    """Create a fake /dev/serial/by-id entry.

    Real ones are symlinks; a plain file is indistinguishable for our purposes
    since we only ever listdir and stat them.
    """
    p = bus_dir / f"usb-{fw}_{chipset}_{serial}"
    p.write_text("", encoding="utf-8")
    return p
