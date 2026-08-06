"""Per-type config folders moving under config_dir/types/.

The stakes: these folders hold saved menuconfig answers, which are the one thing
in the whole system nobody can regenerate from anything else. A migration that
loses them costs a menuconfig session per board type.
"""

from __future__ import annotations

import os

import pytest

from klipper_updater.errors import ConfigError
from klipper_updater.layout import migrate_type_dirs
from klipper_updater.paths import Paths


@pytest.fixture()
def paths(tmp_path) -> Paths:
    return Paths.from_env(env={"KLIPPER_UPDATER_HOME": str(tmp_path)})


def write(path: str, text: str = "CONFIG_X=y\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_a_legacy_folder_moves_and_keeps_its_contents(paths):
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"), "CONFIG_A=y\n")
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "katapult.config"), "CONFIG_B=y\n")

    assert migrate_type_dirs(paths) == ["bttebb36"]

    assert not os.path.exists(paths.legacy_type_dir("bttebb36"))
    with open(paths.config_file("bttebb36", "klipper"), encoding="utf-8") as fh:
        assert fh.read() == "CONFIG_A=y\n"
    with open(paths.config_file("bttebb36", "katapult"), encoding="utf-8") as fh:
        assert fh.read() == "CONFIG_B=y\n"


def test_menuconfig_backup_files_travel_with_the_folder(paths):
    """`make menuconfig` writes a `.old` beside the config it replaces.

    The folder moves whole rather than file-by-file precisely so that anything
    we did not think to enumerate comes along.
    """
    legacy = paths.legacy_type_dir("hexadistrofusion")
    write(os.path.join(legacy, "klipper.config"))
    write(os.path.join(legacy, "klipper.config.old"), "previous\n")

    migrate_type_dirs(paths)

    with open(os.path.join(paths.type_dir("hexadistrofusion"), "klipper.config.old")) as fh:
        assert fh.read() == "previous\n"


def test_every_type_moves_not_just_the_first(paths):
    for name in ("OctopusMAXEZ", "bttebb36", "flylllplusbuffer"):
        write(os.path.join(paths.legacy_type_dir(name), "klipper.config"))

    assert migrate_type_dirs(paths) == ["OctopusMAXEZ", "bttebb36", "flylllplusbuffer"]
    for name in ("OctopusMAXEZ", "bttebb36", "flylllplusbuffer"):
        assert os.path.isfile(paths.config_file(name, "klipper"))


def test_a_folder_with_no_saved_config_is_left_alone(paths):
    """config_dir is served by Moonraker and opens in a file browser.

    Users put things there. Moving a directory on the strength of its position
    alone would eventually relocate somebody's notes.
    """
    write(os.path.join(paths.config_dir, "my_backups", "printer.cfg.bak"), "[mcu]\n")

    assert migrate_type_dirs(paths) == []
    assert os.path.isfile(os.path.join(paths.config_dir, "my_backups", "printer.cfg.bak"))


def test_the_registry_file_itself_is_not_a_folder_and_is_untouched(paths):
    write(paths.main_config, "[updater]\n")
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"))

    migrate_type_dirs(paths)

    with open(paths.main_config, encoding="utf-8") as fh:
        assert fh.read() == "[updater]\n"


def test_running_twice_changes_nothing_the_second_time(paths):
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"))

    assert migrate_type_dirs(paths) == ["bttebb36"]
    assert migrate_type_dirs(paths) == []
    assert os.path.isfile(paths.config_file("bttebb36", "klipper"))


def test_an_already_migrated_install_is_not_disturbed(paths):
    write(paths.config_file("bttebb36", "klipper"), "CONFIG_A=y\n")

    assert migrate_type_dirs(paths) == []
    with open(paths.config_file("bttebb36", "klipper"), encoding="utf-8") as fh:
        assert fh.read() == "CONFIG_A=y\n"


def test_a_missing_config_dir_is_not_an_error(paths):
    """A fresh install has nothing at all yet, and this runs before everything."""
    assert not os.path.exists(paths.config_dir)
    assert migrate_type_dirs(paths) == []


def test_a_collision_refuses_rather_than_picking_one(paths):
    """Two folders for one type means one of them is invisible from then on.

    Building from a config the user cannot see is the failure this whole project
    exists to prevent, so it stops instead.
    """
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"), "OLD\n")
    write(paths.config_file("bttebb36", "klipper"), "NEW\n")

    with pytest.raises(ConfigError) as exc:
        migrate_type_dirs(paths)
    assert "bttebb36" in str(exc.value)

    # Neither copy is touched - the user decides which one is current.
    with open(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config")) as fh:
        assert fh.read() == "OLD\n"
    with open(paths.config_file("bttebb36", "klipper"), encoding="utf-8") as fh:
        assert fh.read() == "NEW\n"


def test_a_type_actually_named_types_refuses(paths):
    """The one name that collides with the new root directory."""
    write(os.path.join(paths.legacy_type_dir("types"), "klipper.config"), "MINE\n")

    with pytest.raises(ConfigError) as exc:
        migrate_type_dirs(paths)
    assert "types" in str(exc.value)

    with open(os.path.join(paths.legacy_type_dir("types"), "klipper.config")) as fh:
        assert fh.read() == "MINE\n"


def test_that_refusal_happens_before_anything_has_moved(paths):
    """Otherwise a partial migration leaves types split across two layouts."""
    write(os.path.join(paths.legacy_type_dir("types"), "katapult.config"))
    write(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"))

    with pytest.raises(ConfigError):
        migrate_type_dirs(paths)

    assert os.path.isfile(os.path.join(paths.legacy_type_dir("bttebb36"), "klipper.config"))


def test_a_type_dir_holding_only_artifacts_is_not_ours_to_move(paths):
    """The data tree is a separate directory; a .bin under config/ is a stray."""
    write(os.path.join(paths.config_dir, "leftover", "klipper.bin"), "\x00")

    assert migrate_type_dirs(paths) == []
    assert os.path.isfile(os.path.join(paths.config_dir, "leftover", "klipper.bin"))


# --------------------------------------------------------------------------
# wiring
#
# The migration is only reached from two places, and it is the kind of call that
# looks removable to anyone tidying an entry point. If either loses it, an
# existing install quietly reports every type as unconfigured.
# --------------------------------------------------------------------------


def test_the_cli_migrates_before_running_a_command(tmp_path, monkeypatch, capsys):
    from klipper_updater import cli

    monkeypatch.setenv("KLIPPER_UPDATER_HOME", str(tmp_path))
    monkeypatch.setenv("KLIPPER_UPDATER_FAKE_BUS", str(tmp_path / "bus"))
    os.makedirs(tmp_path / "bus")

    p = Paths.from_env()
    write(p.main_config, "[updater]\n")
    write(os.path.join(p.legacy_type_dir("bttebb36"), "klipper.config"), "CONFIG_A=y\n")

    cli.main(["status"])

    assert os.path.isfile(p.config_file("bttebb36", "klipper"))
    assert "bttebb36" in capsys.readouterr().err


def test_the_agent_migrates_before_serving(tmp_path, monkeypatch):
    from klipper_updater.agent import __main__ as agent_main

    monkeypatch.setenv("KLIPPER_UPDATER_HOME", str(tmp_path))

    p = Paths.from_env()
    write(os.path.join(p.legacy_type_dir("bttebb36"), "klipper.config"), "CONFIG_A=y\n")

    class StubAgent:
        def __init__(self, *a, **kw) -> None:
            pass

        def reconcile_startup(self) -> None:
            pass

        def run_forever(self) -> None:
            pass

    monkeypatch.setattr(agent_main, "Agent", StubAgent)
    monkeypatch.setattr(agent_main, "wait_for_socket", lambda *a, **kw: True)

    assert agent_main.main(["--wait-for-socket", "0"]) == 0
    assert os.path.isfile(p.config_file("bttebb36", "klipper"))


def test_the_agent_survives_a_migration_it_cannot_do(tmp_path, monkeypatch):
    """Exiting would mean a systemd restart loop, taking the panel - and the
    status that would explain the problem - offline with it."""
    from klipper_updater.agent import __main__ as agent_main

    monkeypatch.setenv("KLIPPER_UPDATER_HOME", str(tmp_path))

    p = Paths.from_env()
    write(os.path.join(p.legacy_type_dir("bttebb36"), "klipper.config"), "OLD\n")
    write(p.config_file("bttebb36", "klipper"), "NEW\n")

    class StubAgent:
        def __init__(self, *a, **kw) -> None:
            pass

        def reconcile_startup(self) -> None:
            pass

        def run_forever(self) -> None:
            pass

    monkeypatch.setattr(agent_main, "Agent", StubAgent)
    monkeypatch.setattr(agent_main, "wait_for_socket", lambda *a, **kw: True)

    assert agent_main.main(["--wait-for-socket", "0"]) == 0
