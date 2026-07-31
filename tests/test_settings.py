from __future__ import annotations

import pytest

from klipper_updater.errors import ConfigError
from klipper_updater.settings import Settings, load_settings, save_settings


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


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [(0, []), (1, ["-j1"]), (8, ["-j8"])],
)
def test_make_flags(jobs, expected):
    assert Settings(make_jobs=jobs).make_flags() == expected


def test_negative_jobs_means_auto():
    flags = Settings(make_jobs=-1).make_flags()
    assert len(flags) == 1 and flags[0].startswith("-j")
