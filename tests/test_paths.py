"""The seam the entire test suite depends on."""

from __future__ import annotations

import os

from klipper_updater.paths import DEFAULT_SERIAL_BY_ID, Paths


def test_defaults_follow_the_printer_data_layout():
    """Hand-edited config under config/, build artifacts beside it - never in it."""
    p = Paths.from_env(env={"KLIPPER_UPDATER_HOME": os.path.join("/srv", "printer")})
    root = os.path.abspath(os.path.join("/srv", "printer", "printer_data"))
    assert p.config_dir == os.path.join(root, "config", "mcu-updater")
    assert p.data_dir == os.path.join(root, "mcu-updater")
    assert p.registry_file == os.path.join(p.config_dir, "mcu-updater.cfg")
    assert p.serial_by_id == DEFAULT_SERIAL_BY_ID


def test_artifacts_are_kept_out_of_the_backed_up_config_tree():
    """git-based backup tools commit everything under config/; binaries would mean
    a churn commit after every build."""
    p = Paths.from_env(env={"KLIPPER_UPDATER_HOME": os.path.join("/srv", "printer")})
    assert p.config_file("board", "klipper").startswith(p.config_dir)
    for artifact in (
        p.bin_file("board", "klipper"),
        p.uf2_file("board", "klipper"),
        p.sidecar_file("board", "klipper"),
        p.lock_file,
        p.journal_file,
    ):
        assert artifact.startswith(p.data_dir)
        assert os.path.join("config", "mcu-updater") not in artifact


def test_each_override_is_honoured_independently(tmp_path):
    p = Paths.from_env(
        env={
            "KLIPPER_UPDATER_HOME": str(tmp_path / "home"),
            "KLIPPER_UPDATER_CONFIG_DIR": str(tmp_path / "elsewhere"),
            "KLIPPER_UPDATER_DATA_DIR": str(tmp_path / "artifacts"),
            "KLIPPER_UPDATER_FAKE_BUS": str(tmp_path / "bus"),
            "KLIPPER_UPDATER_PRINTER_DATA": str(tmp_path / "pdata"),
        }
    )
    assert p.config_dir == str(tmp_path / "elsewhere")
    assert p.data_dir == str(tmp_path / "artifacts")
    assert p.serial_by_id == str(tmp_path / "bus")
    assert p.printer_data == str(tmp_path / "pdata")
    assert p.moonraker_sock == os.path.join(str(tmp_path / "pdata"), "comms", "moonraker.sock")


def test_an_explicit_home_beats_the_environment(tmp_path):
    p = Paths.from_env(
        home=str(tmp_path / "explicit"),
        env={"KLIPPER_UPDATER_HOME": str(tmp_path / "ignored")},
    )
    assert p.home == str(tmp_path / "explicit")


def test_per_type_layout(tmp_path):
    p = Paths.from_env(env={"KLIPPER_UPDATER_HOME": str(tmp_path)})
    assert p.type_dir("bttebb36").endswith(os.path.join("mcu-updater", "bttebb36"))
    assert p.config_file("bttebb36", "klipper").endswith("klipper.config")
    assert p.bin_file("bttebb36", "klipper").endswith("klipper.bin")
    assert p.uf2_file("bttebb36", "katapult").endswith("katapult.uf2")
    assert p.sidecar_file("bttebb36", "klipper").endswith("klipper.build.json")


def test_source_tree_layout(tmp_path):
    p = Paths.from_env(env={"KLIPPER_UPDATER_HOME": str(tmp_path)})
    assert p.fw_dir("klipper") == os.path.join(str(tmp_path), "klipper")
    assert p.flashtool.endswith(os.path.join("katapult", "scripts", "flashtool.py"))
    assert p.kconfiglib("klipper").endswith(
        os.path.join("klipper", "lib", "kconfiglib", "kconfiglib.py")
    )
    assert p.built_artifact("klipper") == os.path.join(str(tmp_path), "klipper", "out", "klipper.bin")
    assert p.built_artifact("katapult", "uf2").endswith("katapult.uf2")


def test_paths_are_frozen():
    import dataclasses

    p = Paths.from_env(env={"KLIPPER_UPDATER_HOME": "/tmp/x"})
    assert dataclasses.is_dataclass(p)
    try:
        p.home = "/other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("Paths should be immutable")
