from __future__ import annotations

import os

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

from .conftest import cmd_tokens, make_device


def _cmds(events: list) -> list[str]:
    return [line for stream, line in events if stream == "cmd"]


@pytest.fixture
def ready(paths, settings, fake_root):
    """A staged firmware binary and an installed flashtool.py."""
    settings.dry_run = True
    (fake_root / "katapult" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_root / "katapult" / "scripts" / "flashtool.py").write_text("", encoding="utf-8")
    _stage_bin(paths)
    return settings


def _stage_bin(paths, mcu_type: str = "board") -> None:
    """Built firmware lives in the data tree, not beside the saved config."""
    os.makedirs(paths.artifact_dir(mcu_type), exist_ok=True)
    with open(paths.bin_file(mcu_type, "klipper"), "wb") as fh:
        fh.write(b"\0" * 16)


def test_missing_flashtool_raises(paths, settings, fake_root):
    _stage_bin(paths)
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
    flags = [t for c in (cmd_tokens(x) for x in _cmds(events)) for t in c]
    assert "-f" in flags
    # No bootloader request needed - it is already there.
    assert "-r" not in flags
    assert any("Flashed S1 successfully" in line for _, line in events)


def test_device_running_klipper_gets_a_bootloader_request_first(paths, ready, fake_root):
    make_device(fake_root / "bus", "klipper", "chipA", "S1")  # lowercase on purpose
    events: list[tuple[str, str]] = []
    flash_katapult(
        paths, ready, "board", "chipA", "S1", reporter=lambda s, line: events.append((s, line))
    )
    per_cmd = [cmd_tokens(c) for c in _cmds(events)]
    assert any("-r" in toks for toks in per_cmd), "should request the bootloader"
    assert any("requesting bootloader" in line for _, line in events)
    # A dry run must still rehearse the write, not stop at the reboot request.
    assert any("-f" in toks for toks in per_cmd), "should still reach the flash step"
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
    per_cmd = [cmd_tokens(c) for c in _cmds(events)]
    assert any(
        toks
        and os.path.basename(toks[0]) == "dfu-util"
        and any("mass-erase" in t for t in toks)
        for toks in per_cmd
    )


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


# --------------------------------------------------------------------------
# list_dfu_devices - the parser itself
#
# Every test above monkeypatches list_dfu_devices out, so the parsing had no
# coverage at all. These use output captured verbatim from a real BTT EBB on a
# Pi running dfu-util 0.11.
# --------------------------------------------------------------------------

#: One physical board. dfu-util prints a line per DFU altsetting, so it is three
#: lines sharing devnum=51, path and serial.
_REAL_ONE_BOARD = """dfu-util 0.11

Copyright 2005-2009 Weston Schmidt, Harald Welte and OpenMoko Inc.
Copyright 2010-2021 Tormod Volden and Stefan Schmidt
This program is Free Software and has ABSOLUTELY NO WARRANTY
Please report bugs to http://sourceforge.net/p/dfu-util/tickets/

Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", alt=2, name="@Internal Flash   /0x08000000/64*02Kg", serial="3941335F3434"
Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", alt=1, name="@Internal Flash   /0x08000000/64*02Kg", serial="3941335F3434"
Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", alt=0, name="@Internal Flash   /0x08000000/64*02Kg", serial="3941335F3434"
"""

#: Same board, same jumper, no udev rule.
_REAL_DENIED = """dfu-util 0.11

Copyright 2005-2009 Weston Schmidt, Harald Welte and OpenMoko Inc.
Copyright 2010-2021 Tormod Volden and Stefan Schmidt
This program is Free Software and has ABSOLUTELY NO WARRANTY
Please report bugs to http://sourceforge.net/p/dfu-util/tickets/

dfu-util: Cannot open DFU device 0483:df11 found on devnum 51 (LIBUSB_ERROR_ACCESS)
"""


def _fake_dfu_util(monkeypatch, stdout: str, stderr: str = "") -> None:
    import subprocess as sp

    def fake_run(cmd, **kwargs):
        assert os.path.basename(cmd[0]) == "dfu-util"
        return sp.CompletedProcess(cmd, 0, stdout, stderr)

    monkeypatch.setattr(flash_mod.subprocess, "run", fake_run)


def test_one_board_with_three_altsettings_is_one_device(monkeypatch):
    """The bug that made every DFU flash impossible: counting lines instead of
    devices meant a single board looked like three and was refused as ambiguous."""
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD)
    assert len(flash_mod.list_dfu_devices()) == 1


def test_a_single_board_is_therefore_flashable(paths, ready, monkeypatch):
    """The end-to-end consequence: no AmbiguousDfuError for one board."""
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD)
    flash_dfu_stm32(paths, ready, str(paths.bin_file("board", "klipper")))


def test_two_real_boards_are_still_refused(monkeypatch):
    """Dedup must not go so far as to merge genuinely distinct boards."""
    second = _REAL_ONE_BOARD.replace('devnum=51', 'devnum=52').replace(
        'path="6-1.6.6.1.3"', 'path="6-1.6.6.1.4"'
    ).replace('serial="3941335F3434"', 'serial="OTHERSERIAL1"')
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD + second)
    assert len(flash_mod.list_dfu_devices()) == 2


def test_permission_denied_is_not_reported_as_no_device(monkeypatch):
    """"Hold BOOT0 and replug" is the worst possible advice here - the jumper was
    already right and nothing the user does at the board will help."""
    from klipper_updater.errors import DfuPermissionError

    _fake_dfu_util(monkeypatch, "", _REAL_DENIED)
    with pytest.raises(DfuPermissionError) as exc:
        flash_mod.list_dfu_devices()
    assert "permissions" in str(exc.value).lower()
    assert "udev" in str(exc.value).lower()


def test_a_genuinely_empty_listing_is_still_empty(monkeypatch):
    _fake_dfu_util(monkeypatch, "dfu-util 0.11\n\nNo DFU capable USB device available\n")
    assert flash_mod.list_dfu_devices() == []


def test_the_parser_does_not_depend_on_the_words_found_dfu(monkeypatch):
    """Matched on VID:PID, so a wording change cannot silently blind us."""
    _fake_dfu_util(
        monkeypatch,
        'Detected DFU: [0483:df11] devnum=7, path="1-2", alt=0, serial="ABC123"\n',
    )
    assert len(flash_mod.list_dfu_devices()) == 1


# --------------------------------------------------------------------------
# the exit code dfu-util returns after a *successful* :leave
# --------------------------------------------------------------------------

#: Captured verbatim from a real EBB flash. The write succeeded; dfu-util then
#: exited 74 because the board had already detached to run the new firmware.
_REAL_LEAVE_TRANSCRIPT = [
    "dfu-util: A valid DFU suffix will be required in a future dfu-util release",
    "Opening DFU capable USB device...",
    "Device ID 0483:df11",
    "Claiming USB DFU Interface...",
    "Performing mass erase, this can take a moment",
    "Downloading element to address = 0x08000000, size = 4720",
    "Download        [=========================] 100%         4720 bytes",
    "Download done.",
    "File downloaded successfully",
    "Submitting leave request...",
    "dfu-util: Error during download get_status",
]


def _fake_run_streamed(monkeypatch, rc: int, lines: list[str]) -> None:
    def fake(cmd, *, cwd=None, reporter=None, dry_run=False, fake_delay=0.0, cancel=None):
        if reporter is not None:
            for line in lines:
                reporter("stdout", line)
        return rc

    monkeypatch.setattr(flash_mod, "run_streamed", fake)


def test_the_exit_code_after_a_successful_leave_is_not_a_failure(paths, ready, monkeypatch):
    """`:leave` makes the board detach to boot the new firmware, so dfu-util's
    final status read cannot succeed and it exits 74. The flash worked."""
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD)
    _fake_run_streamed(monkeypatch, 74, _REAL_LEAVE_TRANSCRIPT)

    events: list[tuple[str, str]] = []
    flash_dfu_stm32(
        paths,
        ready,
        str(paths.bin_file("board", "klipper")),
        reporter=lambda s, line: events.append((s, line)),
    )
    assert any("expected" in line for _, line in events)


def test_a_real_dfu_failure_still_raises(paths, ready, monkeypatch):
    """No success marker, so nothing reached the board - must not be excused."""
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD)
    _fake_run_streamed(
        monkeypatch,
        74,
        ["Opening DFU capable USB device...", "dfu-util: Cannot open DFU device"],
    )
    with pytest.raises(FlashError):
        flash_dfu_stm32(paths, ready, str(paths.bin_file("board", "klipper")))


def test_a_download_that_succeeds_then_fails_unrecognisably_still_raises(
    paths, ready, monkeypatch
):
    """Being permissive only for the known leave artifact: an unfamiliar error
    after a good download is not something to wave through."""
    _fake_dfu_util(monkeypatch, _REAL_ONE_BOARD)
    _fake_run_streamed(
        monkeypatch,
        74,
        ["File downloaded successfully", "dfu-util: something nobody has seen before"],
    )
    with pytest.raises(FlashError):
        flash_dfu_stm32(paths, ready, str(paths.bin_file("board", "klipper")))
