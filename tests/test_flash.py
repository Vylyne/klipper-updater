from __future__ import annotations

import pytest

from klipper_updater import flash as flash_mod
from klipper_updater.errors import (
    AmbiguousDfuError,
    DeviceNotFoundError,
    FlashError,
    ToolMissingError,
    UnsupportedChipsetError,
)
from klipper_updater.flash import (
    flash_dfu_stm32,
    flash_initial_bootloader,
    flash_katapult,
)

from .conftest import make_device


@pytest.fixture
def ready(paths, settings, fake_root):
    """A staged firmware binary and an installed flashtool.py."""
    settings.dry_run = True
    (fake_root / "katapult" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_root / "katapult" / "scripts" / "flashtool.py").write_text("", encoding="utf-8")
    (fake_root / "mcus" / "board").mkdir(parents=True, exist_ok=True)
    (fake_root / "mcus" / "board" / "klipper.bin").write_bytes(b"\0" * 16)
    return settings


def test_missing_flashtool_raises(paths, settings, fake_root):
    (fake_root / "mcus" / "board").mkdir(parents=True)
    (fake_root / "mcus" / "board" / "klipper.bin").write_bytes(b"\0")
    with pytest.raises(ToolMissingError) as exc:
        flash_katapult(paths, settings, "board", "chipA", "S1")
    assert exc.value.data["tool"] == "flashtool.py"


def test_missing_firmware_binary_raises(paths, settings, fake_root):
    (fake_root / "katapult" / "scripts").mkdir(parents=True)
    (fake_root / "katapult" / "scripts" / "flashtool.py").write_text("", encoding="utf-8")
    with pytest.raises(FlashError) as exc:
        flash_katapult(paths, settings, "board", "chipA", "S1")
    assert "Build it first" in str(exc.value)


def test_offline_device_raises_device_not_found(paths, ready):
    with pytest.raises(DeviceNotFoundError) as exc:
        flash_katapult(paths, ready, "board", "chipA", "S1")
    assert exc.value.data["serial"] == "S1"
    assert exc.value.code == "device_not_found"


def test_device_already_in_bootloader_is_flashed_directly(paths, ready, fake_root):
    make_device(fake_root / "bus", "katapult", "chipA", "S1")
    events: list[tuple[str, str]] = []
    flash_katapult(
        paths, ready, "board", "chipA", "S1", reporter=lambda s, line: events.append((s, line))
    )
    cmds = [line for s, line in events if s == "cmd"]
    assert any("-f" in c for c in cmds)
    # No bootloader request needed - it is already there.
    assert not any("-r" in c for c in cmds)
    assert any("Flashed S1 successfully" in line for _, line in events)


def test_device_running_klipper_gets_a_bootloader_request_first(paths, ready, fake_root):
    make_device(fake_root / "bus", "klipper", "chipA", "S1")  # lowercase on purpose
    events: list[tuple[str, str]] = []
    flash_katapult(
        paths, ready, "board", "chipA", "S1", reporter=lambda s, line: events.append((s, line))
    )
    cmds = [line for s, line in events if s == "cmd"]
    assert any("-r" in c for c in cmds), "should request the bootloader"
    assert any("requesting bootloader" in line for _, line in events)
    # A dry run must still rehearse the write, not stop at the reboot request.
    assert any("-f" in c for c in cmds), "should still reach the flash step"
    assert any("Flashed S1 successfully" in line for _, line in events)


# --------------------------------------------------------------------------
# DFU
# --------------------------------------------------------------------------


def test_no_dfu_device_raises(paths, ready, monkeypatch):
    monkeypatch.setattr(flash_mod, "list_dfu_devices", lambda **kw: [])
    with pytest.raises(DeviceNotFoundError):
        flash_dfu_stm32(paths, ready, str(paths.bin_file("board", "klipper")))


def test_multiple_dfu_devices_are_refused(paths, ready, monkeypatch):
    """The original targeted 0483:df11 unconditionally, so with two boards in DFU
    it would flash whichever answered first - i.e. possibly the wrong one."""
    monkeypatch.setattr(
        flash_mod,
        "list_dfu_devices",
        lambda **kw: ["Found DFU: [0483:df11] ... path=1-1.2", "Found DFU: [0483:df11] ... path=1-1.3"],
    )
    with pytest.raises(AmbiguousDfuError) as exc:
        flash_dfu_stm32(paths, ready, str(paths.bin_file("board", "klipper")))
    assert len(exc.value.data["devices"]) == 2
    assert "Unplug all but the target" in str(exc.value)


def test_exactly_one_dfu_device_is_flashed(paths, ready, monkeypatch):
    monkeypatch.setattr(
        flash_mod, "list_dfu_devices", lambda **kw: ["Found DFU: [0483:df11] ... path=1-1.2"]
    )
    events: list[tuple[str, str]] = []
    flash_dfu_stm32(
        paths,
        ready,
        str(paths.bin_file("board", "klipper")),
        reporter=lambda s, line: events.append((s, line)),
    )
    cmds = [line for s, line in events if s == "cmd"]
    assert any("dfu-util" in c and "mass-erase" in c for c in cmds)


def test_missing_binary_for_dfu_raises(paths, ready):
    import os

    with pytest.raises(FlashError):
        flash_dfu_stm32(paths, ready, os.path.join(paths.home, "nope.bin"))


# --------------------------------------------------------------------------
# first-time bootloader dispatch
# --------------------------------------------------------------------------


def test_stm32_dispatches_to_dfu(paths, ready, monkeypatch):
    called = {}
    monkeypatch.setattr(
        flash_mod,
        "flash_dfu_stm32",
        lambda *a, **kw: called.setdefault("yes", True),
    )
    flash_initial_bootloader(paths, ready, "stm32f072xb", "x.bin")
    assert called == {"yes": True}


def test_rp2040_is_explicitly_unsupported_for_now(paths, ready):
    """Not silently broken: BOOTSEL mass storage ignores a .bin, so this needs
    the .uf2 path wiring up before it can work at all."""
    with pytest.raises(UnsupportedChipsetError) as exc:
        flash_initial_bootloader(paths, ready, "rp2040", "x.bin")
    assert ".uf2" in str(exc.value)


def test_an_unknown_chipset_is_reported_clearly(paths, ready):
    with pytest.raises(UnsupportedChipsetError) as exc:
        flash_initial_bootloader(paths, ready, "esp32", "x.bin")
    assert exc.value.data["chipset"] == "esp32"
