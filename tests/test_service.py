"""Service control and the crash journal.

The property under test throughout: klipper must end up running again.
"""

from __future__ import annotations

import pytest

from klipper_updater.errors import PrintInProgressError
from klipper_updater.service import (
    Journal,
    MoonrakerService,
    NullService,
    assert_printer_idle,
    klipper_stopped,
    make_controller,
    reconcile,
)
from klipper_updater.settings import Settings


class FakeService(NullService):
    def __init__(self, active: bool = True) -> None:
        super().__init__("klipper")
        self._active = active

    def stop(self, reporter=lambda s, line: None) -> None:
        self.actions.append("stop")
        self._active = False

    def start(self, reporter=lambda s, line: None) -> None:
        self.actions.append("start")
        self._active = True

    def is_active(self) -> bool:
        return self._active


def test_stops_and_restarts_around_the_block(paths):
    svc = FakeService(active=True)
    with klipper_stopped(paths, svc, "flash"):
        assert svc.is_active() is False
    assert svc.actions == ["stop", "start"]
    assert svc.is_active() is True


def test_restarts_even_when_the_block_raises(paths):
    svc = FakeService(active=True)
    with pytest.raises(RuntimeError):
        with klipper_stopped(paths, svc, "flash"):
            raise RuntimeError("flash exploded")
    assert svc.actions == ["stop", "start"]
    assert svc.is_active() is True


def test_an_already_stopped_service_is_left_stopped(paths):
    """The user stopped it for a reason; don't helpfully start it."""
    svc = FakeService(active=False)
    with klipper_stopped(paths, svc, "flash"):
        pass
    assert svc.actions == []
    assert svc.is_active() is False


def test_journal_is_written_during_and_cleared_after(paths):
    journal = Journal(paths)
    svc = FakeService(active=True)
    assert journal.pending() is None

    with klipper_stopped(paths, svc, "update-all"):
        entry = journal.pending()
        assert entry is not None
        assert entry["service"] == "klipper"
        assert entry["label"] == "update-all"

    assert journal.pending() is None


def test_journal_survives_a_hard_failure_and_is_reconciled(paths):
    """Simulates the process being SIGKILLed mid-flash."""
    Journal(paths).record_stop("klipper", "flash bttebb36")
    svc = FakeService(active=False)

    assert reconcile(paths, svc) is True
    assert svc.actions == ["start"]
    assert Journal(paths).pending() is None


def test_reconcile_is_a_no_op_with_nothing_pending(paths):
    svc = FakeService(active=True)
    assert reconcile(paths, svc) is False
    assert svc.actions == []


def test_reconcile_does_not_restart_an_already_running_service(paths):
    Journal(paths).record_stop("klipper", "flash")
    svc = FakeService(active=True)
    assert reconcile(paths, svc) is True
    assert svc.actions == []  # already up; just clear the journal


def test_journal_ignores_a_corrupt_file(paths):
    with open(paths.journal_file, "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert Journal(paths).pending() is None


# --------------------------------------------------------------------------
# backend selection and fallback
# --------------------------------------------------------------------------


def test_dry_run_always_gets_the_null_backend(paths):
    svc = make_controller(Settings(dry_run=True, service_backend="systemd"))
    assert isinstance(svc, NullService)


def test_moonraker_backend_needs_a_call_channel(paths):
    """The CLI has no Moonraker connection, so it must fall back to systemd."""
    from klipper_updater.service import SystemdService

    svc = make_controller(Settings(service_backend="moonraker"), call=None)
    assert isinstance(svc, SystemdService)


def test_moonraker_start_falls_back_when_moonraker_is_gone():
    """If Moonraker died between our stop and start, the printer must still come back."""
    calls = []

    def broken_call(method, params):
        calls.append(method)
        raise OSError("socket closed")

    fallback = FakeService(active=False)
    svc = MoonrakerService(broken_call, "klipper", fallback=fallback)
    svc.start()

    assert calls == ["machine.services.start"]
    assert fallback.actions == ["start"]
    assert fallback.is_active() is True


def test_moonraker_stop_uses_the_api_when_it_works():
    calls = []
    fallback = FakeService(active=True)
    svc = MoonrakerService(lambda m, p: calls.append((m, p)), "klipper", fallback=fallback)
    svc.stop()
    assert calls == [("machine.services.stop", {"service": "klipper"})]
    assert fallback.actions == []


# --------------------------------------------------------------------------
# print safety gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_refuses_to_flash_during_a_print(state):
    with pytest.raises(PrintInProgressError) as exc:
        assert_printer_idle(Settings(), activity=lambda: {"print_state": state, "idle_state": "Ready"})
    assert exc.value.data["state"] == state


@pytest.mark.parametrize("state", ["standby", "complete", "cancelled", "error", None])
def test_allows_flashing_when_idle(state):
    assert_printer_idle(Settings(), activity=lambda: {"print_state": state, "idle_state": "Ready"})


def test_force_overrides_the_gate():
    assert_printer_idle(Settings(), activity=lambda: {"print_state": "printing"}, force=True)


def test_setting_overrides_the_gate():
    assert_printer_idle(
        Settings(allow_flash_while_printing=True), activity=lambda: {"print_state": "printing"}
    )


def test_no_print_state_source_is_a_no_op():
    """The CLI can't query Moonraker, so the check is best-effort there."""
    assert_printer_idle(Settings(), activity=None)


def test_a_failing_state_query_never_blocks_a_flash():
    def boom():
        raise OSError("moonraker unreachable")

    warnings = []
    assert_printer_idle(
        Settings(),
        activity=boom,
        reporter=lambda s, line: warnings.append((s, line)),
    )
    assert any(s == "warn" for s, _ in warnings)
