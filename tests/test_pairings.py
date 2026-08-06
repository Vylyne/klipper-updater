"""Adopting a board that turned up after the bootloader install had given up.

`add_mcu.start` waits 15 seconds and then reports what it found. When the board
comes back in time there is nothing to do here - the job already knows what it
is. This covers the rest: a marginal port, a chain of hubs, a board unplugged
after flashing and brought back later, or an agent restart in between.

In all of those the stated intent - "this is a bttebb36" - would otherwise be
lost to a timeout, and the board would arrive as an anonymous stranger.

The property under test is not "does it adopt" but **does it ever adopt the
wrong thing**, because this edits the registry without being asked at that
moment.
"""

from __future__ import annotations

import os
import time

import pytest

from klipper_updater.agent.methods import Api
from klipper_updater.config import Registry
from klipper_updater.devices import dfu_serial_for
from klipper_updater.pairings import Pairings

from .conftest import make_device

CHIPSET = "stm32g0b1xx"
# A real UID/DFU-serial pair, captured from a BTT EBB36.
NEW_UID = "27000E000551343438333339-if00"
NEW_DFU = "3941335F3434"


@pytest.fixture
def api(paths, live_registry_text):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    return Api(paths)


def _appear(fake_root, serial, fw="katapult", chipset=CHIPSET):
    make_device(fake_root / "bus", fw, chipset, serial)


def test_the_pair_survives_the_transition_the_serial_does_not():
    """The whole reason this is keyed on the DFU serial: it is the only
    identifier that exists on both sides."""
    assert dfu_serial_for(NEW_UID) == NEW_DFU


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def test_a_pairing_round_trips(paths):
    Pairings(paths).record(NEW_DFU, "bttebb36")
    assert Pairings(paths).type_for(NEW_DFU) == "bttebb36"


def test_a_pairing_expires(paths):
    """A board found in a drawer next month is the stranger it has become."""
    store = Pairings(paths, ttl=0.0)
    store.record(NEW_DFU, "bttebb36")
    time.sleep(0.01)
    assert store.type_for(NEW_DFU) is None


def test_pruning_drops_only_the_expired(paths):
    fresh = Pairings(paths)
    fresh.record(NEW_DFU, "bttebb36")
    assert Pairings(paths, ttl=0.0).prune() == 1
    assert Pairings(paths).all() == {}


def test_a_missing_or_broken_file_is_not_fatal(paths):
    """Losing it degrades to "adopt nothing", which is the safe direction."""
    assert Pairings(paths).all() == {}
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.pairings_file, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert Pairings(paths).all() == {}
    assert Pairings(paths).type_for(NEW_DFU) is None


# --------------------------------------------------------------------------
# adopting
# --------------------------------------------------------------------------


def test_a_late_board_is_adopted_under_the_type_it_was_flashed_for(api, paths, fake_root):
    """The case the whole feature exists for."""
    Pairings(paths).record(NEW_DFU, "bttebb36")
    _appear(fake_root, NEW_UID)

    adopted = api.adopt_paired()

    assert [a["serial"] for a in adopted] == [NEW_UID]
    assert adopted[0]["type"] == "bttebb36"
    assert NEW_UID in Registry.load(paths).get("bttebb36").serials


def test_it_acts_once_and_then_forgets(api, paths, fake_root):
    """A pairing that could fire twice would re-add a board the user had
    deliberately untracked."""
    Pairings(paths).record(NEW_DFU, "bttebb36")
    _appear(fake_root, NEW_UID)
    assert len(api.adopt_paired()) == 1

    assert Pairings(paths).type_for(NEW_DFU) is None

    with Registry.mutate(paths, "untrack") as reg:
        reg.remove_serial("bttebb36", NEW_UID)
    assert api.adopt_paired() == [], "an untracked board must stay untracked"


def test_an_expired_pairing_does_nothing(api, paths, fake_root):
    Pairings(paths).record(NEW_DFU, "bttebb36")
    time.sleep(0.01)
    _appear(fake_root, NEW_UID)

    # The TTL that matters is the one the ADOPTER reads with, not the writer's.
    api.PAIRING_TTL = 0.0
    assert api.adopt_paired() == []
    assert NEW_UID not in Registry.load(paths).get("bttebb36").serials


def test_a_board_with_no_pairing_is_left_alone(api, paths, fake_root):
    """Untracked boards are normal. Only ones we bootloadered are ours to claim."""
    _appear(fake_root, NEW_UID)
    assert api.adopt_paired() == []


def test_an_already_tracked_board_is_untouched(api, paths, fake_root):
    tracked = "290055001850304158373620-if00"
    Pairings(paths).record(dfu_serial_for(tracked) or "x", "bttmmbv1")
    _appear(fake_root, tracked)

    assert api.adopt_paired() == []
    # Still under its original type, not moved to the paired one.
    assert tracked in Registry.load(paths).get("bttebb36").serials


def test_a_klipper_device_is_not_adopted(api, paths, fake_root):
    """Only Katapult. A board running Klipper did not just come out of a
    bootloader install."""
    Pairings(paths).record(NEW_DFU, "bttebb36")
    _appear(fake_root, NEW_UID, fw="Klipper")

    assert api.adopt_paired() == []


def test_a_removed_type_is_skipped_quietly_not_attempted_and_failed(paths, live_registry_text, fake_root):
    """Adopting is skipped either way - the registry refuses an unknown type, and
    that refusal is caught. The guard is about not *trying*: the bus poll fires
    on every change for the whole TTL, so attempting a doomed mutation would take
    the registry lock and log a warning over and over for a day.
    """

    class Recorder:
        def __init__(self):
            self.warnings = []

        def warning(self, msg):
            self.warnings.append(msg)

        def info(self, msg):
            pass

        def debug(self, msg):
            pass

    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    log = Recorder()
    api = Api(paths, logger=log)

    Pairings(paths).record(NEW_DFU, "gonetype")
    _appear(fake_root, NEW_UID)

    assert api.adopt_paired() == []
    assert "gonetype" not in Registry.load(paths).names()
    assert log.warnings == [], "a pairing for a type that is gone is not an error to report"


def test_two_boards_sharing_a_dfu_serial_adopt_neither(api, paths, fake_root):
    """The derivation sums two of the three id words, so a collision is possible.
    Adopting the wrong board under a type is how the wrong firmware gets written
    to it later - so an ambiguous match does nothing, exactly as naming does."""
    twin = "38333339" + "05513434" + "27000E00" + "-if00"
    assert dfu_serial_for(twin) == NEW_DFU

    Pairings(paths).record(NEW_DFU, "bttebb36")
    _appear(fake_root, NEW_UID)
    _appear(fake_root, twin)

    assert api.adopt_paired() == []
    assert Pairings(paths).type_for(NEW_DFU) == "bttebb36", "and the pairing is kept"


def test_nothing_on_the_bus_is_cheap_and_harmless(api, paths):
    Pairings(paths).record(NEW_DFU, "bttebb36")
    assert api.adopt_paired() == []
    # The pairing survives for when the board does turn up.
    assert Pairings(paths).type_for(NEW_DFU) == "bttebb36"


def test_no_pairings_at_all_does_no_work(api, fake_root):
    _appear(fake_root, NEW_UID)
    assert api.adopt_paired() == []


def test_the_flash_records_the_pairing_before_waiting(paths, live_registry_text, fake_root):
    """Ordering is the point: the wait timing out is the case this covers, so
    recording after it would help in exactly the situations it does not."""
    from klipper_updater.jobs import JobRunner

    from .conftest import write_settings
    from .test_agent_dfu import ONE_BOARD

    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    write_settings(paths, dry_run="true", service_backend="null", enable_flashing="true")
    os.makedirs(paths.artifact_dir("bttebb36"), exist_ok=True)
    with open(paths.bin_file("bttebb36", "katapult"), "wb") as fh:
        fh.write(b"\0" * 512)

    runner = JobRunner(
        paths,
        lambda: __import__(
            "klipper_updater.settings", fromlist=["load_settings"]
        ).load_settings(paths.settings_file),
    )
    api = Api(paths, runner=runner)
    api.ADD_MCU_REENUMERATE_TIMEOUT = 1.0

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr("klipper_updater.flash.subprocess.run", _FakeRun(ONE_BOARD))
    monkeypatch.setattr("klipper_updater.flash.flash_initial_bootloader", lambda *a, **k: None)
    try:
        res = api.dispatch("fw.add_mcu.start", {"name": "bttebb36"})
        assert runner.wait(timeout=30)
        # The board never appeared, so the job found nothing...
        assert runner.get(res["job_id"]).result["candidates"] == []
        # ...and the pairing is what makes that recoverable.
        assert Pairings(paths).type_for(NEW_DFU) == "bttebb36"

        # It turns up two minutes later.
        _appear(fake_root, NEW_UID)
        assert [a["type"] for a in api.adopt_paired()] == ["bttebb36"]
    finally:
        monkeypatch.undo()
        runner._cancel.set()
        runner.wait(timeout=20)


class _FakeRun:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""

    def __call__(self, *args, **kwargs):
        return self
