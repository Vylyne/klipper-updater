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
LIVE_MCUS_JSON = REPO_ROOT / "mcus.json"


@pytest.fixture(autouse=True)
def _instant_fake_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the dry-run log pacing.

    In production the fake build log replays at a realistic speed so the
    streaming UI is genuinely exercised. In tests that just makes the suite slow.
    """
    monkeypatch.setattr("klipper_updater.build.FAKE_BUILD_DELAY", 0.0)


@pytest.fixture
def fake_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A pretend ~ containing mcus/, a bus directory, and klipper/katapult trees."""
    (tmp_path / "mcus").mkdir()
    (tmp_path / "bus").mkdir()
    (tmp_path / "klipper" / "src").mkdir(parents=True)
    (tmp_path / "katapult" / "src").mkdir(parents=True)
    (tmp_path / "printer_data" / "comms").mkdir(parents=True)
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
    return LIVE_MCUS_JSON.read_text(encoding="utf-8")


def make_device(bus_dir: pathlib.Path, fw: str, chipset: str, serial: str) -> pathlib.Path:
    """Create a fake /dev/serial/by-id entry.

    Real ones are symlinks; a plain file is indistinguishable for our purposes
    since we only ever listdir and stat them.
    """
    p = bus_dir / f"usb-{fw}_{chipset}_{serial}"
    p.write_text("", encoding="utf-8")
    return p
