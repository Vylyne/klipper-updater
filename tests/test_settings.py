from __future__ import annotations

import pytest

from klipper_updater.config import Registry
from klipper_updater.errors import ConfigError
from klipper_updater.settings import (
    Settings,
    legacy_settings_warning,
    load_settings,
    save_settings,
)


def test_missing_file_yields_defaults(paths):
    s = load_settings(paths.settings_file)
    assert s == Settings()
    assert s.clean_before_build is True
    assert s.enable_flashing is False  # agent-side gate, off until deliberately enabled


def test_values_are_parsed_with_the_right_types(paths):
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write(
            "[updater]\n"
            "make_jobs = 4\n"
            "clean_before_build = false\n"
            "dry_run = yes\n"
            "service = klipper-1\n"
            "service_backend = systemd\n"
        )
    s = load_settings(paths.settings_file)
    assert s.make_jobs == 4
    assert s.clean_before_build is False
    assert s.dry_run is True
    assert s.service == "klipper-1"
    assert s.service_backend == "systemd"


def test_dashes_are_accepted_as_underscores(paths):
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\nclean-before-build = false\n")
    assert load_settings(paths.settings_file).clean_before_build is False


def test_unknown_keys_are_ignored_not_fatal(paths):
    """A newer version may have written a setting this one doesn't know."""
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\nsome_future_option = 7\nmake_jobs = 2\n")
    assert load_settings(paths.settings_file).make_jobs == 2


def test_a_bad_value_raises_rather_than_being_ignored(paths):
    """Silently discarding `dry_run = maybe` is how you flash a board by accident."""
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\ndry_run = maybe\n")
    with pytest.raises(ConfigError):
        load_settings(paths.settings_file)


def test_an_invalid_backend_raises(paths):
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\nservice_backend = telepathy\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(paths.settings_file)
    assert exc.value.data["key"] == "service_backend"


def test_no_section_yields_defaults(paths):
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[something-else]\nfoo = bar\n")
    assert load_settings(paths.settings_file) == Settings()


def test_save_then_load_round_trips(paths):
    original = Settings(make_jobs=3, dry_run=True, service="klipper-2", enable_flashing=True)
    save_settings(paths.settings_file, original)
    assert load_settings(paths.settings_file) == original


# --------------------------------------------------------------------------
# sharing a file with the registry
# --------------------------------------------------------------------------


def test_settings_and_the_registry_are_the_same_file(paths):
    """One file to find and one file to edit - and the reason every test below
    exists, because now a careless write to either destroys the other."""
    assert paths.settings_file == paths.registry_file == paths.main_config


def test_saving_settings_keeps_the_mcu_sections_and_the_comments(paths, live_registry_text):
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(live_registry_text)

    save_settings(paths.settings_file, Settings(enable_flashing=True, make_jobs=3))

    with open(paths.main_config, encoding="utf-8") as fh:
        out = fh.read()
    assert "# mcu-updater configuration." in out
    assert "src/Makefile -> src-y += buffer.c" in out

    reg = Registry.load(paths)
    assert reg.names() == ["bttebb36", "bttmmbv1", "flylllplusbuffer", "sv08Mainboard"]
    assert len(reg.all_serials()) == 10
    assert load_settings(paths.settings_file).enable_flashing is True


def test_saving_the_registry_keeps_the_settings(paths, live_registry_text):
    """The inverse. The panel writes both, and neither write knows about the other."""
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(live_registry_text)
    save_settings(paths.settings_file, Settings(enable_flashing=True))

    reg = Registry.load(paths)
    reg.add_serial("bttebb36", "NEWBOARD-if00")
    reg.save(paths)

    assert load_settings(paths.settings_file).enable_flashing is True
    assert "NEWBOARD-if00" in Registry.load(paths).get("bttebb36").serials


def test_a_registry_only_file_yields_default_settings(paths, live_registry_text):
    """No [updater] section is the normal case: everything has a default."""
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(live_registry_text)
    assert load_settings(paths.settings_file) == Settings()


def test_repeated_saves_do_not_grow_the_file(paths, live_registry_text):
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(live_registry_text)
    for jobs in range(4):
        save_settings(paths.settings_file, Settings(make_jobs=jobs))
    with open(paths.main_config, encoding="utf-8") as fh:
        out = fh.read()
    assert out.count("[updater]") == 1
    # Only the real key - the sample also carries a commented-out `#make_jobs: 0`
    # documenting the default, which must survive and must not be counted.
    assert out.count("\nmake_jobs:") == 1
    assert "#make_jobs: 0" in out
    assert "\n\n\n" not in out


def test_a_second_updater_section_is_refused(paths):
    """Appending a block rather than editing the existing one is the natural
    mistake, and first-wins would make `enable_flashing: true` do nothing at all."""
    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[updater]\nmake_jobs: 2\n\n[updater]\nenable_flashing: true\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(paths.settings_file)
    assert "more than one [updater]" in str(exc.value)


def test_a_duplicate_mcu_section_is_refused(paths):
    """Same trap on the registry side: the second board's serials would vanish."""
    from klipper_updater.errors import ConfigCorruptError

    with open(paths.main_config, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[mcu a]\nchipset: x\nserials:\n    S1\n\n[mcu a]\nserials:\n    S2\n")
    with pytest.raises(ConfigCorruptError) as exc:
        Registry.load(paths)
    assert "[mcu a]" in str(exc.value)


def test_a_leftover_updater_conf_is_reported_not_silently_ignored(paths):
    """Settings reverting to defaults is safe, but enable_flashing going back to
    false makes the flash buttons vanish with no explanation."""
    assert legacy_settings_warning(paths) is None

    with open(paths.legacy_settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\nenable_flashing = true\n")

    warning = legacy_settings_warning(paths)
    assert warning is not None
    assert "updater.conf" in warning
    assert "mcu-updater.cfg" in warning
    # and it is genuinely no longer read
    assert load_settings(paths.settings_file).enable_flashing is False


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [(0, []), (1, ["-j1"]), (8, ["-j8"])],
)
def test_make_flags(jobs, expected):
    assert Settings(make_jobs=jobs).make_flags() == expected


def test_negative_jobs_means_auto():
    flags = Settings(make_jobs=-1).make_flags()
    assert len(flags) == 1 and flags[0].startswith("-j")
