"""The agent's method shapes.

These are the contract with the Mainsail panel, whose TypeScript types are
hand-mirrored from docs/agent-api.md. This file is the only thing preventing the
two from drifting apart, so it asserts on keys, not just on "it didn't crash".
"""

from __future__ import annotations

import pytest

from klipper_updater import API_VERSION
from klipper_updater.agent.methods import Api
from klipper_updater.agent.rpc import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, RpcError

from .conftest import make_device


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
    assert set(ebb["serials"][0]) == {"serial", "state", "path"}
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
