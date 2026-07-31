"""Flashing: the refusal paths and the recovery paths.

The property under test throughout is not "does it flash" but **"does Klipper end
up running again"**. Every branch here exists because of a way that could fail.
"""

from __future__ import annotations

import os

import pytest

from klipper_updater.agent.methods import Api
from klipper_updater.agent.rpc import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, RpcError
from klipper_updater.errors import ServiceControlError
from klipper_updater.jobs import JobRunner
from klipper_updater.service import Journal, NullService, klipper_stopped

from .conftest import make_device

TRACKED_SERIAL = "290055001850304158373620-if00"
TRACKED_TYPE = "bttebb36"
TRACKED_CHIPSET = "stm32g0b1xx"


def _write_settings(paths, **extra) -> None:
    lines = ["[updater]", "dry_run = true", "service_backend = null"]
    lines += [f"{k} = {v}" for k, v in extra.items()]
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _stage_artifact(paths, mcu_type=TRACKED_TYPE) -> str:
    os.makedirs(paths.type_dir(mcu_type), exist_ok=True)
    path = paths.bin_file(mcu_type, "klipper")
    with open(path, "wb") as fh:
        fh.write(b"\0" * 1024)
    return path


@pytest.fixture
def flashable(paths, live_registry_text, fake_root):
    """Everything in place for a successful flash, with flashing enabled."""
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths, enable_flashing="true")
    _stage_artifact(paths)
    (fake_root / "katapult" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_root / "katapult" / "scripts" / "flashtool.py").write_text("", encoding="utf-8")
    make_device(fake_root / "bus", "Klipper", TRACKED_CHIPSET, TRACKED_SERIAL)

    runner = JobRunner(paths, lambda: __import__(
        "klipper_updater.settings", fromlist=["load_settings"]
    ).load_settings(paths.settings_file))
    api = Api(paths, runner=runner)
    yield api
    runner._cancel.set()
    runner.wait(timeout=20)


# --------------------------------------------------------------------------
# the capability gate
# --------------------------------------------------------------------------


def test_flashing_is_not_advertised_by_default(paths, live_registry_text):
    """Installing an update must never silently grant a browser the ability to
    reflash the printer."""
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths)  # enable_flashing omitted -> false
    api = Api(paths, runner=JobRunner(paths, lambda: __import__(
        "klipper_updater.settings", fromlist=["load_settings"]
    ).load_settings(paths.settings_file)))

    assert "fw.flash" not in api.dispatch("fw.ping")["capabilities"]
    assert "fw.build" in api.dispatch("fw.ping")["capabilities"]
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert exc.value.code == ERR_METHOD_NOT_FOUND


def test_flashing_is_advertised_once_enabled(flashable):
    assert "fw.flash" in flashable.dispatch("fw.ping")["capabilities"]


def test_the_gate_is_reported_by_code_when_called_directly(paths, live_registry_text):
    """dispatch hides the method, so this guard is only reachable directly - but it
    must still explain itself rather than raising something opaque."""
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths)
    api = Api(paths, runner=JobRunner(paths, lambda: __import__(
        "klipper_updater.settings", fromlist=["load_settings"]
    ).load_settings(paths.settings_file)))
    with pytest.raises(RpcError) as exc:
        api.flash({"serial": TRACKED_SERIAL})
    assert exc.value.data["code"] == "flashing_disabled"
    assert "enable_flashing" in str(exc.value)


# --------------------------------------------------------------------------
# refusals, all before a job exists
# --------------------------------------------------------------------------


def test_a_missing_serial_is_rejected(flashable):
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_an_untracked_serial_is_rejected(flashable):
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {"serial": "does-not-exist"})
    assert exc.value.data["code"] == "unknown_serial"


def test_a_serial_belonging_to_another_type_is_refused_outright(flashable):
    """A strong signal of a wrong selection, so it is refused rather than adopted."""
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL, "name": "bttmmbv1"})
    assert exc.value.data["code"] == "serial_tracked_elsewhere"


def test_flashing_without_a_built_artifact_is_refused(flashable, paths):
    os.unlink(paths.bin_file(TRACKED_TYPE, "klipper"))
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert exc.value.data["code"] == "no_artifact"
    assert flashable.runner.current() is None, "no job should have been created"


def test_flashing_a_detached_board_is_refused_before_klipper_is_stopped(flashable, fake_root):
    """Checked up front deliberately: discovering it after stopping klipper would
    mean an outage for nothing."""
    for entry in (fake_root / "bus").iterdir():
        entry.unlink()
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert exc.value.data["code"] == "device_not_found"
    assert flashable.runner.current() is None


# --------------------------------------------------------------------------
# the print gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_flashing_during_a_print_is_refused(flashable, state):
    """Nothing prevented this before; a cron'd update would destroy a print."""
    flashable._call = lambda method, params, timeout: {
        "status": {"print_stats": {"state": state}}
    }
    with pytest.raises(RpcError) as exc:
        flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert exc.value.data["code"] == "print_in_progress"
    assert flashable.runner.current() is None


def test_force_overrides_the_print_gate(flashable):
    flashable._call = lambda method, params, timeout: {
        "status": {"print_stats": {"state": "printing"}}
    }
    res = flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL, "force": True})
    assert res["job_id"]
    assert flashable.runner.wait(timeout=30)


@pytest.mark.parametrize("state", ["standby", "complete", "cancelled"])
def test_flashing_while_idle_is_allowed(flashable, state):
    flashable._call = lambda method, params, timeout: {
        "status": {"print_stats": {"state": state}}
    }
    assert flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})["job_id"]
    assert flashable.runner.wait(timeout=30)


def test_an_unreachable_moonraker_does_not_block_a_flash(flashable):
    """The print check is best-effort. Failing closed would make the panel useless
    whenever Moonraker hiccups."""
    def broken(method, params, timeout):
        raise OSError("moonraker went away")

    flashable._call = broken
    assert flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})["job_id"]
    assert flashable.runner.wait(timeout=30)


# --------------------------------------------------------------------------
# the happy path, and its ordering
# --------------------------------------------------------------------------


def test_a_flash_stops_klipper_flashes_then_starts_it_again(flashable, paths):
    res = flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert flashable.runner.wait(timeout=30)

    job = flashable.runner.get(res["job_id"])
    assert job.state == "succeeded", job.error
    assert job.result["serial"] == TRACKED_SERIAL

    lines = [line.text for line in job.log_since(0)[0]]
    joined = "\n".join(lines)
    stop_at = joined.index("would stop klipper")
    flash_at = joined.index("Flashing")
    start_at = joined.index("would start klipper")
    assert stop_at < flash_at < start_at, "klipper must be down only for the write"


def test_the_journal_is_cleared_after_a_successful_flash(flashable, paths):
    flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    assert flashable.runner.wait(timeout=30)
    assert Journal(paths).pending() is None


def test_a_flash_reports_deferred_cancellation(flashable):
    """Interrupting a write leaves half an image, so cancel is between-devices
    only - and the UI has to say so rather than looking stuck."""
    res = flashable.dispatch("fw.flash", {"serial": TRACKED_SERIAL})
    out = flashable.runner.cancel(res["job_id"])
    assert out["cancelling"] is True
    assert out["immediate"] is False
    flashable.runner.wait(timeout=30)


# --------------------------------------------------------------------------
# klipper must come back
# --------------------------------------------------------------------------


class FailingStopService(NullService):
    """Accepts the stop request but never actually goes down."""

    def stop(self, reporter=lambda s, line: None) -> None:
        self.actions.append("stop")
        # _active deliberately left True


def test_a_failed_stop_aborts_rather_than_flashing_anyway(paths):
    """Flashing while klipper holds the serial port is unsafe, so a stop that did
    not take effect must abort - not proceed and hope."""
    svc = FailingStopService()
    with pytest.raises(ServiceControlError) as exc:
        with klipper_stopped(paths, svc, "flash x", verify_timeout=0.3):
            raise AssertionError("the body must never run")
    assert "refusing to continue" in str(exc.value)
    # And it still tried to put things back.
    assert "start" in svc.actions
    assert Journal(paths).pending() is None


def test_klipper_is_restarted_even_when_the_flash_raises(paths):
    svc = NullService()
    with pytest.raises(RuntimeError):
        with klipper_stopped(paths, svc, "flash x"):
            raise RuntimeError("flashtool exploded")
    assert svc.actions == ["stop", "start"]
    assert svc.is_active() is True
    assert Journal(paths).pending() is None


def test_an_already_stopped_klipper_is_left_alone(paths):
    """If the user stopped klipper themselves, don't helpfully start it."""
    svc = NullService()
    svc.stop()
    svc.actions.clear()
    with klipper_stopped(paths, svc, "flash x"):
        pass
    assert svc.actions == []
    assert svc.is_active() is False


def test_the_journal_records_the_stop_for_crash_recovery(paths):
    """This is the layer that covers kill -9, where no finally block runs."""
    svc = NullService()
    with klipper_stopped(paths, svc, "flash 2900550018"):
        entry = Journal(paths).pending()
        assert entry is not None
        assert entry["service"] == "klipper"
        assert "2900550018" in entry["label"]
    assert Journal(paths).pending() is None


def test_agent_startup_reconciles_a_crashed_flash(paths, live_registry_text):
    """Simulates SIGKILL mid-flash: the journal survives, so the next start of the
    agent brings klipper back up."""
    from klipper_updater.agent.service import Agent

    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths)
    Journal(paths).record_stop("klipper", "flash 2900550018 (killed)")

    agent = Agent(paths, socket_path="unused")
    agent.reconcile_startup()

    assert Journal(paths).pending() is None, "the journal must be cleared once handled"


def test_shutdown_defers_while_a_flash_is_running(paths, live_registry_text):
    """systemctl restart mid-flash would otherwise SIGKILL flashtool mid-write."""
    import threading

    from klipper_updater.agent.service import Agent

    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths)

    agent = Agent(paths, socket_path="unused")
    release = threading.Event()
    agent.runner.submit("flash", {"serial": "x"}, lambda ctx: release.wait(10) and None)

    finished = threading.Event()
    threading.Thread(
        target=lambda: (agent.request_stop(20.0), finished.set()), daemon=True
    ).start()

    # Still waiting, because the flash hasn't finished.
    assert not finished.wait(0.5), "shutdown should not complete during a flash"
    release.set()
    assert finished.wait(20), "shutdown should complete once the flash is done"


def test_shutdown_does_not_defer_for_a_build(paths, live_registry_text):
    """A build is safe to interrupt, so waiting for it would just delay a restart."""
    import threading

    from klipper_updater.agent.service import Agent

    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    _write_settings(paths)

    agent = Agent(paths, socket_path="unused")
    release = threading.Event()
    agent.runner.submit("build", {"name": "x"}, lambda ctx: release.wait(10) and None)
    try:
        finished = threading.Event()
        threading.Thread(
            target=lambda: (agent.request_stop(20.0), finished.set()), daemon=True
        ).start()
        assert finished.wait(5), "a build must not hold up shutdown"
    finally:
        release.set()
        agent.runner.wait(timeout=15)
