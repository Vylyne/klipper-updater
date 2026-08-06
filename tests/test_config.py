from __future__ import annotations

import os
import pathlib
import sys

import pytest

from klipper_updater.cfgdoc import CfgDocument
from klipper_updater.config import MakefilePatch, Registry, section_name, validate_type_name
from klipper_updater.errors import (
    AmbiguousSerialError,
    ConfigCorruptError,
    ConfigError,
    DuplicateTypeError,
    InvalidTypeNameError,
    SerialTrackedElsewhereError,
    UnknownSerialError,
    UnknownTypeError,
)


def _write(paths, text: str) -> None:
    os.makedirs(paths.config_dir, exist_ok=True)
    with open(paths.registry_file, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _read(paths) -> str:
    with open(paths.registry_file, encoding="utf-8") as fh:
        return fh.read()


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


def test_makefile_patches_parse_from_the_arrow_form(paths, live_registry_text):
    _write(paths, live_registry_text)
    patches = Registry.load(paths).get("flylllplusbuffer").fw("klipper").makefile_patches
    assert [p.to_json() for p in patches] == [
        {"file": "src/Makefile", "line": "src-y += buffer.c"}
    ]


def test_a_patch_line_containing_an_arrow_or_colon_survives(paths):
    _write(
        paths,
        "[mcu a]\nchipset: x\nklipper_makefile_patches:\n"
        "    src/Makefile -> src-y += a->b:c.c\nserials:\n",
    )
    patch = Registry.load(paths).get("a").fw("klipper").makefile_patches[0]
    assert patch.file == "src/Makefile"
    assert patch.line == "src-y += a->b:c.c"


def test_a_malformed_patch_is_refused_rather_than_silently_dropped(paths):
    """Silently ignoring it means a board quietly builds without its extra source
    file - which is exactly the class of bug this whole key exists to fix."""
    _write(paths, "[mcu a]\nchipset: x\nklipper_makefile_patches:\n    nonsense\n")
    with pytest.raises(ConfigCorruptError) as exc:
        Registry.load(paths)
    assert "->" in str(exc.value)


# --------------------------------------------------------------------------
# write fidelity - the reason for the custom document
# --------------------------------------------------------------------------


def test_an_unchanged_registry_round_trips_byte_identically(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.save(paths)
    assert _read(paths) == live_registry_text


def test_comments_survive_the_panel_adding_a_serial(paths, live_registry_text):
    """The whole point of moving to .cfg: people annotate this file, and the panel
    writes to it structurally."""
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.add_serial("bttebb36", "NEWBOARD-if00")
    reg.save(paths)

    out = _read(paths)
    assert "# mcu-updater configuration." in out
    assert "NEWBOARD-if00" in out
    assert "src/Makefile -> src-y += buffer.c" in out


def test_a_hand_written_comment_inside_a_section_survives(paths):
    _write(
        paths,
        "[mcu a]\n# this board is fussy about its clock\nchipset: stm32f072xb\n"
        "serials:\n    S1\n",
    )
    reg = Registry.load(paths)
    reg.add_serial("a", "S2")
    reg.save(paths)
    out = _read(paths)
    assert "# this board is fussy about its clock" in out
    assert "S1" in out and "S2" in out


def test_unrecognised_keys_survive(paths):
    """A key written by a newer version must not be dropped by an older one."""
    _write(paths, "[mcu a]\nchipset: rp2040\nfuture_option: 42\nserials:\n    S1\n")
    reg = Registry.load(paths)
    reg.add_serial("a", "S2")
    reg.save(paths)
    assert "future_option: 42" in _read(paths)


def test_repeated_edits_do_not_grow_the_file(paths, live_registry_text):
    _write(paths, live_registry_text)
    for i in range(5):
        reg = Registry.load(paths)
        reg.add_serial("bttmmbv1", f"S{i}-if00")
        reg.save(paths)
    out = _read(paths)
    assert "\n\n\n" not in out
    assert out.count("[mcu bttmmbv1]") == 1


def test_removing_a_type_removes_only_its_section(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.remove_type("bttmmbv1")
    reg.save(paths)
    out = _read(paths)
    assert "bttmmbv1" not in out
    assert "[mcu bttebb36]" in out
    assert "# mcu-updater configuration." in out


def test_a_new_type_is_appended_and_reloads(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    reg.add_type("hexa", "stm32f072xb")
    reg.add_serial("hexa", "4B0036000A53594731383520-if00")
    reg.save(paths)

    again = Registry.load(paths)
    assert again.get("hexa").chipset == "stm32f072xb"
    assert again.get("hexa").serials == ["4B0036000A53594731383520-if00"]
    assert len(again) == 5


def test_defaults_are_not_restated_in_the_file(paths):
    """A file full of restated defaults is harder to read and to diff."""
    reg = Registry.load(paths)
    reg.add_type("a", "rp2040")
    reg.save(paths)
    out = _read(paths)
    assert "katapult_installed" not in out
    assert "extra_args" not in out
    assert "makefile_patches" not in out


def test_katapult_installed_false_is_written_and_read_back(paths):
    reg = Registry.load(paths)
    reg.add_type("a", "rp2040", katapult_installed=False)
    reg.save(paths)
    assert "katapult_installed: false" in _read(paths)
    assert Registry.load(paths).get("a").katapult_installed is False


def test_clearing_extra_args_removes_the_key(paths):
    _write(paths, "[mcu a]\nchipset: x\nklipper_extra_args: -j4\nserials:\n")
    reg = Registry.load(paths)
    reg.get("a").fw("klipper").extra_args = ""
    reg.save(paths)
    assert "klipper_extra_args" not in _read(paths)


def test_a_patch_added_programmatically_round_trips(paths):
    reg = Registry.load(paths)
    mcu = reg.add_type("a", "stm32f072xb")
    mcu.fw("klipper").makefile_patches = [
        MakefilePatch(file="src/Makefile", line="src-y += buffer.c")
    ]
    reg.save(paths)
    assert "src/Makefile -> src-y += buffer.c" in _read(paths)

    reloaded = Registry.load(paths).get("a").fw("klipper").makefile_patches
    assert reloaded[0].file == "src/Makefile"
    assert reloaded[0].line == "src-y += buffer.c"


# --------------------------------------------------------------------------
# the legacy guard
# --------------------------------------------------------------------------


def test_a_legacy_json_registry_is_refused_not_ignored(paths, fake_root):
    """Reporting "no MCU types" would let the next add-type write a fresh file
    while the real registry sat untouched in the old location."""
    (fake_root / "mcus").mkdir(exist_ok=True)
    (fake_root / "mcus" / "mcus.json").write_text('{"a": {}}', encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        Registry.load(paths)
    assert "old location" in str(exc.value)
    assert "mcu-updater.cfg" in str(exc.value)


def test_a_fresh_install_with_no_files_at_all_is_empty(paths):
    assert len(Registry.load(paths)) == 0


def test_the_guard_does_not_fire_once_the_new_file_exists(paths, fake_root, live_registry_text):
    """Having both is fine - the new one wins, the old is simply ignored."""
    (fake_root / "mcus").mkdir(exist_ok=True)
    (fake_root / "mcus" / "mcus.json").write_text('{"a": {}}', encoding="utf-8")
    _write(paths, live_registry_text)
    assert len(Registry.load(paths)) == 4


# --------------------------------------------------------------------------
# lookups and mutation
# --------------------------------------------------------------------------


def test_unknown_type_raises_with_the_known_list(paths, live_registry_text):
    _write(paths, live_registry_text)
    with pytest.raises(UnknownTypeError) as exc:
        Registry.load(paths).get("nope")
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


def test_resolve_serial_unique_match(paths, live_registry_text):
    _write(paths, live_registry_text)
    assert (
        Registry.load(paths).resolve_serial("36FFD9054755303923891357-if00") == "sv08Mainboard"
    )


def test_resolve_serial_untracked(paths, live_registry_text):
    _write(paths, live_registry_text)
    with pytest.raises(UnknownSerialError):
        Registry.load(paths).resolve_serial("does-not-exist")


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
    _write(paths, live_registry_text)
    with pytest.raises(SerialTrackedElsewhereError) as exc:
        Registry.load(paths).resolve_serial("36FFD9054755303923891357-if00", "bttmmbv1")
    assert exc.value.data["tracked_under"] == ["sv08Mainboard"]


def test_resolve_serial_with_matching_type(paths, live_registry_text):
    _write(paths, live_registry_text)
    reg = Registry.load(paths)
    assert (
        reg.resolve_serial("36FFD9054755303923891357-if00", "sv08Mainboard") == "sv08Mainboard"
    )


def test_katapult_installed_defaults_to_true_when_absent(paths):
    _write(paths, "[mcu a]\nchipset: x\nserials:\n")
    assert Registry.load(paths).get("a").katapult_installed is True


def test_section_naming_is_stable():
    """User-visible; changing the prefix would orphan every existing file."""
    assert section_name("bttebb36") == "mcu bttebb36"


def test_a_section_without_a_name_is_ignored(paths):
    _write(paths, "[mcu]\nchipset: x\n\n[mcu real]\nchipset: y\nserials:\n")
    assert Registry.load(paths).names() == ["real"]


def test_the_file_stays_valid_klipper_style_cfg(paths, live_registry_text):
    """It has to remain parseable by anything else that reads Klipper configs."""
    _write(paths, live_registry_text)
    doc = CfgDocument(live_registry_text)
    assert doc.section_names("mcu")
    assert doc.get("mcu sv08Mainboard", "chipset") == "stm32f103xe"


def test_the_pre_merge_registry_filename_is_guarded(paths):
    """mcus.cfg in the right directory, before it was merged with the settings.
    The likeliest upgrade path, and the one where silently starting empty would
    do the most damage."""
    old = pathlib.Path(paths.config_dir) / "mcus.cfg"
    old.write_text("[mcu a]\nchipset: x\nserials:\n    S1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        Registry.load(paths)
    assert "mcus.cfg" in str(exc.value)
    assert "mcu-updater.cfg" in str(exc.value)


def test_the_intermediate_config_dir_is_also_guarded(paths, fake_root):
    """The config directory was renamed with the project. Someone who pulled in
    between would otherwise have their registry silently ignored."""
    old = fake_root / "printer_data" / "config" / "klipper-updater"
    old.mkdir(parents=True, exist_ok=True)
    (old / "mcus.cfg").write_text("[mcu a]\nchipset: x\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        Registry.load(paths)
    assert "klipper-updater" in str(exc.value)
    assert "mcu-updater" in str(exc.value)


# --------------------------------------------------------------------------
# Registry.mutate - atomic load-modify-write
# --------------------------------------------------------------------------


def test_mutate_reads_inside_the_lock_so_it_cannot_clobber(paths, live_registry_text):
    """save() rewrites the whole document, so a Registry loaded before someone
    else's edit would erase it on save. The agent and the CLI are separate
    processes that both write this file, so mutate() must re-read, not trust a
    caller's earlier load."""
    _write(paths, live_registry_text)
    stale = Registry.load(paths)
    assert "hexa" not in stale.names()

    # Somebody else adds a type after `stale` was read.
    other = Registry.load(paths)
    other.add_type("hexa", "stm32f072xb")
    other.save(paths)

    with Registry.mutate(paths, "add serial") as reg:
        reg.add_serial("bttebb36", "LATER-if00")

    final = Registry.load(paths)
    assert "hexa" in final.names(), "mutate() used stale state and erased a type"
    assert "LATER-if00" in final.get("bttebb36").serials


def test_mutate_writes_nothing_if_the_body_raises(paths, live_registry_text):
    """A validation failure must leave the file exactly as it was, not half-edited."""
    _write(paths, live_registry_text)
    before = _read(paths)

    with pytest.raises(UnknownTypeError):
        with Registry.mutate(paths, "bad edit") as reg:
            reg.add_serial("bttebb36", "PARTIAL-if00")
            reg.get("does-not-exist")  # raises after a mutation was already made

    assert _read(paths) == before


def test_mutate_uses_its_own_lock_file(paths):
    """Registry edits must not queue behind a build holding the main lock for
    minutes - they touch different things."""
    assert paths.registry_lock_file != paths.lock_file

    from klipper_updater.lock import exclusive

    with exclusive(paths, "a long build"):
        with Registry.mutate(paths, "add a serial anyway") as reg:
            reg.add_type("a", "rp2040")
    assert "a" in Registry.load(paths).names()


def test_mutate_records_who_is_editing(paths, live_registry_text):
    """Portable half of the locking check: the lock is taken, and labelled."""
    from klipper_updater.lock import ExclusiveLock

    _write(paths, live_registry_text)
    with Registry.mutate(paths, "add serial FOO") as reg:
        reg.add_serial("bttebb36", "FOO-if00")
        held = ExclusiveLock(paths, path=paths.registry_lock_file)._record()
        assert held is not None
        assert held["label"] == "add serial FOO"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is unavailable on Windows; the lock degrades to a no-op there",
)
def test_a_concurrent_mutation_is_refused_rather_than_interleaved(paths, live_registry_text):
    """Two interleaved load-modify-writes lose one of the edits silently, which is
    the whole failure mode this phase has to avoid. Refusing is the safe answer -
    the lock is held for sub-milliseconds, so a real collision needs bad luck and
    the caller can simply retry."""
    from klipper_updater.errors import BusyError

    _write(paths, live_registry_text)
    with Registry.mutate(paths, "first") as reg:
        reg.add_serial("bttebb36", "FIRST-if00")
        with pytest.raises(BusyError):
            with Registry.mutate(paths, "second") as other:
                other.add_serial("bttebb36", "SECOND-if00")

    final = Registry.load(paths)
    assert "FIRST-if00" in final.get("bttebb36").serials
    assert "SECOND-if00" not in final.get("bttebb36").serials


# --------------------------------------------------------------------------
# validate_type_name - the name is also a path component
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hexa", "sv08Mainboard", "btt-ebb36", "a.b_c", "OctopusMAXEZ"])
def test_real_type_names_are_accepted(name):
    assert validate_type_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "../../etc",  # traversal out of the config tree
        "foo/bar",
        "foo\bar",
        "..",
        ".",
        "a]b",  # would produce a section header that no longer parses
        "[mcu x",
        "with space",
        " lead",
        "trail ",
        "",
        "   ",
        "x" * 65,
    ],
)
def test_unsafe_type_names_are_refused(name):
    with pytest.raises(InvalidTypeNameError):
        validate_type_name(name)


def test_add_type_applies_the_rule_so_both_front_ends_agree(paths):
    """Enforced in the model rather than in the CLI and the agent separately."""
    reg = Registry.load(paths)
    with pytest.raises(InvalidTypeNameError):
        reg.add_type("../escape", "rp2040")
    assert reg.names() == []


# --------------------------------------------------------------------------
# an annotated config
#
# Reported from the printer: labelling each serial with its toolhead is the
# obvious thing to do by hand, and it silently unregistered boards.
# --------------------------------------------------------------------------

ANNOTATED = """[updater]
enable_flashing: true   # turned on for the panel

[mcu bttebb36]
chipset: stm32g0b1xx
serials:
    # the two toolhead boards
    230048001750304158373620-if00  #mcu EBBT0
    290055001850304158373620-if00  #mcu EBBT1
"""


def test_an_annotated_registry_still_tracks_every_board(paths):
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(ANNOTATED)

    reg = Registry.load(paths)
    assert reg.get("bttebb36").serials == [
        "230048001750304158373620-if00",
        "290055001850304158373620-if00",
    ]


def test_the_labels_survive_adopting_another_board(paths):
    """The panel's "track this" writes the whole block back. Losing the labels
    would leave the user unable to tell which serial is which toolhead."""
    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(ANNOTATED)

    reg = Registry.load(paths)
    reg.add_serial("bttebb36", "NEWBOARD-if00")
    reg.save(paths)

    text = open(paths.registry_file, encoding="utf-8").read()
    assert "#mcu EBBT0" in text
    assert "#mcu EBBT1" in text
    assert "# the two toolhead boards" in text
    assert Registry.load(paths).get("bttebb36").serials[-1] == "NEWBOARD-if00"


def test_an_inline_comment_on_a_setting_is_not_part_of_its_value(paths):
    """`enable_flashing: true   # turned on` must parse as true, not as the string
    "true   # turned on" - which parse_bool would reject, silently leaving
    flashing disabled."""
    from klipper_updater.settings import load_settings

    with open(paths.registry_file, "w", encoding="utf-8") as fh:
        fh.write(ANNOTATED)
    assert load_settings(paths.settings_file).enable_flashing is True
