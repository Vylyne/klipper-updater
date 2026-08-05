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
from klipper_updater.kconfig import (
    Serializer,
    SessionStore,
    _srctree,
    load_kconfiglib,
)
from klipper_updater.paths import Paths

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


# --------------------------------------------------------------------------
# sessions: navigation, editing, persistence
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fake printer host with klipper and katapult trees that both parse."""
    for name in ("klipper", "katapult"):
        make_tree(tmp_path, name)
    (tmp_path / "printer_data" / "config" / "mcu-updater").mkdir(parents=True)
    (tmp_path / "printer_data" / "mcu-updater").mkdir(parents=True)
    paths = Paths.from_env(env={"KLIPPER_UPDATER_HOME": str(tmp_path)})
    return SessionStore(paths)


@pytest.fixture
def session(store):
    return store.open("bttebb36", "klipper")


def choice_row(session):
    return next(n for n in session.menu()["nodes"] if n["kind"] == "choice")


def row(session, name):
    return next(n for n in session.menu()["nodes"] if n["name"] == name)


def test_a_new_session_starts_clean_at_the_top(session):
    m = session.menu()
    assert m["dirty"] is False
    assert m["revision"] == 0
    assert len(m["breadcrumb"]) == 1
    assert m["fw"] == "klipper"


def test_a_missing_config_file_is_not_an_error(session):
    """It means this type has never been configured, and the Kconfig defaults are
    the right place to start."""
    assert not os.path.exists(session.config_path)
    assert row(session, "BOARD_NAME")["value"] == "testboard"


def test_a_choice_reports_its_options_not_its_own_tristate(session):
    """kconfiglib's `assignable` on a Choice describes whether the choice is
    enabled - ('y',) for any ordinary one. Reporting that made every choice look
    unchangeable, because there was only ever one value in it."""
    ch = choice_row(session)
    assert ch["assignable"] == ["MACH_STM32", "MACH_RP2040"]
    assert ch["editable"] is True
    assert ch["value"] == "MACH_STM32"


def test_flipping_a_choice_rewrites_the_menu_and_reports_what_moved(session):
    ch = choice_row(session)
    result = session.set_value(ch["id"], "MACH_RP2040")

    names = [n["name"] for n in result["nodes"]]
    assert "RP2040_FLASH_SIZE" in names
    assert "STM32_CLOCK_REF" not in names
    assert "MACH_RP2040" in result["changed"]
    assert result["dirty"] is True
    assert result["revision"] == 1


def test_an_option_that_is_not_in_the_choice_is_refused(session):
    with pytest.raises(KconfigError) as exc:
        session.set_value(choice_row(session)["id"], "MACH_NONSENSE")
    assert "not one of this choice's options" in str(exc.value)


def test_an_int_outside_its_range_is_refused_and_not_silently_dropped(session):
    """The dangerous case. kconfiglib's set_value returns **True** for an
    out-of-range int and leaves the symbol at its default, so trusting the return
    value would report success while changing nothing."""
    session.set_value("STM32_CLOCK_REF", "16")
    assert row(session, "STM32_CLOCK_REF")["value"] == "16"

    with pytest.raises(KconfigError) as exc:
        session.set_value("STM32_CLOCK_REF", "99")
    assert "outside the allowed range 4..32" in str(exc.value)
    assert row(session, "STM32_CLOCK_REF")["value"] == "16", "the old value must survive a refusal"


def test_a_value_of_the_wrong_shape_is_refused(session):
    with pytest.raises(KconfigError):
        session.set_value("STM32_CLOCK_REF", "not-a-number")


def test_a_bool_only_accepts_what_is_assignable(session):
    with pytest.raises(KconfigError) as exc:
        session.set_value("WITH_HELP", "m")  # bool, so m is not on offer
    assert "accepts" in str(exc.value)


def test_a_symbol_held_by_a_select_cannot_be_set(session):
    """Because kconfiglib narrows assignable to the forced value rather than
    emptying it, this is the check that stops a pointless assignment."""
    menu_id = next(n["id"] for n in session.menu()["nodes"] if n["kind"] == "menu")
    session.enter(menu_id)
    session.set_value("WANT_USB", "y")  # selects USBSERIAL

    with pytest.raises(KconfigError) as exc:
        session.set_value("USBSERIAL", "n")
    assert "held by another symbol" in str(exc.value)


def test_a_menu_or_comment_has_no_value_to_set(session):
    menu_id = next(n["id"] for n in session.menu()["nodes"] if n["kind"] == "menu")
    with pytest.raises(KconfigError) as exc:
        session.set_value(menu_id, "y")
    assert "not something with a value" in str(exc.value)


def test_an_unknown_node_id_is_refused(session):
    with pytest.raises(KconfigError) as exc:
        session.set_value("NO_SUCH_SYMBOL", "y")
    assert "no such config entry" in str(exc.value)


# -- navigation ------------------------------------------------------------


def test_entering_and_leaving_a_menu(session):
    menu_id = next(n["id"] for n in session.menu()["nodes"] if n["kind"] == "menu")
    inside = session.enter(menu_id)
    assert [b["prompt"] for b in inside["breadcrumb"]] == [
        "Test Firmware Configuration",
        "Communication interface",
    ]
    assert any(n["name"] == "USBSERIAL" for n in inside["nodes"])

    assert len(session.up()["breadcrumb"]) == 1


def test_up_from_the_top_stays_at_the_top(session):
    assert len(session.up()["breadcrumb"]) == 1


def test_entering_something_with_nothing_inside_is_refused(session):
    with pytest.raises(KconfigError):
        session.enter(row(session, "BOARD_NAME")["id"])


def test_a_change_that_hides_the_current_menu_falls_back_to_an_ancestor(session):
    """Flipping a choice can delete the screen you are standing on. menuconfig
    reanchors to the nearest surviving ancestor; an empty screen with no way back
    out of it would be worse."""
    choice_id = choice_row(session)["id"]
    session.set_value(choice_id, "MACH_RP2040")
    rp2040_menu = next(
        n["id"] for n in session.menu()["nodes"] if n["prompt"] == "RP2040 specifics"
    )
    session.enter(rp2040_menu)
    assert len(session.menu()["breadcrumb"]) == 2

    # Set by id from inside a submenu, which works because _find searches the whole
    # tree - the node being changed need not be on the current screen. This one
    # deletes the screen we are standing on.
    result = session.set_value(choice_id, "MACH_STM32")
    assert len(result["breadcrumb"]) == 1
    assert result["breadcrumb"][0]["prompt"] == "Test Firmware Configuration"


def test_a_change_that_leaves_the_menu_alone_does_not_move_you(session):
    """The other half: reanchoring must only fire when it has to."""
    menu_id = next(
        n["id"] for n in session.menu()["nodes"] if n["prompt"] == "Communication interface"
    )
    session.enter(menu_id)
    session.set_value("USB_VENDOR_ID", "0x2e8a")
    assert len(session.menu()["breadcrumb"]) == 2


def test_a_choice_option_cannot_be_set_on_its_own(session):
    """Setting the option symbol directly is refused, and the message has to point
    at the choice rather than blame a select that does not exist."""
    with pytest.raises(KconfigError) as exc:
        session.set_value("MACH_RP2040", "y")
    assert "one option of a choice" in str(exc.value)
    assert exc.value.data["choice"]


# -- help and search -------------------------------------------------------


def test_help_is_fetched_per_symbol(session):
    assert "several hundred KB" in session.help("WITH_HELP")["help"]
    assert session.help("BOARD_NAME")["help"] == ""


def test_search_finds_by_name_and_by_prompt(session):
    by_name = session.search("BOARD")
    assert "BOARD_NAME" in [n["name"] for n in by_name["nodes"]]

    by_prompt = session.search("crystal")
    assert "STM32_CLOCK_REF" in [n["name"] for n in by_prompt["nodes"]]


def test_search_only_returns_visible_symbols(session):
    assert "RP2040_FLASH_SIZE" not in [n["name"] for n in session.search("flash")["nodes"]]
    session.set_value(choice_row(session)["id"], "MACH_RP2040")
    assert "RP2040_FLASH_SIZE" in [n["name"] for n in session.search("flash")["nodes"]]


def test_an_empty_search_returns_nothing_rather_than_everything(session):
    assert session.search("   ")["nodes"] == []


# -- persistence -----------------------------------------------------------


def test_save_writes_the_config_and_clears_dirty(session):
    session.set_value("BOARD_NAME", "mine")
    result = session.save()

    assert os.path.isfile(result["path"])
    assert result["backup"] is None  # nothing to back up on a first save
    assert session.dirty is False
    with open(result["path"], encoding="utf-8") as fh:
        assert 'CONFIG_BOARD_NAME="mine"' in fh.read()


def test_a_second_save_keeps_one_generation_of_backup(session):
    session.set_value("BOARD_NAME", "first")
    session.save()
    session.set_value("BOARD_NAME", "second")
    result = session.save()

    assert result["backup"] is not None
    with open(result["backup"], encoding="utf-8") as fh:
        assert 'CONFIG_BOARD_NAME="first"' in fh.read()


def test_save_leaves_no_temp_file_behind(session):
    session.set_value("BOARD_NAME", "x")
    session.save()
    assert not os.path.exists(session.config_path + ".tmp")


def test_saved_answers_survive_a_reparse(store):
    first = store.open("bttebb36", "klipper")
    first.set_value("BOARD_NAME", "persisted")
    first.set_value(choice_row(first)["id"], "MACH_RP2040")
    first.save()

    again = store.open("bttebb36", "klipper")
    assert row(again, "BOARD_NAME")["value"] == "persisted"
    assert choice_row(again)["value"] == "MACH_RP2040"


def test_reset_throws_away_unsaved_edits(session):
    session.set_value("BOARD_NAME", "unsaved")
    assert session.dirty is True

    session.reset()
    assert session.dirty is False
    assert row(session, "BOARD_NAME")["value"] == "testboard"


# -- the store -------------------------------------------------------------


def test_sessions_are_keyed_opaquely_not_by_target(store):
    """Two tabs on the same target must not share one Kconfig object, or one
    save would silently discard the other's edits."""
    a = store.open("bttebb36", "klipper")
    b = store.open("bttebb36", "klipper")
    assert a.id != b.id
    assert a is not b

    a.set_value("BOARD_NAME", "from-a")
    assert row(b, "BOARD_NAME")["value"] == "testboard"


def test_an_unknown_session_says_it_may_have_expired(store):
    with pytest.raises(KconfigError) as exc:
        store.get("kc-999")
    assert "expired" in str(exc.value)


def test_closing_a_session_frees_it(store):
    session = store.open("bttebb36", "klipper")
    assert store.close(session.id) is True
    assert store.close(session.id) is False
    with pytest.raises(KconfigError):
        store.get(session.id)


def test_an_idle_session_expires(store, monkeypatch):
    session = store.open("bttebb36", "klipper")
    monkeypatch.setattr(type(store), "TTL", 0.0)
    with pytest.raises(KconfigError):
        store.get(session.id)


def test_the_store_is_bounded_and_evicts_clean_sessions_first(store):
    """Each parsed tree costs a few MB and a closed browser tab never says so, so
    the cap has to be enforced by eviction rather than by refusal - and unsaved
    work is the last thing to go."""
    monkey = [store.open("bttebb36", "klipper") for _ in range(store.MAX)]
    dirty = monkey[0]
    dirty.set_value("BOARD_NAME", "precious")

    store.open("bttebb36", "klipper")  # over the cap

    assert store.get(dirty.id) is dirty, "a dirty session must not be evicted first"


def test_a_dirty_session_on_the_same_target_is_discoverable(store):
    """So a caller can warn rather than let one save overwrite another's work."""
    first = store.open("bttebb36", "klipper")
    assert store.dirty_for("bttebb36", "klipper") is None

    first.set_value("BOARD_NAME", "editing")
    assert store.dirty_for("bttebb36", "klipper") is first
    assert store.dirty_for("bttebb36", "katapult") is None


def test_klipper_and_katapult_are_separate_targets(store):
    """Both trees vendor their own kconfiglib, which is the two-distinct-modules
    case the serializer is built for."""
    k = store.open("bttebb36", "klipper")
    b = store.open("bttebb36", "katapult")
    assert k.config_path != b.config_path
    assert k.serializer is not b.serializer

    k.set_value("BOARD_NAME", "klipper-side")
    k.save()
    b.set_value("BOARD_NAME", "katapult-side")
    b.save()

    assert row(store.open("bttebb36", "klipper"), "BOARD_NAME")["value"] == "klipper-side"
    assert row(store.open("bttebb36", "katapult"), "BOARD_NAME")["value"] == "katapult-side"


# --------------------------------------------------------------------------
# _same_value - the read-back comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "requested", "actual", "same"),
    [
        ("int", "16", "16", True),
        ("int", "16", "8", False),
        ("int", " 16 ", "16", True),
        # A hex symbol can come back in a different case or without the prefix, so
        # comparing the strings would report a spurious refusal for a value that
        # was accepted exactly as asked.
        ("hex", "0x1d50", "0x1D50", True),
        ("hex", "0x1d50", "1d50", True),
        ("hex", "0x1d50", "0x2e8a", False),
        ("string", "testboard", "testboard", True),
        ("string", "a", "b", False),
        ("bool", "y", "y", True),
        ("bool", "y", "n", False),
        # Garbage on either side must never compare equal.
        ("int", "abc", "8", False),
        ("hex", "nope", "0x10", False),
    ],
)
def test_same_value_normalises_only_where_it_should(kind, requested, actual, same):
    from klipper_updater.kconfig import _same_value

    assert _same_value(kind, requested, actual) is same


# --------------------------------------------------------------------------
# enterable - what is a screen and what is a control
#
# The first real Katapult tree exposed both of these. Nothing here was covered
# before: the tests asserted a menu *is* enterable and never that anything is not.
# --------------------------------------------------------------------------


def test_a_choice_is_not_enterable(session):
    """Descending into a choice showed its raw option symbols as individual
    switches. Each one is correctly unsettable on its own - you set the choice, not
    the option - so the screen was three padlocked toggles and no way to change
    anything."""
    assert choice_row(session)["enterable"] is False


def test_a_choices_options_are_not_listed_as_rows(session):
    """They are the choice's `assignable`. Listing them as rows would show each as
    its own locked control alongside the select that actually works."""
    names = [n["name"] for n in session.menu()["nodes"]]
    assert "MACH_STM32" not in names
    assert "MACH_RP2040" not in names
    assert choice_row(session)["assignable"] == ["MACH_STM32", "MACH_RP2040"]


def test_a_symbol_with_an_implicit_submenu_is_not_enterable(session):
    """Its children are flattened into this screen at depth+1, so offering to enter
    it as well would show the same rows twice in two places."""
    menu_id = next(n["id"] for n in session.menu()["nodes"] if n["prompt"] == "Communication interface")
    rows = {n["name"]: n for n in session.enter(menu_id)["nodes"] if n["name"]}

    assert rows["USBSERIAL"]["enterable"] is False
    assert rows["USB_VENDOR_ID"]["depth"] == rows["USBSERIAL"]["depth"] + 1


def test_a_menuconfig_symbol_is_its_own_screen(session):
    """The one symbol kind that is enterable: it is both a value and a menu."""
    advanced = row(session, "ADVANCED")
    assert advanced["enterable"] is True
    assert advanced["kind"] == "bool"

    # Its children live behind it, not inline.
    assert "ADVANCED_TWEAK" not in [n["name"] for n in session.menu()["nodes"] if n["name"]]
    session.set_value("ADVANCED", "y")
    inside = session.enter(row(session, "ADVANCED")["id"])
    assert "ADVANCED_TWEAK" in [n["name"] for n in inside["nodes"] if n["name"]]


def test_entering_a_choice_is_refused(session):
    with pytest.raises(KconfigError):
        session.enter(choice_row(session)["id"])


# --------------------------------------------------------------------------
# friendly naming
#
# The first real Katapult tree rendered its dropdowns as MACH_STM32 /
# STM32_FLASH_START_0000 - the symbol names, which are what must be *sent* but not
# what should be *shown*.
# --------------------------------------------------------------------------


def test_a_choice_carries_prompts_for_display_and_names_for_sending(session):
    ch = choice_row(session)
    assert ch["options"] == [
        {"value": "MACH_STM32", "label": "STMicroelectronics STM32"},
        {"value": "MACH_RP2040", "label": "Raspberry Pi RP2040"},
    ]
    # The value stays the identifier; the label is only for reading.
    assert ch["value"] == "MACH_STM32"
    assert ch["value_label"] == "STMicroelectronics STM32"
    assert ch["assignable"] == ["MACH_STM32", "MACH_RP2040"]


def test_the_label_follows_the_selection(session):
    session.set_value(choice_row(session)["id"], "MACH_RP2040")
    ch = choice_row(session)
    assert ch["value"] == "MACH_RP2040"
    assert ch["value_label"] == "Raspberry Pi RP2040"


def test_options_are_absent_for_anything_that_is_not_a_choice(session):
    """So the select cannot accidentally be handed an empty item list."""
    for name in ("BOARD_NAME", "WITH_HELP", "STM32_CLOCK_REF"):
        node = row(session, name)
        assert node["options"] is None, name
        assert node["value_label"] is None, name


def test_an_option_with_no_prompt_falls_back_to_its_name(tmp_path):
    """Rare, but a label of "" would render as an empty dropdown entry that cannot
    be told apart from the others."""
    tree = make_tree(tmp_path, "klipper")
    (tree / "src" / "Kconfig").write_text(
        'mainmenu "T"\n'
        "choice\n"
        '    prompt "Pick"\n'
        "config OPT_NAMED\n"
        '    bool "Has a prompt"\n'
        "config OPT_BARE\n"
        "    bool\n"
        "endchoice\n",
        encoding="utf-8",
    )
    kconf, s = parse(tree)
    ch = next(r for r in s.menu(kconf.top_node.list) if r["kind"] == "choice")
    labels = {o["value"]: o["label"] for o in ch["options"]}
    assert labels.get("OPT_NAMED") == "Has a prompt"
    # A bare option has no prompt at all, so kconfiglib does not offer it - but if
    # it ever does, it must not come through with an empty label.
    assert all(o["label"] for o in ch["options"])
