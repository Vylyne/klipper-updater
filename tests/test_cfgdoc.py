"""The .cfg round-tripper.

The whole reason this module exists is that configparser eats comments on write.
Most of these tests are therefore about what *survives* an edit, not about
parsing.
"""

from __future__ import annotations

from klipper_updater.cfgdoc import CfgDocument

SAMPLE = """\
# Klipper Updater MCU registry.
# One [mcu <name>] section per board model.

[mcu sv08Mainboard]
chipset: stm32f103xe
serials:
    36FFD9054755303923891357-if00

# The buffer patch is specific to this batch of boards.
[mcu flylllplusbuffer]
chipset: stm32f072xb
serials:
    4C0033000957465331323720-if00
    3F0037000957465331323720-if00
klipper_makefile_patches:
    src/Makefile -> src-y += buffer.c
"""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_sections_are_found_in_file_order():
    doc = CfgDocument(SAMPLE)
    assert doc.section_names() == ["mcu sv08Mainboard", "mcu flylllplusbuffer"]


def test_prefix_filtering():
    doc = CfgDocument(SAMPLE + "\n[updater]\ndry_run: true\n")
    assert doc.section_names("mcu") == ["mcu sv08Mainboard", "mcu flylllplusbuffer"]
    assert doc.section_names("updater") == ["updater"]


def test_single_line_values():
    doc = CfgDocument(SAMPLE)
    assert doc.get("mcu sv08Mainboard", "chipset") == "stm32f103xe"


def test_multi_line_values_become_lists():
    doc = CfgDocument(SAMPLE)
    assert doc.get_list("mcu flylllplusbuffer", "serials") == [
        "4C0033000957465331323720-if00",
        "3F0037000957465331323720-if00",
    ]


def test_a_missing_key_returns_the_default():
    doc = CfgDocument(SAMPLE)
    assert doc.get("mcu sv08Mainboard", "nope") is None
    assert doc.get("mcu sv08Mainboard", "nope", "fallback") == "fallback"
    assert doc.get_list("mcu sv08Mainboard", "nope") == []


def test_equals_is_accepted_as_a_separator():
    doc = CfgDocument("[a]\nkey = value\n")
    assert doc.get("a", "key") == "value"


def test_a_value_containing_a_colon_survives():
    """Makefile lines and paths contain colons; only the first separator counts."""
    doc = CfgDocument("[a]\nline: src/Makefile -> foo: bar\n")
    assert doc.get("a", "line") == "src/Makefile -> foo: bar"


def test_an_empty_document_is_usable():
    doc = CfgDocument()
    assert doc.section_names() == []
    doc.set("mcu x", "chipset", "rp2040")
    assert doc.get("mcu x", "chipset") == "rp2040"


def test_a_duplicate_section_keeps_the_first():
    """Last-wins would let a stray paste silently shadow a real board."""
    doc = CfgDocument("[mcu a]\nchipset: one\n\n[mcu a]\nchipset: two\n")
    assert doc.get("mcu a", "chipset") == "one"


# --------------------------------------------------------------------------
# what survives a write - the point of the module
# --------------------------------------------------------------------------


def test_an_untouched_document_round_trips_byte_identically():
    assert CfgDocument(SAMPLE).render() == SAMPLE


def test_comments_survive_an_edit():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu sv08Mainboard", "chipset", "stm32f103ze")
    out = doc.render()
    assert "# Klipper Updater MCU registry." in out
    assert "# The buffer patch is specific to this batch of boards." in out
    assert "chipset: stm32f103ze" in out


def test_blank_lines_and_ordering_survive_an_edit():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu flylllplusbuffer", "chipset", "stm32f072x8")
    out = doc.render()
    assert out.index("[mcu sv08Mainboard]") < out.index("[mcu flylllplusbuffer]")
    assert "\n\n# The buffer patch" in out


def test_unrecognised_keys_survive():
    """A key written by a newer version must not be dropped by an older one."""
    doc = CfgDocument("[mcu a]\nchipset: rp2040\nfuture_option: 42\n")
    doc.set("mcu a", "chipset", "stm32f072xb")
    assert "future_option: 42" in doc.render()


def test_appending_to_a_list_keeps_the_others():
    doc = CfgDocument(SAMPLE)
    serials = doc.get_list("mcu flylllplusbuffer", "serials")
    doc.set("mcu flylllplusbuffer", "serials", serials + ["NEW-if00"])
    out = doc.render()
    assert "4C0033000957465331323720-if00" in out
    assert "NEW-if00" in out
    assert "# The buffer patch is specific to this batch of boards." in out


def test_shrinking_a_list_removes_only_its_own_lines():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu flylllplusbuffer", "serials", ["4C0033000957465331323720-if00"])
    out = doc.render()
    assert "3F0037000957465331323720-if00" not in out
    assert "klipper_makefile_patches:" in out, "the next key must not be swallowed"
    assert "src/Makefile -> src-y += buffer.c" in out


def test_a_new_key_lands_inside_its_section():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu sv08Mainboard", "katapult_installed", "true")
    out = doc.render()
    body = out.split("[mcu sv08Mainboard]")[1].split("[mcu")[0]
    assert "katapult_installed: true" in body


def test_a_new_section_is_appended_with_separation():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu hexa", "chipset", "stm32f072xb")
    out = doc.render()
    assert out.rstrip().endswith("chipset: stm32f072xb")
    assert "\n\n[mcu hexa]" in out


def test_removing_an_option_leaves_the_rest_intact():
    doc = CfgDocument(SAMPLE)
    assert doc.remove_option("mcu flylllplusbuffer", "klipper_makefile_patches") is True
    out = doc.render()
    assert "src-y += buffer.c" not in out
    assert "3F0037000957465331323720-if00" in out
    assert doc.remove_option("mcu flylllplusbuffer", "nope") is False


def test_removing_a_section_takes_its_comment_free_gap_with_it():
    doc = CfgDocument(SAMPLE)
    assert doc.remove_section("mcu sv08Mainboard") is True
    out = doc.render()
    assert "sv08Mainboard" not in out
    assert "[mcu flylllplusbuffer]" in out
    assert "# Klipper Updater MCU registry." in out
    assert doc.remove_section("mcu nope") is False


def test_repeated_edits_do_not_accumulate_blank_lines():
    doc = CfgDocument(SAMPLE)
    for i in range(5):
        doc.set("mcu sv08Mainboard", "chipset", f"chip{i}")
        doc.set("mcu sv08Mainboard", "serials", [f"S{i}-if00"])
    out = doc.render()
    assert "\n\n\n" not in out, "edits should not grow the file"
    assert out.count("chipset:") == 2


def test_a_document_reparses_equal_after_a_write():
    """Render then reload must give the same view, or edits drift over time."""
    doc = CfgDocument(SAMPLE)
    doc.set("mcu hexa", "chipset", "stm32f072xb")
    doc.set("mcu hexa", "serials", ["4B0036000A53594731383520-if00"])

    again = CfgDocument(doc.render())
    assert again.section_names() == doc.section_names()
    assert again.get("mcu hexa", "chipset") == "stm32f072xb"
    assert again.get_list("mcu hexa", "serials") == ["4B0036000A53594731383520-if00"]
    assert again.render() == doc.render()


def test_setting_an_empty_list_keeps_the_key_but_no_items():
    doc = CfgDocument(SAMPLE)
    doc.set("mcu flylllplusbuffer", "serials", [])
    assert "serials:" in doc.render()
    assert doc.get_list("mcu flylllplusbuffer", "serials") == []
