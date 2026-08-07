"""Setting up a brand-new board: `fw.add_mcu.start`.

The one method Phase 6 adds. Adopting the result is `fw.serial.add` and putting
Klipper on it is `fw.flash` - both already exist, so wrapping them here would be
a second implementation to keep in step with the first.

The property that shapes everything: **a board in DFU has no identity to adopt.**
It exposes no `/dev/serial/by-id` name at all, so there is nothing to put in the
registry until Katapult is on it and it re-enumerates. Hence the snapshot before
and the diff after, rather than taking a serial as an argument.
"""

from __future__ import annotations

import os

import pytest

from mcu_updater.agent.methods import Api
from mcu_updater.agent.rpc import RpcError
from mcu_updater.config import Registry
from mcu_updater.jobs import JobRunner

from .conftest import make_device, write_settings
from .test_agent_dfu import ONE_BOARD, TWO_BOARDS, patch_dfu

EBB = "bttebb36"
EBB_CHIPSET = "stm32g0b1xx"
TRACKED = "290055001850304158373620-if00"


def _runner(paths) -> JobRunner:
    return JobRunner(
        paths,
        lambda: __import__(
            "mcu_updater.settings", fromlist=["load_settings"]
        ).load_settings(paths.settings_file),
    )


def _stage_katapult(paths, mcu_type=EBB) -> str:
    os.makedirs(paths.artifact_dir(mcu_type), exist_ok=True)
    path = paths.bin_file(mcu_type, "katapult")
    with open(path, "wb") as fh:
        fh.write(b"\0" * 512)
    return path


@pytest.fixture
def adder(paths, live_registry_text, fake_root):
    """An agent with a runner and flashing enabled."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null", enable_flashing="true")

    runner = _runner(paths)
    api = Api(paths, runner=runner)
    # The "nothing appeared" cases otherwise wait out the full re-enumeration
    # timeout, which dominated the run at 15s apiece.
    api.ADD_MCU_REENUMERATE_TIMEOUT = 1.0
    yield api
    runner._cancel.set()
    runner.wait(timeout=20)


@pytest.fixture
def read_only(paths, live_registry_text, fake_root):
    """Flashing off: the capability gate, and nothing else changed."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null")
    return Api(paths, runner=_runner(paths))


# --------------------------------------------------------------------------
# the gates, every one before a job exists
# --------------------------------------------------------------------------


def test_it_is_not_advertised_without_flashing_enabled(read_only):
    caps = read_only.dispatch("fw.ping")["capabilities"]
    assert "fw.add_mcu.start" not in caps
    # The read-only probe stays: diagnosing a board you cannot see is exactly
    # what someone without flashing enabled still needs.
    assert "fw.dfu.scan" in caps


def test_the_method_refuses_too_not_just_the_advertisement(read_only):
    """Two independent layers. Turning the setting off mid-flight is honoured,
    not merely un-suggested."""
    with pytest.raises(RpcError) as exc:
        read_only.add_mcu_start({"name": EBB})
    assert exc.value.data["code"] == "flashing_disabled"


def test_an_unknown_type_fails_fast(adder):
    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": "nosuchtype"})
    assert exc.value.data["code"] == "unknown_type"
    assert adder.runner.current() is None


def test_a_type_with_no_katapult_build_is_refused_with_the_reason(adder, monkeypatch):
    """This flow installs the bootloader, so the bootloader has to exist."""
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": EBB})

    assert exc.value.data["code"] == "no_artifact"
    assert exc.value.data["data"]["fw"] == "katapult"
    assert adder.runner.current() is None


def test_a_non_stm32_type_is_refused_precisely(adder, paths, monkeypatch):
    """RP2040 needs BOOTSEL mass storage and a .uf2 - a different mechanism
    entirely. Failing inside the job with something about dfu-util would send the
    user hunting for a boot jumper that was never involved."""
    adder.dispatch("fw.type.add", {"name": "pico", "chipset": "rp2040"})
    assert "pico" in Registry.load(paths).names()
    _stage_katapult(paths, "pico")
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": "pico"})
    assert exc.value.data["code"] == "unsupported_chipset"
    assert exc.value.data["data"]["chipset"] == "rp2040"


def test_no_board_in_dfu_is_refused_before_a_job(adder, paths, monkeypatch):
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout="dfu-util 0.11\n")

    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert exc.value.data["code"] == "dfu_none"
    assert adder.runner.current() is None


def test_a_permissions_problem_keeps_its_own_code(adder, paths, monkeypatch):
    """It must not collapse into "no board" here either - that is the whole point
    of keeping the reasons apart."""
    _stage_katapult(paths)
    patch_dfu(
        monkeypatch,
        stderr="Cannot open DFU device 0483:df11 found on devnum 51 (LIBUSB_ERROR_ACCESS)",
    )

    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert exc.value.data["code"] == "dfu_permission_denied"


# --------------------------------------------------------------------------
# choosing between boards
# --------------------------------------------------------------------------


def test_two_boards_is_refused_until_one_is_named(adder, paths, monkeypatch):
    """The gate a second board on the bench tests. Not a dead end - dfu-util can
    target one exactly - but the panel has to make the choice explicit, because a
    USB serial says nothing about which board is which physically."""
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=TWO_BOARDS)

    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert exc.value.data["code"] == "dfu_ambiguous"
    assert len(exc.value.data["data"]["devices"]) == 2
    assert adder.runner.current() is None


def test_naming_one_of_two_gets_past_the_gate(adder, paths, monkeypatch):
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=TWO_BOARDS)
    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", lambda *a, **k: None)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB, "dfu_serial": "205B33753539"})
    assert res["dfu_serial"] == "205B33753539"
    assert adder.runner.wait(timeout=30)
    assert adder.runner.get(res["job_id"]).state == "succeeded"


def test_naming_a_serial_that_is_not_in_dfu_is_refused(adder, paths, monkeypatch):
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    with pytest.raises(RpcError) as exc:
        adder.dispatch("fw.add_mcu.start", {"name": EBB, "dfu_serial": "NOPE"})
    assert exc.value.data["code"] == "device_not_found"
    assert adder.runner.current() is None


def test_a_lone_board_needs_no_choice(adder, paths, monkeypatch):
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", lambda *a, **k: None)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    # Resolved for the caller, and reported so the log names what was written to.
    assert res["dfu_serial"] == "3941335F3434"
    assert adder.runner.wait(timeout=30)


# --------------------------------------------------------------------------
# finding what appeared
# --------------------------------------------------------------------------


def test_the_new_board_is_found_by_diffing_the_bus(adder, paths, fake_root, monkeypatch):
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    def appear(*args, **kwargs):
        make_device(fake_root / "bus", "katapult", EBB_CHIPSET, "NEWBOARD-if00")

    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", appear)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    job = adder.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert [c["serial"] for c in job.result["candidates"]] == ["NEWBOARD-if00"]
    assert job.result["type"] == EBB


def test_a_katapult_board_already_on_the_bus_is_not_reported_as_new(
    adder, paths, fake_root, monkeypatch
):
    """The snapshot is taken before the write, so a board already sitting in
    Katapult - a previous adopt that was never finished, say - cannot be mistaken
    for the one this just created.

    It has to be a *Katapult* device to test anything: adoptable_devices only ever
    considers those, so an already-present Klipper board would be excluded by the
    firmware filter whether the snapshot worked or not.
    """
    make_device(fake_root / "bus", "katapult", EBB_CHIPSET, "WASHERE-if00")
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", lambda *a, **k: None)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    job = adder.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert job.result["candidates"] == [], "the pre-existing board is not this one"


def test_the_new_board_is_told_apart_from_one_already_in_katapult(
    adder, paths, fake_root, monkeypatch
):
    """Both on the bus at the end; only the one that appeared is offered."""
    make_device(fake_root / "bus", "katapult", EBB_CHIPSET, "WASHERE-if00")
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    def appear(*args, **kwargs):
        make_device(fake_root / "bus", "katapult", EBB_CHIPSET, "NEWBOARD-if00")

    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", appear)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    job = adder.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert [c["serial"] for c in job.result["candidates"]] == ["NEWBOARD-if00"]


def test_a_re_bootloadered_tracked_board_is_reported_as_such_not_as_nothing(
    adder, paths, fake_root, monkeypatch
):
    """Found on the printer, and the reason this distinction exists.

    Re-installing the bootloader on a board that is ALREADY tracked is the normal
    case, not the exception - it sits offline in the registry precisely because it
    had no firmware. Baselining on untracked-only meant it came back, was
    correctly excluded as tracked, and the job said "no new device appeared",
    sending the user to hunt for a failure when the flash had worked perfectly.
    """
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    def appear(*args, **kwargs):
        make_device(fake_root / "bus", "katapult", EBB_CHIPSET, TRACKED)

    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", appear)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    job = adder.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    # Nothing to adopt - it is already ours...
    assert job.result["candidates"] == []
    # ...but it definitely came back, and the result says so.
    assert [d["serial"] for d in job.result["already_tracked"]] == [TRACKED]

    lines, _, _ = job.log_since(0)
    text = "\n".join(line.text for line in lines)
    assert "already tracked" in text
    assert "No board appeared" not in text, "it did appear; do not send them hunting"


def test_no_new_board_warns_rather_than_failing_the_job(adder, paths, monkeypatch):
    """The write may well have succeeded and the board simply be slow, or on a
    marginal port. Saying what to look at beats failing a job that worked."""
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", lambda *a, **k: None)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    job = adder.runner.get(res["job_id"])

    assert job.state == "succeeded", job.error
    assert job.result["candidates"] == []
    lines, _, _ = job.log_since(0)
    assert any("serial/by-id" in line.text for line in lines)


def test_klipper_is_never_stopped(adder, paths, monkeypatch):
    """A board that is not in printer.cfg is not held by Klipper, so there is no
    port contention and no reason for an outage. The CLI's add-mcu never stopped
    it either."""
    from mcu_updater.service import NullService

    svc = NullService()
    monkeypatch.setattr("mcu_updater.service.make_controller", lambda *a, **k: svc)
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)
    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", lambda *a, **k: None)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)

    assert adder.runner.get(res["job_id"]).state == "succeeded"
    assert svc.actions == []


def test_adopting_the_result_is_the_existing_method(adder, paths, fake_root, monkeypatch):
    """No fw.add_mcu.confirm: fw.serial.add already adopts a serial into a type,
    validation included. A second implementation would only be one more thing to
    keep in step."""
    _stage_katapult(paths)
    patch_dfu(monkeypatch, stdout=ONE_BOARD)

    def appear(*args, **kwargs):
        make_device(fake_root / "bus", "katapult", EBB_CHIPSET, "NEWBOARD-if00")

    monkeypatch.setattr("mcu_updater.flash.flash_initial_bootloader", appear)

    res = adder.dispatch("fw.add_mcu.start", {"name": EBB})
    assert adder.runner.wait(timeout=30)
    candidate = adder.runner.get(res["job_id"]).result["candidates"][0]["serial"]

    adder.dispatch("fw.serial.add", {"name": EBB, "serial": candidate})
    assert candidate in Registry.load(paths).get(EBB).serials
