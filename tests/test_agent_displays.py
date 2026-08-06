"""ESP32 displays: what Klipper is configured for, and whether it is there.

Named for the class rather than for Knomi, because a second, differently shaped
ESP32-S3 display is coming and would be a second PlatformIO env in the same tree
- so the knomi-specific name would have been wrong within weeks.

Read-only, and first, for the same reason `fw.dfu.scan` came before
`fw.add_mcu.start`: it establishes what is actually true on the host before
anything writes.

The device list is Klipper's, not ours. `[knomi_serial T0_knomi]` already names
the port it uses, so a second copy in our registry would only be something to
disagree with.

`present` is the field that earns this method. The klippy module catches a failed
open and runs in no-op mode, so a missing symlink or a display that never
enumerated leaves Klipper reporting no error whatsoever - just a blank screen.
Nothing else in the system notices.
"""

from __future__ import annotations

import os

import pytest

from klipper_updater.agent.methods import Api


def _moonraker(sections: dict, reachable: bool = True):
    """A call channel serving a `configfile.settings` payload.

    Section names are lowercased, as Klipper does in `settings` - matching them
    case-sensitively is what once made the mcu version join find nothing.
    """

    def call(method, params, timeout):
        if method == "printer.objects.query":
            if not reachable:
                return {}
            return {"status": {"configfile": {"settings": sections}}}
        return {}

    return call


@pytest.fixture
def api(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    return Api(paths)


def test_the_displays_come_from_klippers_config(api, fake_root):
    port = str(fake_root / "knomi_t0")
    with open(port, "w", encoding="utf-8") as fh:
        fh.write("")

    api._call = _moonraker(
        {
            "knomi_serial t0_knomi": {"serial": port},
            "mcu ebbt0": {"serial": "/dev/serial/by-id/usb-Klipper_x_y-if00"},
            "printer": {"kinematics": "corexy"},
        }
    )
    res = api.dispatch("fw.display.list")

    assert res["reachable"] is True
    assert [d["name"] for d in res["displays"]] == ["t0_knomi"]
    assert res["displays"][0]["present"] is True


def test_other_sections_are_ignored(api):
    """`configfile.settings` is the whole printer.cfg - mcu sections, kinematics,
    every macro. Only knomi_serial ones are ours."""
    api._call = _moonraker(
        {
            "mcu": {"serial": "/dev/x"},
            "mcu ebbt0": {"serial": "/dev/y"},
            "knomi_serial_helper": {"serial": "/dev/z"},
            "printer": {},
        }
    )
    assert api.dispatch("fw.display.list")["displays"] == []


def test_a_missing_symlink_is_reported_not_hidden(api, fake_root):
    """The case the klippy module swallows. Its no-op fallback means Klipper
    starts perfectly happily with a blank display and no error anywhere, so this
    is the only thing that would ever say so."""
    api._call = _moonraker(
        {"knomi_serial t0_knomi": {"serial": str(fake_root / "knomi_t0_gone")}}
    )
    display = api.dispatch("fw.display.list")["displays"][0]

    assert display["present"] is False
    assert display["resolved_path"] is None
    # ...and it still says what was asked for, so the fix is obvious.
    assert display["configured_path"].endswith("knomi_t0_gone")


def test_a_symlink_is_resolved_to_the_real_device(api, fake_root):
    """The whole scheme is "a stable name udev keeps pointed at the right tty",
    so the resolved target is what tells you which tty it landed on."""
    real = fake_root / "ttyUSB0"
    real.write_text("", encoding="utf-8")
    link = fake_root / "knomi_t0"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")

    api._call = _moonraker({"knomi_serial t0_knomi": {"serial": str(link)}})
    display = api.dispatch("fw.display.list")["displays"][0]

    assert display["present"] is True
    assert display["resolved_path"].endswith("ttyUSB0")
    assert display["configured_path"].endswith("knomi_t0")


def test_several_displays_come_back_in_a_stable_order(api, fake_root):
    """Six of them eventually, and a list that reorders between polls makes the
    panel jump around."""
    sections = {}
    for name in ("t2_knomi", "t0_knomi", "t1_knomi"):
        port = fake_root / f"knomi_{name}"
        port.write_text("", encoding="utf-8")
        sections[f"knomi_serial {name}"] = {"serial": str(port)}

    api._call = _moonraker(sections)
    names = [d["name"] for d in api.dispatch("fw.display.list")["displays"]]
    assert names == ["t0_knomi", "t1_knomi", "t2_knomi"]


def test_a_section_with_no_serial_is_skipped(api):
    """`serial:` is required by the module, but a half-edited config should not
    produce an entry pointing at nothing."""
    api._call = _moonraker({"knomi_serial t0_knomi": {"heater_hotend": "extruder"}})
    assert api.dispatch("fw.display.list")["displays"] == []


def test_an_unreachable_klipper_says_so_rather_than_claiming_none(api):
    """"No displays configured" and "we could not ask" must not look the same -
    that conflation is what made a board 90 commits behind report up to date."""
    api._call = _moonraker({}, reachable=False)
    res = api.dispatch("fw.display.list")

    assert res["reachable"] is False
    assert res["displays"] == []


def test_it_works_with_no_moonraker_at_all(paths, live_registry_text):
    """A read-only install with no call channel still answers, rather than
    raising."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    res = Api(paths).dispatch("fw.display.list")

    assert res["reachable"] is False
    assert res["displays"] == []


def test_it_is_available_to_a_read_only_agent(api):
    """It reads config and stats paths. Nothing here writes."""
    assert "fw.display.list" in api.dispatch("fw.ping")["capabilities"]


# --------------------------------------------------------------------------
# in fw.status, so the panel paints in one call
# --------------------------------------------------------------------------


def test_a_printer_with_no_displays_pays_nothing(api):
    """Not even the configfile query - an absent feature should cost an absent
    key, not a round trip."""
    calls = []
    api._call = lambda method, params, timeout: calls.append(method) or {}

    assert api.dispatch("fw.status")["displays"] == []
    assert "printer.objects.query" in calls  # for the mcu join
    # ...but display_status short-circuited before adding its own.
    assert calls.count("printer.objects.query") <= 2


def test_configured_displays_appear_in_status(api, paths, fake_root, live_registry_text):
    port = fake_root / "knomi_t0"
    port.write_text("", encoding="utf-8")
    with open(paths.main_config, "a", encoding="utf-8") as fh:
        fh.write(f"\n[display knomi_toolchanger]\nsource: {fake_root}\n")

    api._call = _moonraker({"knomi_serial t0_knomi": {"serial": str(port)}})
    entry = api.dispatch("fw.status")["displays"][0]

    assert entry["env"] == "knomi_toolchanger"
    assert [s["name"] for s in entry["screens"]] == ["t0_knomi"]
    assert entry["screens"][0]["present"] is True
    # Never flashed by us, so no MAC is known yet - and it says so rather than
    # inventing one.
    assert entry["screens"][0]["mac"] is None


def test_a_known_mac_travels_with_its_screen(api, paths, fake_root):
    from klipper_updater import displays as displays_mod

    port = fake_root / "knomi_t0"
    port.write_text("", encoding="utf-8")
    with open(paths.main_config, "a", encoding="utf-8") as fh:
        fh.write(f"\n[display knomi_toolchanger]\nsource: {fake_root}\n")
    displays_mod.record_mac(paths, str(port), "cc:ba:97:19:aa:38", "knomi_toolchanger")

    api._call = _moonraker({"knomi_serial t0_knomi": {"serial": str(port)}})
    screen = api.dispatch("fw.status")["displays"][0]["screens"][0]

    assert screen["mac"] == "cc:ba:97:19:aa:38"
    assert screen["flashed_at"] is not None


def test_screens_are_matched_to_their_type_by_klipper_section(api, paths, fake_root):
    """Two display types with different klippy modules must not collect each
    other's screens."""
    for name in ("knomi_t0", "other_a"):
        (fake_root / name).write_text("", encoding="utf-8")
    with open(paths.main_config, "a", encoding="utf-8") as fh:
        fh.write(
            f"\n[display knomi_toolchanger]\nsource: {fake_root}\n"
            f"\n[display otherscreen]\nsource: {fake_root}\nklipper_section: other_display\n"
        )

    api._call = _moonraker(
        {
            "knomi_serial t0_knomi": {"serial": str(fake_root / "knomi_t0")},
            "other_display a": {"serial": str(fake_root / "other_a")},
        }
    )
    by_env = {d["env"]: d for d in api.dispatch("fw.status")["displays"]}

    assert [s["name"] for s in by_env["knomi_toolchanger"]["screens"]] == ["t0_knomi"]
    assert [s["name"] for s in by_env["otherscreen"]["screens"]] == ["a"]
