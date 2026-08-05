"""The agent's method shapes.

These are the contract with the Mainsail panel, whose TypeScript types are
hand-mirrored from docs/agent-api.md. This file is the only thing preventing the
two from drifting apart, so it asserts on keys, not just on "it didn't crash".
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from klipper_updater import API_VERSION
from klipper_updater.agent.methods import Api
from klipper_updater.agent.rpc import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, RpcError
from klipper_updater.settings import Settings

from .conftest import make_device, write_settings


@pytest.fixture
def api(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    return Api(paths)


# --------------------------------------------------------------------------
# fw.ping
# --------------------------------------------------------------------------


def test_ping_reports_the_api_version_the_panel_gates_on(api):
    res = api.dispatch("fw.ping")
    assert res["api_version"] == API_VERSION
    assert set(res) >= {
        "api_version",
        "version",
        "dry_run",
        "enable_flashing",
        "capabilities",
        "host",
    }
    assert "fw.status" in res["capabilities"]


def test_ping_advertises_exactly_the_registered_methods(api):
    assert sorted(api.dispatch("fw.ping")["capabilities"]) == sorted(api.available_methods())


def test_a_runnerless_agent_does_not_advertise_job_methods(api):
    """The panel gates controls on `capabilities`, so a read-only deployment must
    not claim it can build."""
    caps = api.dispatch("fw.ping")["capabilities"]
    for method in Api.JOB_METHODS:
        assert method not in caps
    assert api.dispatch("fw.ping")["phase"] == 1
    assert api.dispatch("fw.status")["read_only"] is True


def test_flashing_is_off_by_default(api):
    """The web flash path must not be live until it has been failure-tested."""
    assert api.dispatch("fw.ping")["enable_flashing"] is False


# --------------------------------------------------------------------------
# fw.status
# --------------------------------------------------------------------------


def test_status_paints_the_whole_panel_in_one_call(api):
    res = api.dispatch("fw.status")
    assert set(res) >= {
        "types",
        "bus",
        "job",
        "recent",
        "locked_by",
        "klipper_service",
        "printing",
        "settings",
    }
    assert len(res["types"]) == 4
    assert res["job"] is None  # no job runner in this phase
    assert res["recent"] == []
    assert res["read_only"] is True


def test_status_type_shape(api):
    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    ebb = types["bttebb36"]
    assert ebb["chipset"] == "stm32g0b1xx"
    assert len(ebb["serials"]) == 2
    # Each serial also carries what that board is actually *running*, which is
    # a different question from whether the artifact is stale.
    assert set(ebb["serials"][0]) == {
        "serial",
        "state",
        "path",
        "mcu",
        "running_version",
        "running_sha",
        "needs_flash",
    }
    assert set(ebb["artifacts"]) == {"klipper", "katapult"}
    assert ebb["katapult"]["installed"] is True


def test_status_surfaces_makefile_patches(api):
    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    patches = types["flylllplusbuffer"]["klipper"]["makefile_patches"]
    assert patches == [{"file": "src/Makefile", "line": "src-y += buffer.c"}]


def test_status_reports_device_state_from_the_bus(api, paths, fake_root):
    make_device(fake_root / "bus", "klipper", "stm32f103xe", "36FFD9054755303923891357-if00")
    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    serials = {s["serial"]: s for s in types["sv08Mainboard"]["serials"]}
    online = serials["36FFD9054755303923891357-if00"]
    assert online["state"] == "klipper"
    assert online["path"] is not None

    offline = {s["serial"]: s for s in types["bttmmbv1"]["serials"]}
    assert next(iter(offline.values()))["state"] == "offline"


def test_artifact_reports_never_built_for_a_fresh_install(api):
    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    art = types["bttebb36"]["artifacts"]["klipper"]
    assert art["has_bin"] is False
    assert art["stale"] is True
    assert art["stale_reason"] == "never_built"
    assert set(art) >= {
        "has_config",
        "has_bin",
        "has_uf2",
        "built_fw_sha",
        "current_fw_sha",
        "stale",
        "stale_reason",
        "last_build_seconds",
    }


def test_artifact_goes_clean_after_a_build(api, paths, settings):
    """The staleness field is the whole reason the panel is worth building."""
    import os

    from klipper_updater.build import build, clear_head_cache
    from klipper_updater.config import Registry

    clear_head_cache()
    settings.dry_run = True
    os.makedirs(paths.type_dir("bttebb36"), exist_ok=True)
    with open(paths.config_file("bttebb36", "klipper"), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")
    build(paths, Registry.load(paths), settings, "bttebb36", "klipper")

    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    art = types["bttebb36"]["artifacts"]["klipper"]
    assert art["has_bin"] is True
    assert art["stale"] is False
    assert art["stale_reason"] is None
    assert art["last_build_seconds"] is not None


# --------------------------------------------------------------------------
# fw.bus.scan
# --------------------------------------------------------------------------


def test_bus_scan_marks_who_tracks_each_device(api, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "Klipper", "stm32f103xe", "36FFD9054755303923891357-if00")  # tracked
    make_device(bus, "katapult", "rp2040", "STRANGER-if00")  # not tracked

    devices = {d["serial"]: d for d in api.dispatch("fw.bus.scan")["devices"]}
    assert devices["36FFD9054755303923891357-if00"]["tracked_by"] == "sv08Mainboard"
    assert devices["STRANGER-if00"]["tracked_by"] is None
    assert devices["STRANGER-if00"]["state"] == "katapult"


def test_bus_scan_can_filter_to_untracked_only(api, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "Klipper", "stm32f103xe", "36FFD9054755303923891357-if00")
    make_device(bus, "katapult", "rp2040", "STRANGER-if00")

    res = api.dispatch("fw.bus.scan", {"only_untracked": True})
    assert [d["serial"] for d in res["devices"]] == ["STRANGER-if00"]


def test_bus_scan_can_filter_by_chipset(api, fake_root):
    bus = fake_root / "bus"
    make_device(bus, "katapult", "rp2040", "A-if00")
    make_device(bus, "katapult", "stm32g0b1xx", "B-if00")
    res = api.dispatch("fw.bus.scan", {"chipset": "rp2040"})
    assert [d["serial"] for d in res["devices"]] == ["A-if00"]


def test_bus_scan_is_empty_with_no_bus(api):
    assert api.dispatch("fw.bus.scan")["devices"] == []


# --------------------------------------------------------------------------
# fw.type.list / fw.artifacts / fw.settings.get
# --------------------------------------------------------------------------


def test_type_list_matches_status_types(api):
    assert api.dispatch("fw.type.list")["types"] == api.dispatch("fw.status")["types"]


def test_artifacts_requires_a_name(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.artifacts")
    assert exc.value.code == ERR_INVALID_PARAMS


def test_artifacts_for_an_unknown_type_carries_the_stable_code(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.artifacts", {"name": "nope"})
    assert exc.value.data["code"] == "unknown_type"


def test_artifacts_returns_both_firmwares(api):
    res = api.dispatch("fw.artifacts", {"name": "bttebb36"})
    assert set(res) == {"klipper", "katapult"}


def test_settings_get_is_serialisable(api):
    s = api.dispatch("fw.settings.get")["settings"]
    assert s["service"] == "klipper"
    assert isinstance(s["clean_before_build"], bool)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def test_unknown_method_raises_method_not_found(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.nope")
    assert exc.value.code == ERR_METHOD_NOT_FOUND


def test_none_params_is_treated_as_no_arguments(api):
    assert api.dispatch("fw.ping", None)["api_version"] == API_VERSION


def test_an_empty_positional_list_is_tolerated(api):
    assert api.dispatch("fw.ping", [])["api_version"] == API_VERSION


def test_a_non_object_params_is_rejected(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.ping", "a string")
    assert exc.value.code == ERR_INVALID_PARAMS


def test_a_corrupt_registry_surfaces_as_a_typed_error(api, paths):
    """A .cfg tolerates most junk by ignoring it, so the corrupt case that
    actually matters is a value we cannot interpret - here a makefile patch
    missing its separator, which would otherwise silently drop a source file."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write("[mcu a]\nchipset: x\nklipper_makefile_patches:\n    nonsense\n")
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.status")
    assert exc.value.data["code"] == "config_corrupt"


# --------------------------------------------------------------------------
# Moonraker enrichment, which must never be load-bearing
# --------------------------------------------------------------------------


def test_status_works_with_no_moonraker_connection(api):
    """The Api is constructed without a call channel here, so these are unknown
    rather than fatal."""
    res = api.dispatch("fw.status")
    assert res["klipper_service"] is None
    assert res["printing"] is None


def test_a_failing_probe_does_not_break_status(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    def broken(method, params, timeout):
        raise OSError("moonraker went away mid-flash")

    res = Api(paths, call=broken).dispatch("fw.status")
    assert res["klipper_service"] is None
    assert res["printing"] is None
    assert len(res["types"]) == 4  # the real payload still arrives


def test_service_state_and_print_state_are_parsed(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    def fake(method, params, timeout):
        if method == "machine.system_info":
            return {"system_info": {"service_state": {"klipper": {"active_state": "active"}}}}
        if method == "printer.objects.query":
            return {"status": {"print_stats": {"state": "printing"}}}
        raise AssertionError(f"unexpected probe {method}")

    res = Api(paths, call=fake).dispatch("fw.status")
    assert res["klipper_service"] == "active"
    assert res["printing"] is True


def test_an_unexpected_moonraker_shape_is_reported_as_unknown(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    res = Api(paths, call=lambda m, p, t: {"unexpected": True}).dispatch("fw.status")
    assert res["klipper_service"] is None
    assert res["printing"] is None


# --------------------------------------------------------------------------
# fw.bus.scan - the adoptable subset
# --------------------------------------------------------------------------


def test_bus_scan_exposes_is_mcu_per_device(api, fake_root):
    make_device(fake_root / "bus", "katapult", "stm32f072xb", "NEWBOARD-if00")
    (fake_root / "bus" / "usb-1a86_USB_Serial-if00").write_text("", encoding="utf-8")

    by_serial = {d["serial"]: d for d in api.dispatch("fw.bus.scan")["devices"]}
    assert by_serial["NEWBOARD-if00"]["is_mcu"] is True
    assert by_serial["Serial-if00"]["is_mcu"] is False


def test_adoptable_excludes_serial_adapters(api, fake_root):
    """The Phase 4 footgun: a Knomi's CH340 one tap from being tracked as a board
    and having Klipper firmware built for it."""
    make_device(fake_root / "bus", "katapult", "stm32f072xb", "NEWBOARD-if00")
    (fake_root / "bus" / "usb-1a86_USB_Serial-if00").write_text("", encoding="utf-8")

    res = api.dispatch("fw.bus.scan")
    assert [d["serial"] for d in res["adoptable"]] == ["NEWBOARD-if00"]
    # ...but the adapter is still *visible*, because someone hunting for a board
    # that hasn't appeared is better served by seeing what did.
    assert "Serial-if00" in [d["serial"] for d in res["devices"]]


def test_adoptable_excludes_already_tracked_boards(api, fake_root, live_registry_text):
    """A tracked serial from the live registry must not be offered again."""
    make_device(fake_root / "bus", "Klipper", "stm32g0b1xx", "290055001850304158373620-if00")
    res = api.dispatch("fw.bus.scan")
    tracked = next(d for d in res["devices"] if d["serial"].startswith("290055"))
    assert tracked["tracked_by"] == "bttebb36"
    assert tracked["serial"] not in [d["serial"] for d in res["adoptable"]]


def test_adoptable_respects_the_chipset_filter(api, fake_root):
    make_device(fake_root / "bus", "katapult", "stm32f072xb", "AAAA-if00")
    make_device(fake_root / "bus", "katapult", "rp2040", "BBBB-if00")
    res = api.dispatch("fw.bus.scan", {"chipset": "rp2040"})
    assert [d["serial"] for d in res["adoptable"]] == ["BBBB-if00"]


# --------------------------------------------------------------------------
# fw.serial.add / fw.serial.remove
# --------------------------------------------------------------------------


def test_serial_add_tracks_the_board_and_announces_it(paths, live_registry_text, fake_root):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    changes: list[int] = []
    api = Api(paths, on_change=lambda: changes.append(1))
    make_device(fake_root / "bus", "katapult", "stm32g0b1xx", "NEWBOARD-if00")

    res = api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "NEWBOARD-if00"})
    assert res["added"] is True
    assert "NEWBOARD-if00" in api.registry().get("bttebb36").serials
    # Other clients have to learn about it; the bus poll would not tell them,
    # because the devices on the bus did not change - only who tracks them.
    assert len(changes) == 1


def test_serial_add_is_idempotent(api, fake_root):
    make_device(fake_root / "bus", "katapult", "stm32g0b1xx", "NEWBOARD-if00")
    api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "NEWBOARD-if00"})
    again = api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "NEWBOARD-if00"})
    assert again["added"] is False
    assert api.registry().get("bttebb36").serials.count("NEWBOARD-if00") == 1


def test_serial_add_refuses_a_device_that_is_not_a_board(api, fake_root):
    """Server-side enforcement of what the panel merely filters. The panel only
    offers `adoptable` entries, but it is not the only possible caller."""
    (fake_root / "bus" / "usb-1a86_USB_Serial-if00").write_text("", encoding="utf-8")
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "Serial-if00"})
    assert exc.value.data["code"] == "not_an_mcu"
    assert "Serial-if00" not in api.registry().get("bttebb36").serials


def test_serial_add_allows_a_board_that_is_not_plugged_in(api):
    """Pre-registering an absent board is legitimate - you cannot judge what you
    cannot see, and refusing would block adding a board that is simply off."""
    res = api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "ABSENT-if00"})
    assert res["added"] is True


def test_serial_add_refuses_a_serial_tracked_under_another_type(api, fake_root):
    """One board under two types gets flashed twice with different firmware."""
    make_device(fake_root / "bus", "Klipper", "stm32g0b1xx", "290055001850304158373620-if00")
    with pytest.raises(RpcError) as exc:
        api.dispatch(
            "fw.serial.add",
            {"name": "bttmmbv1", "serial": "290055001850304158373620-if00"},
        )
    assert exc.value.data["code"] == "serial_tracked_elsewhere"
    assert "bttebb36" in exc.value.data["data"]["tracked_under"]


def test_serial_add_refuses_an_unknown_type(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.serial.add", {"name": "nope", "serial": "X-if00"})
    assert exc.value.data["code"] == "unknown_type"


@pytest.mark.parametrize("method", ["fw.serial.add", "fw.serial.remove"])
@pytest.mark.parametrize("args", [{}, {"name": "bttebb36"}, {"serial": "X"}, {"name": " ", "serial": "X"}])
def test_serial_methods_require_both_arguments(api, method, args):
    with pytest.raises(RpcError) as exc:
        api.dispatch(method, args)
    assert exc.value.code == ERR_INVALID_PARAMS


def test_serial_remove_reports_whether_it_acted(api):
    first = api.dispatch(
        "fw.serial.remove",
        {"name": "bttebb36", "serial": "290055001850304158373620-if00"},
    )
    assert first["removed"] is True
    again = api.dispatch(
        "fw.serial.remove",
        {"name": "bttebb36", "serial": "290055001850304158373620-if00"},
    )
    assert again["removed"] is False


def test_serial_remove_touches_nothing_but_the_registry(api, paths):
    """The panel must be able to promise this: untracking is not uninstalling.
    The board keeps its firmware, the type keeps its config and artifacts."""
    import os

    os.makedirs(paths.artifact_dir("bttebb36"), exist_ok=True)
    binary = paths.bin_file("bttebb36", "klipper")
    with open(binary, "wb") as fh:
        fh.write(b"\0" * 64)
    os.makedirs(paths.type_dir("bttebb36"), exist_ok=True)
    config = paths.config_file("bttebb36", "klipper")
    with open(config, "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")

    api.dispatch(
        "fw.serial.remove",
        {"name": "bttebb36", "serial": "290055001850304158373620-if00"},
    )

    assert os.path.exists(binary)
    assert os.path.exists(config)
    assert "bttebb36" in api.registry().names()  # the type survives its last serial


def test_a_mutation_preserves_comments_and_other_sections(api, paths, fake_root):
    make_device(fake_root / "bus", "katapult", "stm32g0b1xx", "NEWBOARD-if00")
    api.dispatch("fw.serial.add", {"name": "bttebb36", "serial": "NEWBOARD-if00"})

    with open(paths.main_config, encoding="utf-8") as fh:
        out = fh.read()
    assert "# mcu-updater configuration." in out
    assert "src/Makefile -> src-y += buffer.c" in out
    assert out.count("[mcu bttebb36]") == 1


# --------------------------------------------------------------------------
# fw.type.add / update / remove
# --------------------------------------------------------------------------


def test_type_add_registers_a_model(paths, live_registry_text, fake_root):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    changes: list[int] = []
    api = Api(paths, on_change=lambda: changes.append(1))

    res = api.dispatch("fw.type.add", {"name": "hexa", "chipset": "stm32f072xb"})
    assert res == {"name": "hexa", "chipset": "stm32f072xb"}
    assert api.registry().get("hexa").chipset == "stm32f072xb"
    assert len(changes) == 1


@pytest.mark.parametrize(
    "name",
    ["../../etc", "foo/bar", "foo\bar", "..", ".", "a]b", "[mcu x", "with space"],
)
def test_type_add_refuses_a_name_that_is_unsafe_as_a_path(api, name):
    """The name becomes a directory under both the config and data trees, so a
    separator or .. would write outside them. Only reachable from a CLI argument
    until the panel grew a free-text name field."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.add", {"name": name, "chipset": "stm32f072xb"})
    assert exc.value.data["code"] == "invalid_type_name"
    assert name not in api.registry().names()


@pytest.mark.parametrize(("sent", "stored"), [(" hexa", "hexa"), ("hexa ", "hexa")])
def test_type_add_normalises_surrounding_whitespace(api, sent, stored):
    """A stray space from a form field is normalised rather than refused - the
    model rejects an unstripped name, but the agent has already trimmed it, which
    is the friendlier half of the same rule."""
    res = api.dispatch("fw.type.add", {"name": sent, "chipset": "stm32f072xb"})
    assert res["name"] == stored
    assert stored in api.registry().names()


def test_type_add_refuses_a_duplicate(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.add", {"name": "bttebb36", "chipset": "stm32g0b1xx"})
    assert exc.value.data["code"] == "duplicate_type"


def test_type_add_requires_a_chipset(api):
    """Without one, no board could ever be matched on the bus."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.add", {"name": "newtype"})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_type_update_touches_only_what_was_sent(api):
    before = api.dispatch("fw.type.list")["types"]
    ebb = next(t for t in before if t["name"] == "bttebb36")

    api.dispatch("fw.type.update", {"name": "bttebb36", "klipper_extra_args": "-j2"})

    after = next(t for t in api.dispatch("fw.type.list")["types"] if t["name"] == "bttebb36")
    assert after["klipper"]["extra_args"] == "-j2"
    assert after["chipset"] == ebb["chipset"]
    assert [s["serial"] for s in after["serials"]] == [s["serial"] for s in ebb["serials"]]


def test_type_update_can_clear_katapult_installed(api):
    api.dispatch("fw.type.update", {"name": "bttebb36", "katapult_installed": False})
    assert api.registry().get("bttebb36").katapult_installed is False
    api.dispatch("fw.type.update", {"name": "bttebb36", "katapult_installed": True})
    assert api.registry().get("bttebb36").katapult_installed is True


def test_type_update_warns_when_a_chipset_change_orphans_a_binary(api, paths):
    """Staleness compares the source commit and a hash of the .config - neither
    changes when the chipset does, so an old binary would keep reporting itself
    fresh and get flashed onto a different chip."""
    import os

    os.makedirs(paths.artifact_dir("bttebb36"), exist_ok=True)
    with open(paths.bin_file("bttebb36", "klipper"), "wb") as fh:
        fh.write(b"\0" * 64)

    res = api.dispatch("fw.type.update", {"name": "bttebb36", "chipset": "stm32f446xx"})
    assert res["chipset"] == "stm32f446xx"
    assert any("Rebuild before flashing" in w for w in res["warnings"])


def test_type_update_does_not_warn_when_there_is_nothing_built(api):
    res = api.dispatch("fw.type.update", {"name": "bttebb36", "chipset": "stm32f446xx"})
    assert res["warnings"] == []


def test_type_update_refuses_a_rename(api):
    """A rename is a filesystem migration, not a config edit: the name is also the
    directory holding the saved menuconfig answers."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.update", {"name": "bttebb36", "new_name": "something"})
    assert exc.value.data["code"] == "rename_unsupported"


def test_type_remove_refuses_while_boards_are_tracked(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.remove", {"name": "bttebb36"})
    assert exc.value.data["code"] == "type_has_serials"
    assert len(exc.value.data["data"]["serials"]) == 2
    assert "bttebb36" in api.registry().names()


def test_type_remove_with_force_removes_it(api):
    res = api.dispatch("fw.type.remove", {"name": "bttebb36", "force": True})
    assert res["removed_serials"] == 2
    assert "bttebb36" not in api.registry().names()


def test_type_remove_keeps_the_saved_menuconfig_answers(api, paths):
    """The one thing here that genuinely cannot be regenerated. Removing a type
    must not delete it, so re-adding the same name gets everything back."""
    import os

    os.makedirs(paths.type_dir("bttebb36"), exist_ok=True)
    config = paths.config_file("bttebb36", "klipper")
    with open(config, "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")

    res = api.dispatch("fw.type.remove", {"name": "bttebb36", "force": True})

    assert os.path.exists(config), "removing a type deleted its menuconfig answers"
    assert res["kept_config_dir"] == paths.type_dir("bttebb36")

    # ...and re-adding recovers it.
    api.dispatch("fw.type.add", {"name": "bttebb36", "chipset": "stm32g0b1xx"})
    assert api.dispatch("fw.artifacts", {"name": "bttebb36"})["klipper"]["has_config"] is True


def test_type_remove_refuses_an_unknown_type(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.type.remove", {"name": "nope"})
    assert exc.value.data["code"] == "unknown_type"


# --------------------------------------------------------------------------
# fw.settings.set
# --------------------------------------------------------------------------


def test_settings_set_changes_only_what_was_sent(api):
    before = api.dispatch("fw.settings.get")["settings"]
    res = api.dispatch("fw.settings.set", {"settings": {"make_jobs": 4}})

    assert res["settings"]["make_jobs"] == 4
    assert res["changed"] == ["make_jobs"]
    assert res["settings"]["clean_before_build"] == before["clean_before_build"]
    assert api.dispatch("fw.settings.get")["settings"]["make_jobs"] == 4


def test_settings_set_reports_nothing_changed_when_the_value_matches(api):
    api.dispatch("fw.settings.set", {"settings": {"make_jobs": 4}})
    again = api.dispatch("fw.settings.set", {"settings": {"make_jobs": 4}})
    assert again["changed"] == []


def test_settings_set_does_not_eat_the_registry_it_shares_a_file_with(api, paths):
    """Settings and the [mcu ...] sections live in one file, so a settings write
    that rewrote the file would take the whole registry with it."""
    api.dispatch("fw.settings.set", {"settings": {"enable_flashing": True}})

    assert api.registry().names() == ["bttebb36", "bttmmbv1", "flylllplusbuffer", "sv08Mainboard"]
    with open(paths.main_config, encoding="utf-8") as fh:
        out = fh.read()
    assert "# mcu-updater configuration." in out
    assert "src/Makefile -> src-y += buffer.c" in out


def test_settings_set_announces_the_change(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    changes: list[int] = []
    api = Api(paths, on_change=lambda: changes.append(1))
    api.dispatch("fw.settings.set", {"settings": {"dry_run": True}})
    assert len(changes) == 1


@pytest.mark.parametrize("key", ["service", "service_backend"])
def test_wiring_settings_cannot_be_changed_remotely(api, key):
    """service_backend: null would let a real flash proceed *without* stopping
    klipper. Nothing about a browser form makes that worth offering."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.settings.set", {"settings": {key: "null"}})
    assert exc.value.data["code"] == "setting_not_settable"
    assert key in exc.value.data["data"]["rejected"]


def test_an_unknown_setting_is_refused(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.settings.set", {"settings": {"nonsense": 1}})
    assert exc.value.data["code"] == "setting_not_settable"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"make_jobs": "4"}, "a string is not a number"),
        ({"make_jobs": True}, "bool is an int subclass and must not sail through"),
        ({"make_jobs": -2}, "below the -1 = one per CPU floor"),
        ({"make_jobs": 65}, "absurdly high"),
        ({"log_ring_size": 0}, "would keep no log at all"),
        ({"log_ring_size": 10**7}, "would eat the host's memory"),
        ({"enable_flashing": "yes"}, "a string is not a boolean"),
        ({"enable_flashing": 1}, "1 is not a boolean here"),
    ],
)
def test_bad_values_are_refused_not_clamped(api, payload, why):
    """Clamping means the UI shows one thing and the tool does another - the same
    quiet disagreement that made a working QGL refusal look like a dead agent."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.settings.set", {"settings": payload})
    assert exc.value.code == ERR_INVALID_PARAMS
    key = next(iter(payload))
    assert getattr(api.settings(), key) == getattr(Settings(), key), why


@pytest.mark.parametrize("payload", [{}, {"settings": {}}, {"settings": "nope"}, {"settings": None}])
def test_settings_set_requires_a_non_empty_object(api, payload):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.settings.set", payload)
    assert exc.value.code == ERR_INVALID_PARAMS


def test_enabling_flashing_makes_fw_flash_appear(paths, live_registry_text):
    """The capability list is what the panel gates its flash buttons on, so this is
    the whole point: no SSH, no restart."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    from klipper_updater.jobs import JobRunner

    api = Api(paths)
    # The runner needs a settings getter, and it must be the Api's own so the
    # capability list reflects a change the instant it is written.
    api.runner = JobRunner(paths, api.settings)
    assert "fw.flash" not in api.dispatch("fw.ping")["capabilities"]

    api.dispatch("fw.settings.set", {"settings": {"enable_flashing": True}})
    assert "fw.flash" in api.dispatch("fw.ping")["capabilities"]


# --------------------------------------------------------------------------
# fw.kconfig.*
# --------------------------------------------------------------------------


@pytest.fixture
def kapi(paths, live_registry_text, fake_root):
    """An Api on a host whose klipper and katapult trees both parse."""
    import shutil

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    for fw in ("klipper", "katapult"):
        tree = fake_root / fw
        (tree / "src").mkdir(parents=True, exist_ok=True)
        (tree / "lib" / "kconfiglib").mkdir(parents=True, exist_ok=True)
        shutil.copy(fixtures / "kconfiglib" / "kconfiglib.py", tree / "lib" / "kconfiglib")
        shutil.copy(fixtures / "kconfig_tree" / "Kconfig", tree / "src" / "Kconfig")
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    return Api(paths)


def open_session(kapi, name="bttebb36", fw="klipper"):
    return kapi.dispatch("fw.kconfig.open", {"name": name, "fw": fw})


def test_status_reports_which_trees_can_be_configured(kapi):
    """So the panel hides the button rather than offering one that fails on a host
    with no source tree."""
    assert kapi.dispatch("fw.status")["kconfig_available"] == {
        "klipper": True,
        "katapult": True,
    }


def test_a_missing_source_tree_is_reported_as_unavailable(api):
    """The plain `api` fixture has empty klipper/katapult dirs and no kconfiglib."""
    available = api.dispatch("fw.status")["kconfig_available"]
    assert available == {"klipper": False, "katapult": False}


def test_open_returns_a_session_and_the_top_menu(kapi):
    res = open_session(kapi)
    assert res["session"].startswith("kc-")
    assert res["dirty"] is False
    assert len(res["breadcrumb"]) == 1
    assert any(n["kind"] == "choice" for n in res["nodes"])
    assert res["available"]["klipper"] is True


def test_open_refuses_an_unknown_type(kapi):
    """The answers are saved per type, so inventing a directory for a typo would
    not be helpful."""
    with pytest.raises(RpcError) as exc:
        kapi.dispatch("fw.kconfig.open", {"name": "nope", "fw": "klipper"})
    assert exc.value.data["code"] == "unknown_type"


@pytest.mark.parametrize("fw", ["", "nonsense", "Klipper"])
def test_open_refuses_an_unknown_firmware(kapi, fw):
    with pytest.raises(RpcError) as exc:
        kapi.dispatch("fw.kconfig.open", {"name": "bttebb36", "fw": fw})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_open_refuses_while_another_session_has_unsaved_changes(kapi):
    """Two sessions on one target means one save silently discards the other's
    work, so this is refused rather than allowed and hoped about."""
    first = open_session(kapi)
    kapi.dispatch(
        "fw.kconfig.set",
        {"session": first["session"], "id": "BOARD_NAME", "value": "editing"},
    )

    with pytest.raises(RpcError) as exc:
        open_session(kapi)
    assert exc.value.data["code"] == "kconfig_session_conflict"
    assert exc.value.data["data"]["session"] == first["session"]


def test_force_takes_over_from_a_dirty_session(kapi):
    first = open_session(kapi)
    kapi.dispatch(
        "fw.kconfig.set",
        {"session": first["session"], "id": "BOARD_NAME", "value": "editing"},
    )
    second = kapi.dispatch(
        "fw.kconfig.open", {"name": "bttebb36", "fw": "klipper", "force": True}
    )
    assert second["session"] != first["session"]


def test_a_clean_session_is_not_a_conflict(kapi):
    """Only unsaved work is worth protecting; two read-only tabs are fine."""
    open_session(kapi)
    assert open_session(kapi)["session"].startswith("kc-")


def test_navigation_round_trips(kapi):
    res = open_session(kapi)
    sid = res["session"]
    menu_id = next(n["id"] for n in res["nodes"] if n["kind"] == "menu")

    inside = kapi.dispatch("fw.kconfig.enter", {"session": sid, "id": menu_id})
    assert len(inside["breadcrumb"]) == 2
    assert len(kapi.dispatch("fw.kconfig.up", {"session": sid})["breadcrumb"]) == 1


def test_menu_refetches_the_current_screen(kapi):
    sid = open_session(kapi)["session"]
    assert kapi.dispatch("fw.kconfig.menu", {"session": sid})["revision"] == 0


def test_set_returns_the_whole_menu_and_what_changed(kapi):
    res = open_session(kapi)
    sid = res["session"]
    choice = next(n for n in res["nodes"] if n["kind"] == "choice")

    after = kapi.dispatch(
        "fw.kconfig.set", {"session": sid, "id": choice["id"], "value": "MACH_RP2040"}
    )
    assert "MACH_RP2040" in after["changed"]
    assert after["dirty"] is True
    assert "RP2040_FLASH_SIZE" in [n["name"] for n in after["nodes"]]


def test_set_requires_a_value_even_an_empty_one_must_be_explicit(kapi):
    sid = open_session(kapi)["session"]
    with pytest.raises(RpcError) as exc:
        kapi.dispatch("fw.kconfig.set", {"session": sid, "id": "BOARD_NAME"})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_a_refused_value_surfaces_as_a_kconfig_error_with_its_code(kapi):
    sid = open_session(kapi)["session"]
    with pytest.raises(RpcError) as exc:
        kapi.dispatch(
            "fw.kconfig.set", {"session": sid, "id": "STM32_CLOCK_REF", "value": "99"}
        )
    assert exc.value.data["code"] == "kconfig"
    assert "range" in exc.value.data["message"]


def test_help_and_search(kapi):
    sid = open_session(kapi)["session"]
    assert "help text" in kapi.dispatch(
        "fw.kconfig.help", {"session": sid, "id": "WITH_HELP"}
    )["help"]
    found = kapi.dispatch("fw.kconfig.search", {"session": sid, "query": "crystal"})
    assert "STM32_CLOCK_REF" in [n["name"] for n in found["nodes"]]


def test_reset_discards_unsaved_edits(kapi):
    sid = open_session(kapi)["session"]
    kapi.dispatch("fw.kconfig.set", {"session": sid, "id": "BOARD_NAME", "value": "x"})
    after = kapi.dispatch("fw.kconfig.reset", {"session": sid})
    assert after["dirty"] is False
    assert next(n["value"] for n in after["nodes"] if n["name"] == "BOARD_NAME") == "testboard"


def test_save_writes_the_config_and_announces_it(paths, live_registry_text, fake_root):
    import shutil

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    tree = fake_root / "klipper"
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "lib" / "kconfiglib").mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures / "kconfiglib" / "kconfiglib.py", tree / "lib" / "kconfiglib")
    shutil.copy(fixtures / "kconfig_tree" / "Kconfig", tree / "src" / "Kconfig")
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    changes: list[int] = []
    kapi = Api(paths, on_change=lambda: changes.append(1))
    sid = kapi.dispatch("fw.kconfig.open", {"name": "bttebb36", "fw": "klipper"})["session"]
    kapi.dispatch("fw.kconfig.set", {"session": sid, "id": "BOARD_NAME", "value": "saved"})

    res = kapi.dispatch("fw.kconfig.save", {"session": sid})
    assert os.path.isfile(res["path"])
    assert res["menu"]["dirty"] is False
    # The panel's artifact view changes when a config is written, so clients need
    # telling - staleness is computed from a hash of this file.
    assert len(changes) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is a no-op on Windows, so the lock cannot refuse there",
)
def test_save_refuses_while_a_build_holds_the_lock(kapi, paths):
    """Not the registry lock - the build lock, because this genuinely conflicts.
    build() hashes the .config to record what a binary was compiled from, so
    changing it underneath would leave provenance that does not match the artifact
    and staleness would report a wrong binary as fresh."""
    from klipper_updater.errors import BusyError
    from klipper_updater.lock import exclusive

    sid = open_session(kapi)["session"]
    kapi.dispatch("fw.kconfig.set", {"session": sid, "id": "BOARD_NAME", "value": "x"})

    with exclusive(paths, "a build"):
        with pytest.raises(RpcError) as exc:
            kapi.dispatch("fw.kconfig.save", {"session": sid})
    assert exc.value.data["code"] == BusyError.code

    # The edit survived the refusal, so nothing was lost by trying.
    assert kapi.dispatch("fw.kconfig.menu", {"session": sid})["dirty"] is True


def test_save_and_build_starts_a_job(paths, live_registry_text, fake_root):
    import shutil

    from klipper_updater.jobs import JobRunner

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    tree = fake_root / "klipper"
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "lib" / "kconfiglib").mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures / "kconfiglib" / "kconfiglib.py", tree / "lib" / "kconfiglib")
    shutil.copy(fixtures / "kconfig_tree" / "Kconfig", tree / "src" / "Kconfig")
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null")

    kapi = Api(paths)
    kapi.runner = JobRunner(paths, kapi.settings)
    sid = kapi.dispatch("fw.kconfig.open", {"name": "bttebb36", "fw": "klipper"})["session"]
    kapi.dispatch("fw.kconfig.set", {"session": sid, "id": "BOARD_NAME", "value": "built"})

    res = kapi.dispatch("fw.kconfig.save", {"session": sid, "build": True})
    assert res["job_id"]
    assert kapi.runner.wait(timeout=30), "the dry-run build should finish quickly"


def test_close_frees_the_session(kapi):
    sid = open_session(kapi)["session"]
    assert kapi.dispatch("fw.kconfig.close", {"session": sid})["closed"] is True
    with pytest.raises(RpcError) as exc:
        kapi.dispatch("fw.kconfig.menu", {"session": sid})
    assert "expired" in exc.value.data["message"]


def test_every_method_needs_a_session_id(kapi):
    for method in (
        "fw.kconfig.menu",
        "fw.kconfig.up",
        "fw.kconfig.reset",
        "fw.kconfig.save",
        "fw.kconfig.close",
    ):
        with pytest.raises(RpcError) as exc:
            kapi.dispatch(method, {})
        assert exc.value.code == ERR_INVALID_PARAMS, method


def test_klipper_and_katapult_are_configured_independently(kapi):
    k = open_session(kapi, fw="klipper")
    b = open_session(kapi, fw="katapult")
    assert k["session"] != b["session"]
    assert k["fw"] == "klipper"
    assert b["fw"] == "katapult"

    kapi.dispatch("fw.kconfig.set", {"session": k["session"], "id": "BOARD_NAME", "value": "kl"})
    kapi.dispatch("fw.kconfig.save", {"session": k["session"]})
    kapi.dispatch("fw.kconfig.set", {"session": b["session"], "id": "BOARD_NAME", "value": "ka"})
    kapi.dispatch("fw.kconfig.save", {"session": b["session"]})

    arts = kapi.dispatch("fw.artifacts", {"name": "bttebb36"})
    assert arts["klipper"]["has_config"] and arts["katapult"]["has_config"]


# --------------------------------------------------------------------------
# what is running on the boards
#
# `staleness()` compares the built .bin against the source tree, which answers
# "do I need to rebuild?". It says nothing about the boards - so a board flashed
# months ago reported "up to date", and two boards of the *same type* on different
# versions could not be expressed at all.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "sha"),
    [
        ("v0.13.0-711-gd7cea5bb", "d7cea5bb"),
        # A makefile-patched build is always -dirty, so that must not defeat the
        # match or those types would report needing a flash forever.
        ("v0.13.0-712-g6d43f8b3-dirty", "6d43f8b3"),
        ("v0.12.0", None),
        ("unknown", None),
        ("", None),
    ],
)
def test_the_commit_is_extracted_from_a_git_describe(version, sha):
    from klipper_updater.agent.methods import _running_sha

    assert _running_sha(version) == sha


def test_a_board_behind_the_source_tree_needs_flashing(api):
    head = "d7cea5bb1aca70849f28d0bb98ab1b96b9f6db65"
    versions = {"A-if00": {"version": "v0.13.0-623-gaea1bcf5", "mcu": "mcu hexa"}}
    state = api.flash_state("A-if00", versions, head)
    assert state["needs_flash"] is True
    assert state["running_sha"] == "aea1bcf5"


def test_a_board_at_the_source_tree_does_not(api):
    head = "d7cea5bb1aca70849f28d0bb98ab1b96b9f6db65"
    info = {"A-if00": {"version": "v0.13.0-711-gd7cea5bb", "mcu": "mcu"}}
    assert api.flash_state("A-if00", info, head)["needs_flash"] is False


def test_a_dirty_version_still_matches(api):
    """A type with makefile patches is dirty by construction - the patch is in place
    while klipper stamps its version - so dirty must not mean out of date."""
    head = "6d43f8b3ddbfab679d1a64cb6f9f7adbe851ee82"
    info = {"A-if00": {"version": "v0.13.0-712-g6d43f8b3-dirty", "mcu": "mcu T0_buffer"}}
    state = api.flash_state("A-if00", info, head)
    assert state["needs_flash"] is False


@pytest.mark.parametrize(
    ("versions", "head", "why"),
    [
        ({}, "d7cea5bb", "the board is offline or klippy is unreachable"),
        ({"A-if00": {"version": "v0.12.0", "mcu": "mcu"}}, "d7cea5bb", "no commit in the version"),
        ({"A-if00": {"version": "v0.13.0-711-gd7cea5bb", "mcu": "mcu"}}, None, "no git metadata"),
    ],
)
def test_unknown_is_reported_as_unknown_not_as_up_to_date(api, versions, head, why):
    """Claiming a board is current without having checked is the bug being fixed,
    so absence of evidence must not read as evidence."""
    assert api.flash_state("A-if00", versions, head)["needs_flash"] is None, why


def test_mcu_info_joins_serials_to_versions_and_names(paths, live_registry_text):
    """Klipper lowercases config section names while the printer object keeps the
    case from the file, so `[mcu EBBT0]` is object "mcu EBBT0" and setting
    "mcu ebbt0". Joining them case-sensitively would find nothing."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    calls: list[str] = []

    def fake_call(method, params=None, timeout=1.5):
        calls.append(method)
        if method == "printer.objects.list":
            return {"objects": ["configfile", "mcu", "mcu EBBT0", "toolhead"]}
        if method == "printer.objects.query":
            return {
                "status": {
                    "configfile": {
                        "settings": {
                            "mcu": {
                                "serial": "/dev/serial/by-id/usb-Klipper_stm32h723xx_2100-if00"
                            },
                            "mcu ebbt0": {
                                "serial": "/dev/serial/by-id/usb-Klipper_stm32g0b1xx_2900-if00"
                            },
                        }
                    },
                    "mcu": {"mcu_version": "v0.13.0-711-gd7cea5bb"},
                    "mcu EBBT0": {"mcu_version": "v0.13.0-712-g6d43f8b3"},
                }
            }
        return None

    api = Api(paths, call=fake_call)
    assert api.mcu_info() == {
        "2100-if00": {"version": "v0.13.0-711-gd7cea5bb", "mcu": "mcu"},
        "2900-if00": {"version": "v0.13.0-712-g6d43f8b3", "mcu": "mcu EBBT0"},
    }
    # The object list is cached, so a second call costs one probe, not two.
    calls.clear()
    api.mcu_info()
    assert calls == ["printer.objects.query"]


def test_two_boards_of_one_type_can_disagree(paths, live_registry_text, fake_root):
    """The case a per-type answer cannot express, and the one that exposed this:
    EBBT0 on -711 and EBBT1 on -712, both tracked under bttebb36."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    api = Api(paths)
    versions = {
        "290055001850304158373620-if00": {"version": "v0.13.0-711-gd7cea5bb", "mcu": "mcu EBBT0"},
        "230048001750304158373620-if00": {"version": "v0.13.0-623-gaea1bcf5", "mcu": "mcu EBBT1"},
    }
    head = "d7cea5bb1aca70849f28d0bb98ab1b96b9f6db65"

    ebb = api.type_status(api.registry(), "bttebb36", versions, head)
    by_serial = {s["serial"]: s for s in ebb["serials"]}
    assert by_serial["290055001850304158373620-if00"]["needs_flash"] is False
    assert by_serial["230048001750304158373620-if00"]["needs_flash"] is True
    assert ebb["needs_flash"] is True, "the type rolls up to needing attention"


def test_status_fetches_the_version_map_once_not_once_per_type(paths, live_registry_text):
    """Ten types must not mean ten round trips."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    calls: list[str] = []

    def fake_call(method, params=None, timeout=1.5):
        calls.append(method)
        return None

    api = Api(paths, call=fake_call)
    api.dispatch("fw.status")
    assert calls.count("printer.objects.list") <= 1
    assert calls.count("printer.objects.query") <= 2  # activity probe + versions


def test_the_klipper_mcu_name_travels_with_the_serial(api):
    """A serial is meaningless until you know which MCU it is. The name is the
    printer object verbatim - "mcu", "mcu EBBT0" - matching what Mainsail's own
    System Loads panel shows, so the two read consistently."""
    head = "d7cea5bb1aca70849f28d0bb98ab1b96b9f6db65"
    info = {"A-if00": {"version": "v0.13.0-711-gd7cea5bb", "mcu": "mcu EBBT0"}}
    assert api.flash_state("A-if00", info, head)["mcu"] == "mcu EBBT0"
    # Unknown board: no name to give, and None rather than a guess.
    assert api.flash_state("B-if00", info, head)["mcu"] is None
