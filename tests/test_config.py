from __future__ import annotations

import json

import pytest

from klipper_updater.config import FwConfig, Registry
from klipper_updater.errors import (
    AmbiguousSerialError,
    ConfigCorruptError,
    DuplicateTypeError,
    SerialTrackedElsewhereError,
    UnknownSerialError,
    UnknownTypeError,
)


def _write(paths, text: str) -> None:
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# the real registry
# --------------------------------------------------------------------------


def test_loads_the_live_registry(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)

    assert reg.names() == ["bttebb36", "bttmmbv1", "flylllplusbuffer", "sv08Mainboard"]
    assert len(reg.all_serials()) == 10
    assert len(reg.get("flylllplusbuffer").serials) == 6
    assert reg.get("sv08Mainboard").chipset == "stm32f103xe"


def test_live_registry_uses_only_the_three_known_fw_keys(live_registry_text):
    """Guards the schema claim the whole design rests on."""
    raw = json.loads(live_registry_text)
    seen = set()
    for body in raw.values():
        for fw in ("klipper", "katapult"):
            seen |= set(body.get(fw, {}))
    assert seen == {"extra_args", "installed", "makefile_patches"}
    assert "extra_src" not in live_registry_text


def test_round_trip_is_byte_identical(paths, live_registry_text):
    """Saving must not reorder keys, drop makefile_patches, or reformat.

    This file is hand-edited on a live printer; a lossy save is data loss.
    """
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.save(paths)
    after = open(paths.mcus_json, encoding="utf-8").read()
    assert json.loads(after) == json.loads(live_registry_text)
    # Key order preserved too, not just equal contents.
    assert list(json.loads(after)) == list(json.loads(live_registry_text))
    assert (
        json.loads(after)["flylllplusbuffer"]["klipper"]["makefile_patches"][0]["line"]
        == "src-y += buffer.c"
    )


def test_unknown_keys_are_preserved(paths):
    _write(
        paths,
        json.dumps(
            {
                "board": {
                    "chipset": "stm32f072xb",
                    "klipper": {"extra_args": "", "future_key": 42},
                    "serials": ["A"],
                    "notes": "hand-written",
                }
            }
        ),
    )
    reg = Registry.load(paths)
    reg.add_serial("board", "B")
    reg.save(paths)

    out = json.load(open(paths.mcus_json, encoding="utf-8"))
    assert out["board"]["notes"] == "hand-written"
    assert out["board"]["klipper"]["future_key"] == 42
    assert out["board"]["serials"] == ["A", "B"]


# --------------------------------------------------------------------------
# corrupt input
# --------------------------------------------------------------------------


def test_corrupt_json_raises_rather_than_reading_as_empty(paths):
    """The original returned {} here, so one stray comma looked exactly like
    'no MCU types configured' - and the next add-type would overwrite the lot."""
    _write(paths, '{"a": {"chipset": "x",, }}')
    with pytest.raises(ConfigCorruptError) as exc:
        Registry.load(paths)
    assert exc.value.code == "config_corrupt"
    assert "line" in exc.value.data


def test_non_object_json_raises(paths):
    _write(paths, "[1, 2, 3]")
    with pytest.raises(ConfigCorruptError):
        Registry.load(paths)


def test_missing_and_empty_files_are_empty_registries(paths):
    assert len(Registry.load(paths)) == 0
    _write(paths, "")
    assert len(Registry.load(paths)) == 0


# --------------------------------------------------------------------------
# legacy extra_src
# --------------------------------------------------------------------------


def test_legacy_extra_src_makefile_line_becomes_a_patch():
    """extra_src was never a synonym for extra_args - it was appended to
    src/Makefile. Passing it to make would produce bogus goals and fail."""
    cfg = FwConfig.from_json({"extra_src": "src-$(CONFIG_MACH_STM32F072) += buffer.c"})
    assert cfg.extra_args == ""
    assert len(cfg.makefile_patches) == 1
    assert cfg.makefile_patches[0].file == "src/Makefile"
    assert cfg.makefile_patches[0].line == "src-$(CONFIG_MACH_STM32F072) += buffer.c"


def test_legacy_extra_src_non_makefile_value_becomes_make_args():
    cfg = FwConfig.from_json({"extra_src": "FOO=bar"})
    assert cfg.extra_args == "FOO=bar"
    assert cfg.makefile_patches == []


def test_legacy_extra_src_empty_is_dropped():
    cfg = FwConfig.from_json({"extra_src": "", "extra_args": ""})
    assert cfg.extra_args == ""
    assert cfg.makefile_patches == []
    assert "extra_src" not in cfg.to_json()


def test_legacy_extra_src_does_not_clobber_real_extra_args():
    cfg = FwConfig.from_json({"extra_src": "FOO=bar", "extra_args": "-j4"})
    assert cfg.extra_args == "-j4"


# --------------------------------------------------------------------------
# lookups and mutation
# --------------------------------------------------------------------------


def test_unknown_type_raises_with_the_known_list(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    with pytest.raises(UnknownTypeError) as exc:
        reg.get("nope")
    assert "bttebb36" in exc.value.data["known"]


def test_duplicate_type_raises_unless_overwriting(paths):
    reg = Registry.load(paths)
    reg.add_type("a", "stm32f072xb")
    with pytest.raises(DuplicateTypeError):
        reg.add_type("a", "stm32f072xb")
    reg.add_type("a", "rp2040", overwrite=True)
    assert reg.get("a").chipset == "rp2040"


def test_add_and_remove_serial_report_whether_they_acted(paths):
    reg = Registry.load(paths)
    reg.add_type("a", "x")
    assert reg.add_serial("a", "S1") is True
    assert reg.add_serial("a", "S1") is False
    assert reg.remove_serial("a", "S1") is True
    assert reg.remove_serial("a", "S1") is False


def test_remove_type_drops_it_from_the_saved_file(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.remove_type("bttmmbv1")
    reg.save(paths)
    assert "bttmmbv1" not in json.load(open(paths.mcus_json, encoding="utf-8"))


def test_resolve_serial_unique_match(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    assert reg.resolve_serial("36FFD9054755303923891357-if00") == "sv08Mainboard"


def test_resolve_serial_untracked(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    with pytest.raises(UnknownSerialError):
        reg.resolve_serial("does-not-exist")


def test_resolve_serial_ambiguous(paths):
    reg = Registry.load(paths)
    reg.add_type("a", "x")
    reg.add_type("b", "x")
    reg.add_serial("a", "SHARED")
    reg.add_serial("b", "SHARED")
    with pytest.raises(AmbiguousSerialError) as exc:
        reg.resolve_serial("SHARED")
    assert exc.value.data["tracked_under"] == ["a", "b"]


def test_resolve_serial_tracked_elsewhere_is_refused_not_offered(paths, live_registry_text):
    """A serial belonging to another type is a strong signal of a wrong -t, so it
    is refused outright rather than offered as an add."""
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    with pytest.raises(SerialTrackedElsewhereError) as exc:
        reg.resolve_serial("36FFD9054755303923891357-if00", "bttmmbv1")
    assert exc.value.data["tracked_under"] == ["sv08Mainboard"]


def test_resolve_serial_with_matching_type(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    assert (
        reg.resolve_serial("36FFD9054755303923891357-if00", "sv08Mainboard")
        == "sv08Mainboard"
    )


def test_katapult_installed_defaults_to_true_when_absent(paths):
    _write(paths, json.dumps({"a": {"chipset": "x", "serials": []}}))
    reg = Registry.load(paths)
    assert reg.get("a").katapult_installed is True


def test_katapult_installed_false_is_honoured(paths):
    _write(paths, json.dumps({"a": {"chipset": "x", "katapult": {"installed": False}}}))
    reg = Registry.load(paths)
    assert reg.get("a").katapult_installed is False
