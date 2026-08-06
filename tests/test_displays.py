"""ESP32 displays: config, PlatformIO builds, esptool uploads.

The property that matters most here is that **an upload never chooses its own
target**. Every display on this printer is an indistinguishable CH340, and
PlatformIO's auto-detect was observed picking between two of them with nothing to
tell the user which it took. Firmware written to the wrong screen is the failure
this module exists to prevent.
"""

from __future__ import annotations

import os

import pytest

from klipper_updater import displays
from klipper_updater.errors import ConfigError, FlashError, SourceTreeMissingError

# Captured verbatim from a successful `pio run -e knomi_toolchanger -t upload`
# on the printer. Parsing invented output is how the dfu-util altsetting bug
# happened, so the fixtures here are the real thing.
REAL_UPLOAD = """\
Processing knomi_toolchanger (platform: espressif32; board: knomi; framework: arduino)
PLATFORM: Espressif 32 (7.0.1) > ESP32-S3R8 8MB PSRAM
HARDWARE: ESP32S3 240MHz, 320KB RAM, 16MB Flash
Configuring upload protocol...
CURRENT: upload_protocol = esptool
Looking for upload port...
Auto-detected: /dev/ttyUSB1
Forcing reset using 1200bps open/close on port /dev/ttyUSB1
Uploading .pio/build/knomi_toolchanger/firmware.bin
esptool.py v4.11.0
Serial port /dev/ttyUSB1
Connecting....
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: WiFi, BLE, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: cc:ba:97:19:aa:38
Uploading stub...
Hash of data verified.

Leaving...
Hard resetting via RTS pin...
"""


@pytest.fixture
def tree(tmp_path):
    """A source tree that looks enough like knomi-serial."""
    root = tmp_path / "knomi_serial"
    (root / ".pio" / "build" / "knomi_toolchanger").mkdir(parents=True)
    (root / "platformio.ini").write_text("[env:knomi_toolchanger]\n", encoding="utf-8")
    return root


@pytest.fixture
def display(tree):
    return displays.DisplayType(name="knomi_toolchanger", source=str(tree))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_the_env_is_the_type(paths):
    """A PlatformIO env already names the board, partitions and flags, so the
    section name is the env unless someone says otherwise."""
    with open(paths.main_config, "w", encoding="utf-8") as fh:
        fh.write("[display knomi_toolchanger]\nsource: ~/knomi_serial\n")

    found = displays.load(paths)
    assert list(found) == ["knomi_toolchanger"]
    assert found["knomi_toolchanger"].env == "knomi_toolchanger"


def test_an_env_can_be_named_separately_if_they_ever_diverge(paths):
    with open(paths.main_config, "w", encoding="utf-8") as fh:
        fh.write("[display tool_screens]\nenv: knomi_toolchanger\n")
    assert displays.load(paths)["tool_screens"].env == "knomi_toolchanger"


def test_a_shared_source_tree_is_the_default(paths):
    """One repo, several envs - so the tree is configured once."""
    with open(paths.main_config, "w", encoding="utf-8") as fh:
        fh.write("[display knomi]\n[display knomi_toolchanger]\n")

    found = displays.load(paths, default_source="~/knomi_serial")
    assert {d.source for d in found.values()} == {"~/knomi_serial"}


def test_the_klipper_section_defaults_to_knomi_serial(paths):
    """A second display sharing the same klippy extra needs no config at all;
    one bringing its own module sets this."""
    with open(paths.main_config, "w", encoding="utf-8") as fh:
        fh.write("[display knomi_toolchanger]\n")
    assert displays.load(paths)["knomi_toolchanger"].klipper_section == "knomi_serial"


def test_no_display_sections_is_not_an_error(paths):
    with open(paths.main_config, "w", encoding="utf-8") as fh:
        fh.write("[updater]\ndry_run: true\n")
    assert displays.load(paths) == {}


def test_display_sections_do_not_disturb_the_mcu_registry(paths, live_registry_text):
    """They share a file, so each has to ignore the other's sections."""
    from klipper_updater.config import Registry

    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text + "\n[display knomi_toolchanger]\nsource: ~/k\n")

    assert "bttebb36" in Registry.load(paths).names()
    assert "knomi_toolchanger" in displays.load(paths)


# --------------------------------------------------------------------------
# the port guard - the whole point of the module
# --------------------------------------------------------------------------


def test_an_upload_without_a_port_is_refused(paths, settings, display):
    """PlatformIO would auto-detect one. With several identical CH340s attached
    that means writing firmware to whichever answered first - seen doing exactly
    that on the printer, choosing between two displays."""
    with pytest.raises(FlashError) as exc:
        displays.upload(paths, settings, display, "")
    assert "explicit port" in str(exc.value)


def test_the_upload_command_always_pins_the_port(paths, settings, display, monkeypatch):
    commands = []
    monkeypatch.setattr(
        displays, "run_streamed", lambda cmd, **kw: commands.append(cmd) or 0
    )
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")
    # Held steady so this stays about pinning: the suite runs on Windows, where
    # realpath turns a /dev path into C:\dev. Resolution has its own tests.
    monkeypatch.setattr(displays.os.path, "realpath", lambda p: p)

    displays.upload(paths, settings, display, "/dev/knomi_t0")

    cmd = commands[0]
    assert "--upload-port" in cmd
    assert cmd[cmd.index("--upload-port") + 1] == "/dev/knomi_t0"
    assert cmd[cmd.index("-e") + 1] == "knomi_toolchanger"


# --------------------------------------------------------------------------
# reading esptool's banner
# --------------------------------------------------------------------------


def test_the_mac_is_captured_from_a_real_transcript(paths, settings, display, monkeypatch):
    """The only durable identity a display has: it is in efuse, so it survives
    reflashing, and the CH340 in front of it offers no serial of its own."""

    def fake(cmd, **kwargs):
        reporter = kwargs["reporter"]
        for line in REAL_UPLOAD.splitlines():
            reporter("stdout", line)
        return 0

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    result = displays.upload(paths, settings, display, "/dev/knomi_t0")

    assert result["mac"] == "cc:ba:97:19:aa:38"
    assert result["chip"] == "ESP32-S3 (QFN56) (revision v0.2)"
    assert result["port"] == "/dev/knomi_t0"


def test_a_transcript_with_no_mac_reports_none_rather_than_guessing(
    paths, settings, display, monkeypatch
):
    def fake(cmd, **kwargs):
        kwargs["reporter"]("stdout", "Uploading...")
        return 0

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    assert displays.upload(paths, settings, display, "/dev/x")["mac"] is None


def test_a_failed_upload_raises_rather_than_returning_a_mac(
    paths, settings, display, monkeypatch
):
    """esptool refuses to write to anything that is not an ESP32, so a non-zero
    exit is the target check doing its job - it must not read as success."""

    def fake(cmd, **kwargs):
        kwargs["reporter"]("stderr", "A fatal error occurred: Failed to connect to ESP32-S3")
        return 2

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    with pytest.raises(FlashError):
        displays.upload(paths, settings, display, "/dev/knomi_t0")


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def test_a_build_runs_the_named_env_in_the_source_tree(paths, settings, display, monkeypatch):
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs["cwd"]
        return 0

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    out = displays.build(paths, settings, display)

    assert seen["cmd"][1:] == ["run", "-e", "knomi_toolchanger"]
    assert seen["cwd"] == str(display.source)
    assert out.endswith(os.path.join(".pio", "build", "knomi_toolchanger", "firmware.bin"))


def test_a_missing_source_tree_says_so_before_running_anything(paths, settings):
    absent = displays.DisplayType(name="knomi", source="/no/such/tree")
    with pytest.raises(SourceTreeMissingError):
        displays.build(paths, settings, absent)


def test_no_source_configured_is_its_own_error(paths, settings):
    """Distinct from a missing tree: one is a typo in a path, the other is a
    setting nobody filled in."""
    with pytest.raises(ConfigError):
        displays.build(paths, settings, displays.DisplayType(name="knomi"))


def test_pio_not_installed_names_the_fix(settings, monkeypatch):
    from klipper_updater.errors import ToolMissingError

    monkeypatch.setattr(displays.shutil, "which", lambda name: None)
    monkeypatch.setattr(displays.os.path, "exists", lambda path: False)

    with pytest.raises(ToolMissingError) as exc:
        displays.find_pio(settings)
    assert "penv/bin/pio" in str(exc.value)


# --------------------------------------------------------------------------
# remembering which display sat on which port
# --------------------------------------------------------------------------


def test_a_new_port_records_without_claiming_movement(paths):
    assert displays.record_mac(paths, "/dev/knomi_t0", "cc:ba:97:19:aa:38", "knomi") is None
    assert displays.read_macs(paths)["/dev/knomi_t0"]["mac"] == "cc:ba:97:19:aa:38"


def test_the_same_display_returning_is_not_movement(paths):
    displays.record_mac(paths, "/dev/knomi_t0", "cc:ba:97:19:aa:38", "knomi")
    assert displays.record_mac(paths, "/dev/knomi_t0", "cc:ba:97:19:aa:38", "knomi") is None


def test_a_different_display_on_a_known_port_reports_the_old_one(paths):
    """The swap signal. Two tophat boards in each other's sockets moves every
    display on them at once, and nothing else in the system would say so."""
    displays.record_mac(paths, "/dev/knomi_t0", "aa:aa:aa:aa:aa:aa", "knomi")
    previous = displays.record_mac(paths, "/dev/knomi_t0", "bb:bb:bb:bb:bb:bb", "knomi")

    assert previous == "aa:aa:aa:aa:aa:aa"
    assert displays.read_macs(paths)["/dev/knomi_t0"]["mac"] == "bb:bb:bb:bb:bb:bb"


def test_a_missing_mac_leaves_the_record_alone(paths):
    """esptool did not report one - a dry run, or output that changed shape.
    Writing a blank over a good record would destroy the very history this
    exists for, and it would do it silently."""
    displays.record_mac(paths, "/dev/knomi_t0", "cc:ba:97:19:aa:38", "knomi")

    assert displays.record_mac(paths, "/dev/knomi_t0", None, "knomi") is None
    assert displays.read_macs(paths)["/dev/knomi_t0"]["mac"] == "cc:ba:97:19:aa:38"


def test_an_unreadable_record_degrades_to_empty(paths):
    """Losing it means "we have no history", which is the safe direction - it
    can only ever fail to report a move, never invent one."""
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.display_macs_file, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    assert displays.read_macs(paths) == {}
    assert displays.record_mac(paths, "/dev/knomi_t0", "cc:ba:97:19:aa:38", "knomi") is None


# --------------------------------------------------------------------------
# handing PlatformIO a port it can see
#
# `pio device list` enumerates through pyserial, which reports /dev/ttyUSB0 and
# never the /dev/knomi_t0 symlink pointing at it. Passing the symlink made a
# healthy display fail with "Couldn't find a board on the selected port".
# --------------------------------------------------------------------------


def test_a_symlinked_port_is_resolved_before_platformio_sees_it(
    paths, settings, display, monkeypatch, tmp_path
):
    commands = []
    monkeypatch.setattr(displays, "run_streamed", lambda cmd, **kw: commands.append(cmd) or 0)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")
    monkeypatch.setattr(
        displays.os.path, "realpath", lambda p: "/dev/ttyUSB0" if p == "/dev/knomi_t0" else p
    )

    result = displays.upload(paths, settings, display, "/dev/knomi_t0")

    cmd = commands[0]
    assert cmd[cmd.index("--upload-port") + 1] == "/dev/ttyUSB0"
    # The stable name stays the identity: it is what the config names and what
    # the MAC record is keyed on. Only PlatformIO gets the resolved device.
    assert result["port"] == "/dev/knomi_t0"


def test_the_resolution_is_reported_so_the_written_device_is_visible(
    paths, settings, display, monkeypatch
):
    lines = []
    monkeypatch.setattr(displays, "run_streamed", lambda cmd, **kw: 0)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")
    monkeypatch.setattr(
        displays.os.path, "realpath", lambda p: "/dev/ttyUSB0" if p == "/dev/knomi_t0" else p
    )

    displays.upload(
        paths, settings, display, "/dev/knomi_t0", reporter=lambda s, t: lines.append(t)
    )

    assert any("/dev/knomi_t0 -> /dev/ttyUSB0" in line for line in lines)


def test_a_port_that_is_not_a_symlink_is_passed_through_unchanged(
    paths, settings, display, monkeypatch
):
    commands = []
    monkeypatch.setattr(displays, "run_streamed", lambda cmd, **kw: commands.append(cmd) or 0)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")
    monkeypatch.setattr(displays.os.path, "realpath", lambda p: p)

    lines = []
    displays.upload(
        paths, settings, display, "/dev/ttyUSB1", reporter=lambda s, t: lines.append(t)
    )

    cmd = commands[0]
    assert cmd[cmd.index("--upload-port") + 1] == "/dev/ttyUSB1"
    # Nothing to say when there was no indirection to report.
    assert not any("->" in line for line in lines)


def test_the_upload_command_carries_no_option_pio_run_does_not_have(
    paths, settings, display, monkeypatch
):
    """`--project-option` belongs to `pio ci` and `pio project init`, not `pio run`.

    Passing it made pio exit 2 before touching the board - which costs a whole
    Klipper stop/start cycle, because the batch has already stopped it by then.
    """
    commands = []
    monkeypatch.setattr(displays, "run_streamed", lambda cmd, **kw: commands.append(cmd) or 0)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    displays.upload(paths, settings, display, "/dev/ttyUSB0")

    cmd = commands[0]
    assert "--project-option" not in cmd
    assert not any(arg.startswith("board_upload.") for arg in cmd)
    # Only the flags `pio run` actually accepts.
    assert set(a for a in cmd if a.startswith("--")) == {"--upload-port"}


def test_waiting_for_a_new_port_is_explained_rather_than_reported_as_exit_2(
    paths, settings, display, monkeypatch
):
    """The one failure the tool cannot fix for you, so it has to say where to fix it.

    board_upload.* is settable only in platformio.ini - there is no `pio run`
    option for it - so an unadorned "pio exited 1" leaves the user with nothing.
    """
    transcript = [
        "Forcing reset using 1200bps open/close on port /dev/ttyUSB0",
        "Waiting for the new upload port...",
        "Error: Couldn't find a board on the selected port. Check that you have the "
        "correct port selected.",
    ]

    def fake(cmd, **kwargs):
        for line in transcript:
            kwargs["reporter"]("stdout", line)
        return 1

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    with pytest.raises(FlashError) as exc:
        displays.upload(paths, settings, display, "/dev/knomi_t0")

    message = str(exc.value)
    assert "board_upload.wait_for_upload_port = no" in message
    assert "knomi_toolchanger" in message  # the env whose section to edit
    assert "platformio.ini" in message
    assert exc.value.data["remedy"] == "board_upload.wait_for_upload_port = no"


def test_an_ordinary_build_failure_keeps_the_plain_message(
    paths, settings, display, monkeypatch
):
    """Only the wait-for-port signature gets the long explanation."""

    def fake(cmd, **kwargs):
        kwargs["reporter"]("stderr", "src/main.cpp:42:3: error: 'fooo' was not declared")
        return 1

    monkeypatch.setattr(displays, "run_streamed", fake)
    monkeypatch.setattr(displays, "find_pio", lambda s: "/usr/bin/pio")

    with pytest.raises(FlashError) as exc:
        displays.upload(paths, settings, display, "/dev/knomi_t0")

    assert "pio exited 1" in str(exc.value)
    assert "wait_for_upload_port" not in str(exc.value)


def test_resolve_port_survives_a_path_it_cannot_stat(monkeypatch):
    def boom(p):
        raise OSError("nope")

    monkeypatch.setattr(displays.os.path, "realpath", boom)
    # PlatformIO's own "missing port" message beats anything invented here.
    assert displays.resolve_port("/dev/knomi_t9") == "/dev/knomi_t9"
