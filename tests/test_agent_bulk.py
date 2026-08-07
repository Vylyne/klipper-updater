"""Bulk operations: build all, flash all, update all.

The interesting property is not "did it loop" but **what it chose to touch**.
A bulk flash stops Klipper and writes to every board it selected, so a selection
bug here is not a cosmetic one - it is an unnecessary outage, or a board left on
last week's firmware because it was quietly skipped.

So most of these tests drive the selection helpers directly and assert on the
exact set, and only the handful that need it run a real job.
"""

from __future__ import annotations

import os

import pytest

from mcu_updater.agent.methods import Api
from mcu_updater.agent.rpc import ERR_INVALID_PARAMS, RpcError
from mcu_updater.config import Registry
from mcu_updater.jobs import IMMEDIATELY_CANCELLABLE, JobRunner
from mcu_updater.service import NullService

from .conftest import make_device, write_settings

EBB = "bttebb36"
EBB_CHIPSET = "stm32g0b1xx"
EBB_A = "290055001850304158373620-if00"
EBB_B = "230048001750304158373620-if00"
MMB = "bttmmbv1"
MMB_SERIAL = "1F002A000A50304158373420-if00"

HEAD = "d7cea5bb1aca70849f28d0bb98ab1b96b9f6db65"
CURRENT_VERSION = "v0.13.0-711-gd7cea5bb"
OLD_VERSION = "v0.13.0-623-gaea1bcf5"


def _moonraker(versions: dict[str, str], print_state="standby", idle_state="Ready"):
    """A call channel reporting one `[mcu <name>]` per serial, at a given version.

    Keyed by serial so a test can put two boards of one type at different
    versions, which is the whole reason flash selection is per-serial. The object
    names are deliberately mixed-case, because Klipper lowercases the section name
    in `configfile.settings` while the printer object keeps the file's case.
    """
    objects = {f"mcu M{i}": serial for i, serial in enumerate(versions)}

    def call(method, params, timeout):
        if method == "printer.objects.list":
            return {"objects": ["configfile", "print_stats", *objects]}
        if method == "printer.objects.query":
            requested = (params or {}).get("objects") or {}
            status: dict = {}
            if "print_stats" in requested:
                status["print_stats"] = {"state": print_state}
            if "idle_timeout" in requested:
                status["idle_timeout"] = {"state": idle_state}
            if "configfile" in requested:
                status["configfile"] = {
                    "settings": {
                        name.lower(): {
                            "serial": f"/dev/serial/by-id/usb-Klipper_{EBB_CHIPSET}_{serial}"
                        }
                        for name, serial in objects.items()
                    }
                }
            for name, serial in objects.items():
                if name in requested:
                    status[name] = {"mcu_version": versions[serial]}
            return {"status": status}
        if method == "printer.info":
            return {"state": "ready", "state_message": "klippy is ready"}
        if method == "machine.system_info":
            return {"system_info": {"service_state": {"klipper": {"active_state": "active"}}}}
        return {}

    return call


def _stage_artifact(paths, mcu_type, content=b"\0" * 1024) -> str:
    os.makedirs(paths.artifact_dir(mcu_type), exist_ok=True)
    path = paths.bin_file(mcu_type, "klipper")
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _save_config(paths, mcu_type, fw="klipper") -> None:
    """Pretend menuconfig has been run once for this type."""
    os.makedirs(paths.type_dir(mcu_type), exist_ok=True)
    with open(paths.config_file(mcu_type, fw), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")


@pytest.fixture
def bulk(paths, live_registry_text, fake_root):
    """Flashing enabled, a fake bus, and a flashtool to call."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(
        paths, dry_run="true", service_backend="null", enable_flashing="true"
    )
    (fake_root / "katapult" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_root / "katapult" / "scripts" / "flashtool.py").write_text("", encoding="utf-8")

    runner = JobRunner(
        paths,
        lambda: __import__(
            "mcu_updater.settings", fromlist=["load_settings"]
        ).load_settings(paths.settings_file),
    )
    api = Api(paths, runner=runner, call=_moonraker({}))
    api.KLIPPY_READY_TIMEOUT = 2.0
    api.KLIPPY_RESTART_TIMEOUT = 2.0
    api.KLIPPY_POLL_INTERVAL = 0.05
    yield api
    runner._cancel.set()
    runner.wait(timeout=20)


# --------------------------------------------------------------------------
# what gets advertised
# --------------------------------------------------------------------------


def test_bulk_methods_are_not_offered_by_a_read_only_agent(paths, live_registry_text):
    """The panel gates its overflow menu on `capabilities`. An agent with no
    runner must not claim it can update the fleet."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    caps = Api(paths).dispatch("fw.ping")["capabilities"]
    for method in ("fw.build_all", "fw.flash_all", "fw.update_all"):
        assert method not in caps


def test_bulk_flashing_is_not_offered_while_flashing_is_disabled(paths, live_registry_text):
    """build_all touches no board, so it stays. The two that write do not."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null")
    runner = JobRunner(paths, lambda: __import__(
        "mcu_updater.settings", fromlist=["load_settings"]
    ).load_settings(paths.settings_file))
    caps = Api(paths, runner=runner).dispatch("fw.ping")["capabilities"]

    assert "fw.build_all" in caps
    assert "fw.flash_all" not in caps
    assert "fw.update_all" not in caps


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------


def test_an_unknown_scope_is_refused_rather_than_treated_as_stale(bulk):
    """Silently falling back to `stale` would mean a user asking for `all` - the
    buffer-patch case - quietly getting nothing flashed."""
    with pytest.raises(RpcError) as exc:
        bulk.dispatch("fw.build_all", {"scope": "everything"})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_scope_defaults_to_stale(bulk):
    assert bulk._scope({}) == "stale"
    assert bulk._scope({"scope": None}) == "stale"


# --------------------------------------------------------------------------
# build_all: which types
# --------------------------------------------------------------------------


def test_a_type_with_no_saved_config_is_skipped_not_failed(bulk, paths):
    """menuconfig is ncurses and cannot run here, so there is nothing this could
    do about it. Failing the whole batch over one unconfigured type would turn a
    one-type problem into a fleet-wide one."""
    _save_config(paths, EBB)
    names = bulk._types_to_build(Registry.load(paths), "klipper", "all")
    assert names == [EBB]


def test_stale_skips_a_type_that_is_already_built(bulk, paths, settings):
    from mcu_updater.build import build

    settings.dry_run = True
    _save_config(paths, EBB)
    _save_config(paths, MMB)
    build(paths, Registry.load(paths), settings, EBB, "klipper")

    reg = Registry.load(paths)
    assert bulk._types_to_build(reg, "klipper", "stale") == [MMB]
    # ...and `all` is what overrides that judgement.
    assert sorted(bulk._types_to_build(reg, "klipper", "all")) == sorted([EBB, MMB])


def test_nothing_to_build_is_a_refusal_with_a_code_not_an_empty_job(bulk, paths, settings):
    """A job that starts and immediately does nothing reads as a bug. The panel
    switches on `nothing_to_do` to say so plainly."""
    from mcu_updater.build import build

    settings.dry_run = True
    _save_config(paths, EBB)
    build(paths, Registry.load(paths), settings, EBB, "klipper")

    with pytest.raises(RpcError) as exc:
        bulk.dispatch("fw.build_all", {"scope": "stale"})
    assert exc.value.data["code"] == "nothing_to_do"


# --------------------------------------------------------------------------
# flash_all: which boards
#
# The selection is per-serial, never per-type: two boards of one model genuinely
# do run different firmware, which is the case that started all of this.
# --------------------------------------------------------------------------


def test_only_boards_that_need_it_are_selected(paths, live_registry_text, fake_root):
    """One EBB on the current commit, its twin on an older one. Flashing both
    would be an outage half of which achieved nothing."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_B)

    api = Api(paths, call=_moonraker({EBB_A: CURRENT_VERSION, EBB_B: OLD_VERSION}))
    monkey_head(api, paths)

    boards = api._boards_to_flash(Registry.load(paths), "stale")
    assert [b["serial"] for b in boards] == [EBB_B]
    assert boards[0]["reason"] == "source_changed"


def test_scope_all_takes_every_online_board_of_a_built_type(paths, live_registry_text, fake_root):
    """The buffer-patch case: the source has not moved, so nothing looks stale,
    but you know the binary changed."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_B)

    api = Api(paths, call=_moonraker({EBB_A: CURRENT_VERSION, EBB_B: CURRENT_VERSION}))
    monkey_head(api, paths)

    assert api._boards_to_flash(Registry.load(paths), "stale") == []
    forced = api._boards_to_flash(Registry.load(paths), "all")
    assert sorted(b["serial"] for b in forced) == sorted([EBB_A, EBB_B])
    assert {b["reason"] for b in forced} == {"forced"}


def test_an_offline_board_is_never_included(paths, live_registry_text, fake_root):
    """A flash needs the board on the bus. Including it would guarantee a failure
    partway through a batch that has already stopped Klipper - and `scope: all`
    must not be a way to talk yourself past that."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)  # EBB_B absent

    api = Api(paths, call=_moonraker({EBB_A: OLD_VERSION}))
    monkey_head(api, paths)

    # Both scopes: `all` overrides the version judgement, not the physics.
    for scope in ("stale", "all"):
        assert [b["serial"] for b in api._boards_to_flash(Registry.load(paths), scope)] == [EBB_A]


def test_a_type_with_no_built_firmware_is_skipped(paths, live_registry_text, fake_root):
    """There is nothing to write. This is why update_all builds first."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)

    api = Api(paths, call=_moonraker({EBB_A: OLD_VERSION}))
    monkey_head(api, paths)

    assert api._boards_to_flash(Registry.load(paths), "all") == []


def test_a_board_in_its_bootloader_is_selected(paths, live_registry_text, fake_root):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "katapult", EBB_CHIPSET, EBB_A)

    api = Api(paths, call=_moonraker({}))
    monkey_head(api, paths)

    boards = api._boards_to_flash(Registry.load(paths), "stale")
    assert [b["reason"] for b in boards] == ["in_bootloader"]


def test_an_untracked_board_is_structurally_excluded(paths, live_registry_text, fake_root):
    """Bulk operations walk the registry, so an adopted-but-untracked board cannot
    be swept up by one. It has no type, therefore no firmware to write."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", "stm32f072xb", "4B0036000A53594731383520-if00")

    api = Api(paths, call=_moonraker({EBB_A: OLD_VERSION}))
    monkey_head(api, paths)

    serials = [b["serial"] for b in api._boards_to_flash(Registry.load(paths), "all")]
    assert "4B0036000A53594731383520-if00" not in serials


def test_naming_a_type_narrows_the_batch_to_it(paths, live_registry_text, fake_root):
    """`flash_all {name}` is flash-this-type: the same operation with a filter,
    rather than a second implementation of the loop."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _stage_artifact(paths, EBB)
    _stage_artifact(paths, MMB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, MMB_SERIAL)

    api = Api(paths, call=_moonraker({EBB_A: OLD_VERSION, MMB_SERIAL: OLD_VERSION}))
    monkey_head(api, paths)

    reg = Registry.load(paths)
    assert sorted(b["serial"] for b in api._boards_to_flash(reg, "stale")) == sorted(
        [EBB_A, MMB_SERIAL]
    )
    assert [b["serial"] for b in api._boards_to_flash(reg, "stale", MMB)] == [MMB_SERIAL]


def test_an_unknown_type_name_fails_before_a_job_exists(bulk):
    with pytest.raises(RpcError):
        bulk.dispatch("fw.flash_all", {"name": "nosuchtype"})
    assert bulk.runner.current() is None


def test_nothing_to_flash_is_a_refusal_with_a_code(bulk, paths, fake_root):
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    bulk._call = _moonraker({EBB_A: CURRENT_VERSION})
    monkey_head(bulk, paths)

    with pytest.raises(RpcError) as exc:
        bulk.dispatch("fw.flash_all", {})
    assert exc.value.data["code"] == "nothing_to_do"


# --------------------------------------------------------------------------
# the gates, all of them before a job exists
# --------------------------------------------------------------------------


def test_bulk_flash_is_refused_while_flashing_is_disabled(paths, live_registry_text, fake_root):
    """Two independent layers, and this asserts the inner one.

    Dispatch never reaches these while flashing is off, because they are not
    advertised - but the capability list is not the gate, it is the hint. The
    methods themselves must refuse too, so that turning the setting off mid-flight
    is honoured rather than merely un-suggested.
    """
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null")
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    runner = JobRunner(paths, lambda: __import__(
        "mcu_updater.settings", fromlist=["load_settings"]
    ).load_settings(paths.settings_file))
    api = Api(paths, runner=runner, call=_moonraker({EBB_A: OLD_VERSION}))

    for call in (api.flash_all, api.update_all):
        with pytest.raises(RpcError) as exc:
            call({})
        assert exc.value.data["code"] == "flashing_disabled"
    assert runner.current() is None


def test_a_bulk_flash_is_refused_while_the_printer_is_moving(bulk, paths, fake_root):
    """Not just while printing. A QGL is just as destructive to interrupt, and
    only idle_timeout sees it."""
    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    bulk._call = _moonraker({EBB_A: OLD_VERSION}, idle_state="Printing")
    monkey_head(bulk, paths)

    with pytest.raises(RpcError) as exc:
        bulk.dispatch("fw.flash_all", {})
    assert exc.value.data["code"] == "print_in_progress"
    assert bulk.runner.current() is None


# --------------------------------------------------------------------------
# running the batch
# --------------------------------------------------------------------------


def test_a_batch_stops_klipper_once_not_once_per_board(bulk, paths, fake_root, monkeypatch):
    """Ten stop/start cycles would take far longer and give ten chances for the
    restart to be the thing that fails."""
    svc = NullService()
    monkeypatch.setattr("mcu_updater.service.make_controller", lambda *a, **k: svc)

    _stage_artifact(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_B)
    bulk._call = _moonraker({EBB_A: OLD_VERSION, EBB_B: OLD_VERSION})
    monkey_head(bulk, paths)

    res = bulk.dispatch("fw.flash_all", {})
    assert len(res["boards"]) == 2
    assert bulk.runner.wait(timeout=60)

    job = bulk.runner.get(res["job_id"])
    assert job.state == "succeeded", job.error
    assert len(job.result["flashed"]) == 2
    assert svc.actions == ["stop", "start"], "one stop for the whole batch"


def test_a_build_failure_does_not_abandon_the_rest_of_the_fleet(bulk, paths, monkeypatch):
    """One type failing to compile is usually about that type."""
    from mcu_updater import build as build_mod
    from mcu_updater.errors import BuildError

    _save_config(paths, EBB)
    _save_config(paths, MMB)

    real = build_mod.build

    def flaky(paths_, reg, settings_, name, fw, **kw):
        if name == EBB:
            raise BuildError("make exploded")
        return real(paths_, reg, settings_, name, fw, **kw)

    monkeypatch.setattr(build_mod, "build", flaky)

    res = bulk.dispatch("fw.build_all", {"scope": "all"})
    assert bulk.runner.wait(timeout=60)
    job = bulk.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert job.result["built"] == [MMB]
    assert job.result["failures"] == [{"type": EBB, "error": "make exploded"}]


def test_update_all_builds_before_it_chooses_what_to_flash(bulk, paths, fake_root):
    """A build is what makes boards stale. Choosing the boards up front would use
    provenance the build is about to invalidate."""
    _save_config(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    bulk._call = _moonraker({EBB_A: OLD_VERSION})
    monkey_head(bulk, paths)

    # Nothing is built yet, so a flash_all right now would find no artifact.
    assert bulk._boards_to_flash(Registry.load(paths), "stale") == []

    res = bulk.dispatch("fw.update_all", {})
    assert bulk.runner.wait(timeout=60)
    job = bulk.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert job.result["build"]["built"] == [EBB]
    assert [f["serial"] for f in job.result["flash"]["flashed"]] == [EBB_A]


def test_update_all_re_checks_the_printer_after_the_build(bulk, paths, fake_root, monkeypatch):
    """A fleet build takes minutes. The gate that passed before submission is
    stale by the time the flash starts, and this is the last moment before Klipper
    gets stopped - so it is checked again, and Klipper is never stopped at all.
    """
    from mcu_updater import build as build_mod

    svc = NullService()
    monkeypatch.setattr("mcu_updater.service.make_controller", lambda *a, **k: svc)

    _save_config(paths, EBB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    bulk._call = _moonraker({EBB_A: OLD_VERSION})
    monkey_head(bulk, paths)

    real = build_mod.build

    def build_then_start_printing(*args, **kwargs):
        result = real(*args, **kwargs)
        bulk._call = _moonraker({EBB_A: OLD_VERSION}, idle_state="Printing")
        return result

    monkeypatch.setattr(build_mod, "build", build_then_start_printing)

    res = bulk.dispatch("fw.update_all", {})
    assert bulk.runner.wait(timeout=60)
    job = bulk.runner.get(res["job_id"])

    assert job.state == "failed"
    assert job.error["code"] == "print_in_progress"
    assert svc.actions == [], "klipper must not have been stopped"


def test_each_board_is_waited_for_before_klipper_is_started(bulk, paths, fake_root, monkeypatch):
    """The board reboots into the new firmware and re-enumerates over USB, and
    starting Klipper before its device node exists brings it up unable to find its
    MCU. Per board, not once at the end: the last board of a batch would otherwise
    have nothing between its write and the service restart.
    """
    import mcu_updater.devices as devices_mod
    import mcu_updater.flash as flash_mod

    order: list[str] = []
    svc = NullService()
    monkeypatch.setattr("mcu_updater.service.make_controller", lambda *a, **k: svc)
    monkeypatch.setattr(
        flash_mod, "flash_katapult", lambda *a, **k: order.append(f"flash {a[4]}")
    )
    monkeypatch.setattr(
        devices_mod, "wait_for_device", lambda *a, **k: order.append(f"wait {a[2]}")
    )
    write_settings(paths, dry_run="false", service_backend="null", enable_flashing="true")

    boards = [
        {"type": EBB, "serial": EBB_A, "chipset": EBB_CHIPSET, "state": "klipper", "reason": "x"},
        {"type": EBB, "serial": EBB_B, "chipset": EBB_CHIPSET, "state": "klipper", "reason": "x"},
    ]
    bulk._do_flash_all(_ctx(), boards)

    assert order == [f"flash {EBB_A}", f"wait {EBB_A}", f"flash {EBB_B}", f"wait {EBB_B}"]
    assert svc.actions == ["stop", "start"]


def test_only_the_compile_is_interruptible_mid_step(bulk):
    """Killing make costs a half-written object file. Interrupting flashtool
    leaves half an image on a board, so those kinds cancel between boards only."""
    assert "build_all" in IMMEDIATELY_CANCELLABLE
    assert "flash_all" not in IMMEDIATELY_CANCELLABLE
    assert "update_all" not in IMMEDIATELY_CANCELLABLE


def _ctx():
    """A stand-in JobContext for driving a batch body without a runner."""
    import threading
    import types

    return types.SimpleNamespace(
        reporter=lambda stream, text: None,
        cancel=threading.Event(),
        step=lambda label, index=0, total=0: None,
        check_cancelled=lambda: None,
    )


def monkey_head(api, paths):
    """Pin the klipper source HEAD so version comparison is deterministic.

    The fake root is not a git checkout, so `git_head` would return None and every
    board would read as `unknown_version` - which is a real answer, just not the
    one any of these tests are about.
    """
    import mcu_updater.build as build_mod

    build_mod._head_cache[os.path.abspath(paths.fw_dir("klipper"))] = (
        float("inf"),
        HEAD,
    )


# --------------------------------------------------------------------------
# narrowing update_all to one type
#
# "Rebuild this board type and flash its boards" is the same operation with a
# filter, exactly as flash_all {name} is - not a third loop to keep in step.
# --------------------------------------------------------------------------


def test_update_all_can_be_narrowed_to_one_type(bulk, paths, fake_root):
    _save_config(paths, EBB)
    _save_config(paths, MMB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, MMB_SERIAL)
    bulk._call = _moonraker({EBB_A: OLD_VERSION, MMB_SERIAL: OLD_VERSION})
    monkey_head(bulk, paths)

    res = bulk.dispatch("fw.update_all", {"name": MMB})
    assert res["types"] == [MMB], "only the named type is built"
    assert bulk.runner.wait(timeout=60)
    job = bulk.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert job.result["build"]["built"] == [MMB]
    # ...and only its boards are written to.
    assert [f["serial"] for f in job.result["flash"]["flashed"]] == [MMB_SERIAL]


def test_narrowing_to_an_unknown_type_fails_before_a_job(bulk):
    with pytest.raises(RpcError):
        bulk.dispatch("fw.update_all", {"name": "nosuchtype"})
    assert bulk.runner.current() is None


def test_without_a_name_it_is_still_the_whole_fleet(bulk, paths, fake_root):
    _save_config(paths, EBB)
    _save_config(paths, MMB)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, EBB_A)
    make_device(fake_root / "bus", "Klipper", EBB_CHIPSET, MMB_SERIAL)
    bulk._call = _moonraker({EBB_A: OLD_VERSION, MMB_SERIAL: OLD_VERSION})
    monkey_head(bulk, paths)

    res = bulk.dispatch("fw.update_all", {})
    assert sorted(res["types"]) == sorted([EBB, MMB])
    assert bulk.runner.wait(timeout=60)
    job = bulk.runner.get(res["job_id"])
    assert sorted(f["serial"] for f in job.result["flash"]["flashed"]) == sorted(
        [EBB_A, MMB_SERIAL]
    )
