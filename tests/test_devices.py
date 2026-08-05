from __future__ import annotations

import pytest

from klipper_updater.devices import (
    STATE_KATAPULT,
    STATE_KLIPPER,
    STATE_OFFLINE,
    device_state,
    find_device,
    find_untracked,
    parse_entry,
    scan,
    wait_for_device,
)
from klipper_updater.errors import BootloaderTimeoutError

from .conftest import make_device


def test_parses_the_three_part_name():
    dev = parse_entry("usb-Klipper_stm32g0b1xx_2900550018-if00", "/bus")
    assert dev is not None
    assert (dev.fw, dev.chipset, dev.serial) == ("Klipper", "stm32g0b1xx", "2900550018-if00")


def test_parses_a_two_part_name_as_having_no_chipset():
    dev = parse_entry("usb-Klipper_2900550018-if00", "/bus")
    assert dev is not None
    assert dev.chipset == ""
    assert dev.serial == "2900550018-if00"


@pytest.mark.parametrize("name", ["not-a-usb-device", "usb-onlyonepart", "usb-", "README"])
def test_ignores_unparseable_entries(name):
    assert parse_entry(name, "/bus") is None


def test_lowercase_klipper_is_found(paths, fake_root):
    """The bug this whole module exists to fix.

    The original discovered devices case-insensitively but rebuilt the path with
    an exact-case f-string when flashing, so a board enumerating as lowercase
    `usb-klipper_...` was detected and then declared missing.
    """
    make_device(fake_root / "bus", "klipper", "rp2040", "E660-if00")
    dev = find_device(paths, "rp2040", "E660-if00", fw="Klipper")
    assert dev is not None
    assert dev.is_klipper
    assert dev.path.endswith("usb-klipper_rp2040_E660-if00")


def test_uppercase_klipper_is_also_found(paths, fake_root):
    make_device(fake_root / "bus", "Klipper", "rp2040", "E660-if00")
    assert find_device(paths, "rp2040", "E660-if00", fw="klipper") is not None


def test_canboot_counts_as_katapult(paths, fake_root):
    """Katapult was called CanBoot; older bootloaders still enumerate that way."""
    make_device(fake_root / "bus", "CanBoot", "stm32f072xb", "4C00-if00")
    dev = find_device(paths, "stm32f072xb", "4C00-if00", fw="katapult")
    assert dev is not None
    assert dev.is_katapult
    assert dev.state == STATE_KATAPULT


def test_chipset_must_match(paths, fake_root):
    make_device(fake_root / "bus", "Klipper", "stm32g0b1xx", "S1-if00")
    assert find_device(paths, "rp2040", "S1-if00") is None


def test_device_state_reports_klipper_katapult_offline(paths, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "Klipper", "chipA", "running")
    make_device(bus, "katapult", "chipA", "bootloader")

    assert device_state(paths, "chipA", "running")[0] == STATE_KLIPPER
    assert device_state(paths, "chipA", "bootloader")[0] == STATE_KATAPULT
    assert device_state(paths, "chipA", "unplugged") == (STATE_OFFLINE, None)


def test_missing_bus_directory_is_empty_not_an_error(paths):
    # /dev/serial/by-id does not exist when no USB serial device is attached.
    assert scan(paths) == []


def test_find_untracked_excludes_known_serials(paths, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "Klipper", "chipA", "known")
    make_device(bus, "katapult", "chipA", "fresh")

    found = find_untracked(paths, {"known"})
    assert [d.serial for d in found] == ["fresh"]


def test_find_untracked_can_filter_by_fw_and_chipset(paths, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "katapult", "chipA", "kat-a")
    make_device(bus, "katapult", "chipB", "kat-b")
    make_device(bus, "Klipper", "chipA", "klip-a")

    assert [d.serial for d in find_untracked(paths, set(), fw="katapult")] == ["kat-a", "kat-b"]
    assert [d.serial for d in find_untracked(paths, set(), fw="katapult", chipset="chipB")] == [
        "kat-b"
    ]


def test_wait_for_device_returns_immediately_when_present(paths, fake_root):
    make_device(fake_root / "bus", "katapult", "chipA", "S1")
    dev = wait_for_device(paths, "chipA", "S1", "katapult", timeout=1, poll=0.05)
    assert dev.serial == "S1"


def test_wait_for_device_times_out_with_a_useful_error(paths):
    with pytest.raises(BootloaderTimeoutError) as exc:
        wait_for_device(paths, "chipA", "S1", "katapult", timeout=0.2, poll=0.05)
    assert exc.value.data["serial"] == "S1"
    assert exc.value.code == "bootloader_timeout"


def test_scan_is_sorted_and_stable(paths, fake_root):
    bus = fake_root / "bus"
    for serial in ("c", "a", "b"):
        make_device(bus, "Klipper", "chipA", serial)
    assert [d.serial for d in scan(paths)] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# is_mcu - which entries may be offered for adoption
# --------------------------------------------------------------------------


def test_a_ch340_adapter_parses_as_a_device_but_is_not_an_mcu(paths, fake_root):
    """usb-1a86_USB_Serial-if00 splits into three parts and so parses cleanly.
    It is a Knomi's serial adapter, not a board - and once the panel offers
    one-tap "track this", listing it is one tap from building Klipper firmware
    for a display."""
    bus = fake_root / "bus"
    (bus / "usb-1a86_USB_Serial-if00").write_text("", encoding="utf-8")

    found = scan(paths)
    assert len(found) == 1
    assert found[0].is_mcu is False


@pytest.mark.parametrize(
    ("fw", "expected"),
    [
        ("Klipper", True),
        ("klipper", True),  # lowercase happens; the bug-3 fix
        ("katapult", True),
        ("Katapult", True),
        ("CanBoot", True),  # pre-rename bootloaders are still out there
        ("1a86", False),
        ("Prolific", False),
        ("FTDI", False),
    ],
)
def test_only_klipper_and_katapult_devices_are_adoptable(paths, fake_root, fw, expected):
    make_device(fake_root / "bus", fw, "stm32g0b1xx", "AAAA1111-if00")
    found = scan(paths)
    assert len(found) == 1
    assert found[0].is_mcu is expected


def test_a_board_in_its_bootloader_is_adoptable(paths, fake_root):
    """The most important true case: a board sitting in Katapult is exactly what
    add-mcu leaves behind, so it is the thing most likely to need adopting."""
    make_device(fake_root / "bus", "katapult", "stm32g0b1xx", "NEWBOARD-if00")
    assert scan(paths)[0].is_mcu is True
