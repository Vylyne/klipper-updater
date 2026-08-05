"""The DFU probe: what is waiting to be adopted, and can we open it.

`fw.dfu.scan` reports failures rather than raising them, because describing the
situation *is* its job. The distinctions matter physically - each one sends the
user to do something different, and getting them confused is how someone ends up
redoing a step that already worked.
"""

from __future__ import annotations

import pytest

from klipper_updater.agent.methods import Api
from klipper_updater.flash import dfu_devices, list_dfu_devices

# One real board, as dfu-util actually prints it: three altsettings sharing a
# devnum, path and serial. Counting lines here is what once refused every
# single-board flash with "3 devices are in DFU mode".
ONE_BOARD = """\
Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", \
alt=2, name="@OTP Memory /0x1FFF7000/01*0001Ke", serial="3941335F3434"
Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", \
alt=1, name="@Option Bytes /0x1FFF7800/01*040 e", serial="3941335F3434"
Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3", \
alt=0, name="@Internal Flash /0x08000000/64*02Kg", serial="3941335F3434"
"""

TWO_BOARDS = ONE_BOARD + """\
Found DFU: [0483:df11] ver=0200, devnum=52, cfg=1, intf=0, path="6-1.6.6.1.4", \
alt=0, name="@Internal Flash /0x08000000/64*02Kg", serial="205B33753539"
"""

DENIED = """\
dfu-util 0.11
Cannot open DFU device 0483:df11 found on devnum 51 (LIBUSB_ERROR_ACCESS)
"""


class FakeRun:
    """Stands in for `subprocess.run(["dfu-util", "-l"])`."""

    def __init__(self, stdout="", stderr="", exc=None):
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc

    def __call__(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return self


def patch_dfu(monkeypatch, **kwargs):
    monkeypatch.setattr("klipper_updater.flash.subprocess.run", FakeRun(**kwargs))


@pytest.fixture
def api(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    return Api(paths)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_one_board_is_one_device_not_three_altsettings(monkeypatch):
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    devices = dfu_devices()

    assert len(devices) == 1
    assert devices[0]["serial"] == "3941335F3434"
    assert devices[0]["path"] == "6-1.6.6.1.3"
    assert devices[0]["devnum"] == "51"
    assert devices[0]["vidpid"] == "0483:df11"


def test_the_fields_are_the_only_identity_a_dfu_board_has(monkeypatch):
    """It has no /dev/serial/by-id name until it re-enumerates as Katapult, so
    the USB serial and bus path are all there is to show the user."""
    patch_dfu(monkeypatch, stdout=TWO_BOARDS)
    devices = dfu_devices()

    assert [d["serial"] for d in devices] == ["3941335F3434", "205B33753539"]
    assert [d["devnum"] for d in devices] == ["51", "52"]


def test_the_raw_line_contract_is_unchanged(monkeypatch):
    """flash_dfu_stm32 and the CLI still take strings."""
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    lines = list_dfu_devices()

    assert len(lines) == 1
    assert isinstance(lines[0], str)
    assert "0483:df11" in lines[0]


# --------------------------------------------------------------------------
# fw.dfu.scan
# --------------------------------------------------------------------------


def test_a_single_board_is_ready(api, monkeypatch):
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    res = api.dispatch("fw.dfu.scan")

    assert res["ready"] is True
    assert res["reason"] is None
    assert res["count"] == 1
    assert res["devices"][0]["serial"] == "3941335F3434"


def test_permission_denied_is_never_reported_as_no_board(api, monkeypatch):
    """The regression that matters most here.

    Without the udev rule dfu-util prints no "Found DFU" line at all, and the old
    code answered "no DFU device detected, hold BOOT0 and replug" - sending the
    user back to redo the one step that had actually worked. The board and the
    jumper are fine; this is permissions.
    """
    patch_dfu(monkeypatch, stderr=DENIED)
    res = api.dispatch("fw.dfu.scan")

    assert res["reason"] == "permission_denied"
    assert res["ready"] is False
    assert res["count"] == 0
    assert "LIBUSB_ERROR_ACCESS" in (res["output"] or "")

    # It must actively say the jumper worked, and must not ask for a replug -
    # "boot" appearing at all is fine, and in fact desirable, because the useful
    # message is the reassurance "the board and the boot jumper are fine".
    message = (res["message"] or "").lower()
    assert "are fine" in message
    assert "replug" not in message
    assert "udev" in message, "it has to name the actual fix"


def test_a_missing_dfu_util_is_its_own_answer(api, monkeypatch):
    """Not an error: "the tool isn't installed" is a state to render, and it is
    nothing to do with the board."""
    patch_dfu(monkeypatch, exc=FileNotFoundError("dfu-util"))
    res = api.dispatch("fw.dfu.scan")

    assert res["reason"] == "no_tool"
    assert res["ready"] is False
    assert "apt install dfu-util" in (res["message"] or "")


def test_nothing_in_dfu_says_to_fit_the_jumper(api, monkeypatch):
    patch_dfu(monkeypatch, stdout="dfu-util 0.11\n")
    res = api.dispatch("fw.dfu.scan")

    assert res["reason"] == "none"
    assert res["ready"] is False
    assert "jumper" in (res["message"] or "").lower()


def test_two_boards_is_not_ready_until_one_is_chosen(api, monkeypatch):
    """`ready` is false because the CALLER has not chosen, not because it cannot
    be done - dfu-util takes -S/-p/-n and can target one exactly.

    Refusing by default is still right. A USB serial like "3941335F3434" says
    nothing about which board on the bench it is, so choosing on the user's behalf
    risks writing a bootloader to the wrong one.
    """
    patch_dfu(monkeypatch, stdout=TWO_BOARDS)
    res = api.dispatch("fw.dfu.scan")

    assert res["reason"] == "ambiguous"
    assert res["ready"] is False
    assert res["count"] == 2
    # Both are listed with their bus paths - the only field corresponding to a
    # physical port, and so the only hint about which board is which.
    assert len(res["devices"]) == 2
    assert all(d["path"] for d in res["devices"])


def test_the_probe_never_raises_whatever_dfu_util_does(api, monkeypatch):
    """Every branch must return a renderable answer. A scan that throws leaves
    the panel with an error banner and no idea what to tell the user to do."""
    for kwargs in (
        {"stdout": ONE_BOARD},
        {"stdout": TWO_BOARDS},
        {"stderr": DENIED},
        {"stdout": ""},
        {"exc": FileNotFoundError("dfu-util")},
        {"exc": OSError("bus error")},
    ):
        patch_dfu(monkeypatch, **kwargs)
        res = api.dispatch("fw.dfu.scan")
        assert set(res) >= {"devices", "count", "ready", "reason", "message"}
        assert isinstance(res["ready"], bool)


def test_the_probe_is_available_to_a_read_only_agent(api):
    """It runs `dfu-util -l` and nothing else. Diagnosing why a board cannot be
    seen is exactly what someone with a read-only install needs."""
    caps = api.dispatch("fw.ping")["capabilities"]
    assert "fw.dfu.scan" in caps


# --------------------------------------------------------------------------
# targeting one board among several
#
# dfu-util takes -S/-p/-n, so several boards in DFU is a choice rather than the
# dead end an earlier version of this claimed. It stays a refusal by DEFAULT,
# because knowing which physical board a USB serial belongs to is the hard part.
# --------------------------------------------------------------------------


def test_a_lone_board_is_still_pinned_by_serial(monkeypatch):
    """Not belt-and-braces: between the scan and the write, someone can jumper a
    second board and plug it in. Targeting the VID:PID alone would then take
    whichever answered first."""
    from klipper_updater.flash import dfu_selector

    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    assert dfu_selector(dfu_devices()[0]) == ["-S", "3941335F3434"]


def test_the_selector_degrades_by_how_well_each_field_survives():
    """Serial comes from the die's unique id and survives a replug; a bus path
    holds only while the board stays in one port; devnum changes every time."""
    from klipper_updater.flash import dfu_selector

    assert dfu_selector({"serial": "ABC", "path": "6-1", "devnum": "9"}) == ["-S", "ABC"]
    assert dfu_selector({"serial": None, "path": "6-1", "devnum": "9"}) == ["-p", "6-1"]
    assert dfu_selector({"serial": None, "path": None, "devnum": "9"}) == ["-n", "9"]
    assert dfu_selector({"serial": None, "path": None, "devnum": None}) == []


def _staged_bin(paths) -> str:
    path = str(paths.home) + "/katapult.bin"
    with open(path, "wb") as fh:
        fh.write(b"\0" * 16)
    return path


def test_two_boards_with_no_choice_made_is_refused(paths, monkeypatch, settings):
    from klipper_updater.errors import AmbiguousDfuError
    from klipper_updater.flash import flash_dfu_stm32

    patch_dfu(monkeypatch, stdout=TWO_BOARDS)
    with pytest.raises(AmbiguousDfuError) as exc:
        flash_dfu_stm32(paths, settings, _staged_bin(paths))
    # It must say naming one is possible, not merely "unplug the others".
    assert "serial" in str(exc.value)


def test_naming_a_serial_resolves_two_boards(paths, monkeypatch, settings):
    """The case a second board on the bench tests."""
    from klipper_updater.flash import flash_dfu_stm32

    patch_dfu(monkeypatch, stdout=TWO_BOARDS)
    commands = []
    monkeypatch.setattr(
        "klipper_updater.flash.run_streamed",
        lambda cmd, **kw: commands.append(cmd) or 0,
    )
    settings.dry_run = False
    flash_dfu_stm32(paths, settings, _staged_bin(paths), target_serial="205B33753539")

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[cmd.index("-S") + 1] == "205B33753539"
    # The altsetting stays pinned by NUMBER. All three altsettings on a G0B1
    # report the same name, so matching on the name would be the ambiguous one.
    assert cmd[cmd.index("-a") + 1] == "0"


def test_naming_a_serial_that_is_not_there_is_refused(paths, monkeypatch, settings):
    """Rather than falling back to "the only one attached" - which is precisely
    how you flash the board you were trying not to."""
    from klipper_updater.errors import DeviceNotFoundError
    from klipper_updater.flash import flash_dfu_stm32

    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    with pytest.raises(DeviceNotFoundError):
        flash_dfu_stm32(paths, settings, _staged_bin(paths), target_serial="NOTHERE")
