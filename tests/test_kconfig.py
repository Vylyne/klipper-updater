"""The kconfig loader and serializer, against the real kconfiglib.

Deliberately not against a stub. Both failure modes worth testing here only exist
in the genuine library: dependency evaluation is kconfiglib's own expression
engine, and the per-tree module identity problem needs two real copies to
reproduce. A fake with shared classes would pass while the real thing broke.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from klipper_updater.errors import KconfigError
from klipper_updater.kconfig import Serializer, _srctree, load_kconfiglib

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
VENDORED = FIXTURES / "kconfiglib" / "kconfiglib.py"
SAMPLE_KCONFIG = FIXTURES / "kconfig_tree" / "Kconfig"


def make_tree(root: pathlib.Path, name: str = "klipper") -> pathlib.Path:
    """A firmware tree shaped like klipper's: src/Kconfig plus its own kconfiglib."""
    tree = root / name
    (tree / "src").mkdir(parents=True)
    (tree / "lib" / "kconfiglib").mkdir(parents=True)
    shutil.copy(VENDORED, tree / "lib" / "kconfiglib" / "kconfiglib.py")
    shutil.copy(SAMPLE_KCONFIG, tree / "src" / "Kconfig")
    return tree


@pytest.fixture
def tree(tmp_path):
    return make_tree(tmp_path)


def parse(tree: pathlib.Path):
    """Load the tree's own kconfiglib and parse it, returning (kconf, serializer)."""
    mod = load_kconfiglib(str(tree))
    with _srctree(str(tree)):
        kconf = mod.Kconfig("src/Kconfig", warn_to_stderr=False)
    return kconf, Serializer(mod)


def rows_by_name(serializer: Serializer, kconf) -> dict:
    return {r["name"]: r for r in serializer.menu(kconf.top_node.list) if r["name"]}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def test_the_library_comes_from_the_tree(tree):
    mod = load_kconfiglib(str(tree))
    assert mod.__file__ is not None
    assert os.path.realpath(mod.__file__) == os.path.realpath(
        str(tree / "lib" / "kconfiglib" / "kconfiglib.py")
    )


def test_a_tree_with_no_vendored_kconfiglib_is_refused(tmp_path):
    """No falling back to a system copy: a different version would disagree with
    the Kconfig files it is parsing."""
    bare = tmp_path / "bare"
    (bare / "src").mkdir(parents=True)
    with pytest.raises(KconfigError) as exc:
        load_kconfiglib(str(bare))
    assert "vendored kconfiglib" in str(exc.value)


def test_loading_does_not_touch_sys_path_or_shadow_a_system_copy(tree):
    import sys

    before = list(sys.path)
    mod = load_kconfiglib(str(tree))
    assert sys.path == before
    assert "kconfiglib" not in sys.modules
    assert mod.__name__.startswith("_ku_kconfiglib_")


def test_the_same_tree_yields_the_same_module_object(tree):
    """Caching is correctness, not speed: two module objects for one tree would
    have mutually unrecognisable classes."""
    assert load_kconfiglib(str(tree)) is load_kconfiglib(str(tree))


def test_two_trees_yield_distinct_modules_whose_classes_do_not_interoperate(tmp_path):
    """The trap, reproduced. Klipper and Katapult vendor separate copies.

    The sentinels are plain ints and compare fine across copies - it is the
    *classes* that differ, so isinstance against the wrong copy silently says "not
    a symbol" and every node would serialize as unknown, with no error anywhere.
    """
    klipper = make_tree(tmp_path, "klipper")
    katapult = make_tree(tmp_path, "katapult")

    a = load_kconfiglib(str(klipper))
    b = load_kconfiglib(str(katapult))
    assert a is not b

    # Constants: safe across copies.
    assert a.BOOL == b.BOOL
    assert a.MENU == b.MENU

    # Classes: not safe, which is the whole reason Serializer takes its module.
    assert a.Symbol is not b.Symbol

    kconf, _ = parse(klipper)
    node = kconf.top_node.list.list  # first option inside the choice
    assert isinstance(node.item, a.Symbol)
    assert not isinstance(node.item, b.Symbol)


def test_a_serializer_built_with_the_wrong_module_reports_nothing_useful(tmp_path):
    """Demonstrates the consequence, so the guard in Serializer has a reason a
    future reader can see rather than a warning they have to trust."""
    klipper = make_tree(tmp_path, "klipper")
    katapult = make_tree(tmp_path, "katapult")
    kconf, correct = parse(klipper)
    wrong = Serializer(load_kconfiglib(str(katapult)))

    node = kconf.top_node.list  # the choice
    assert correct.kind(node) == "choice"
    assert wrong.kind(node) == "unknown"


# --------------------------------------------------------------------------
# no process-global chdir
# --------------------------------------------------------------------------


def test_parsing_does_not_change_the_working_directory(tree):
    """chdir is process-global and this runs in a multithreaded agent, so holding
    one would break any other thread using a relative path."""
    before = os.getcwd()
    parse(tree)
    assert os.getcwd() == before


def test_srctree_is_restored_including_its_absence(tree):
    os.environ.pop("srctree", None)
    parse(tree)
    assert "srctree" not in os.environ

    os.environ["srctree"] = "/somewhere/else"
    try:
        parse(tree)
        assert os.environ["srctree"] == "/somewhere/else"
    finally:
        os.environ.pop("srctree", None)


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------


def test_the_top_menu_serializes_with_the_expected_kinds(tree):
    kconf, s = parse(tree)
    rows = s.menu(kconf.top_node.list)
    kinds = [(r["kind"], r["name"] or r["prompt"]) for r in rows]

    assert ("choice", "Micro-controller Architecture") in kinds
    assert ("int", "STM32_CLOCK_REF") in kinds
    assert ("menu", "Communication interface") in kinds
    assert ("string", "BOARD_NAME") in kinds
    assert ("bool", "WITH_HELP") in kinds
    assert not any(k == "unknown" for k, _ in kinds)


def test_flipping_the_choice_flips_what_is_visible(tree):
    """The reason dependency evaluation is left to kconfiglib rather than
    reimplemented: one assignment rewrites which half of the tree exists."""
    kconf, s = parse(tree)

    assert "STM32_CLOCK_REF" in rows_by_name(s, kconf)
    assert "RP2040_FLASH_SIZE" not in rows_by_name(s, kconf)

    kconf.syms["MACH_RP2040"].set_value(2)

    assert "STM32_CLOCK_REF" not in rows_by_name(s, kconf)
    assert "RP2040_FLASH_SIZE" in rows_by_name(s, kconf)


def test_a_selected_symbol_reports_as_not_assignable(tree):
    """The difference between "off" and "not yours to set". Inferring assignable
    from the type would get this wrong, so it comes from kconfiglib."""
    kconf, s = parse(tree)
    menu = next(n for n in walk(kconf.top_node.list) if n.prompt and n.prompt[0] == "Communication interface")

    before = {r["name"]: r for r in s.menu(menu.list) if r["name"]}
    assert "y" in before["USBSERIAL"]["assignable"]

    kconf.syms["WANT_USB"].set_value(2)  # selects USBSERIAL

    after = {r["name"]: r for r in s.menu(menu.list) if r["name"]}
    assert after["USBSERIAL"]["value"] == "y"
    # kconfiglib narrows assignable to the forced value rather than emptying it, so
    # a control gated on `assignable` alone would render as a switch that silently
    # refuses to move. `editable` is the flag that actually means "not yours".
    assert after["USBSERIAL"]["assignable"] == ["y"]
    assert after["USBSERIAL"]["editable"] is False

    # ...whereas before the select it genuinely could be turned off.
    assert before["USBSERIAL"]["editable"] is True


def test_an_int_reports_its_resolved_range(tree):
    kconf, s = parse(tree)
    row = rows_by_name(s, kconf)["STM32_CLOCK_REF"]
    assert row["range"] == {"min": "4", "max": "32"}


def test_a_string_has_no_range_and_a_placeholder_assignable(tree):
    kconf, s = parse(tree)
    row = rows_by_name(s, kconf)["BOARD_NAME"]
    assert row["range"] is None
    assert row["assignable"] == ["<value>"]
    assert row["value"] == "testboard"


def test_help_is_flagged_but_not_included(tree):
    """Klipper's full help text is several hundred KB against 40-80 KB for the tree
    without it, and almost none of it is ever read."""
    kconf, s = parse(tree)
    rows = rows_by_name(s, kconf)
    assert rows["WITH_HELP"]["has_help"] is True
    assert rows["BOARD_NAME"]["has_help"] is False
    assert not any("help" in r for r in rows.values())


def test_help_is_fetchable_on_demand(tree):
    from klipper_updater.kconfig import help_for

    kconf, _ = parse(tree)
    node = kconf.syms["WITH_HELP"].nodes[0]
    assert "several hundred KB" in help_for(node)
    assert help_for(kconf.syms["BOARD_NAME"].nodes[0]) == ""


def test_an_implicit_dependency_submenu_is_flattened_into_its_parent(tree):
    """USB_VENDOR_ID only exists because USBSERIAL is on, so kconfiglib nests it.
    menuconfig shows that as an indent rather than a separate screen, and so do we."""
    kconf, s = parse(tree)
    menu = next(n for n in walk(kconf.top_node.list) if n.prompt and n.prompt[0] == "Communication interface")
    rows = s.menu(menu.list)

    usb = next(r for r in rows if r["name"] == "USBSERIAL")
    vid = next(r for r in rows if r["name"] == "USB_VENDOR_ID")
    assert vid["depth"] == usb["depth"] + 1


def test_a_real_menu_is_enterable_and_not_flattened(tree):
    kconf, s = parse(tree)
    rows = s.menu(kconf.top_node.list)
    menu = next(r for r in rows if r["kind"] == "menu")
    assert menu["enterable"] is True
    # Its children belong to their own screen, not to the top menu.
    assert not any(r["name"] == "USBSERIAL" for r in rows)


def test_node_ids_are_stable_across_a_reparse(tree):
    """The panel round-trips these, so they cannot be positional."""
    first = {r["id"] for r in parse(tree)[1].menu(parse(tree)[0].top_node.list)}
    kconf, s = parse(tree)
    assert {r["id"] for r in s.menu(kconf.top_node.list)} == first


def walk(node):
    """Every node in the tree, depth first - test helper only."""
    while node:
        yield node
        if node.list:
            yield from walk(node.list)
        node = node.next
