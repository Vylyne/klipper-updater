"""Reading and editing a firmware tree's Kconfig from something other than a TTY.

``menuconfig`` is ncurses and needs a terminal, which is the last thing in this
tool that cannot be driven from a browser. This module loads the *tree's own*
kconfiglib and turns its menus into JSON.

Three things here are non-obvious, and all three were established by experiment
against kconfiglib 14.1.0 rather than assumed.

**The library comes from the firmware tree, never from pip.** Klipper vendors a
locally patched kconfiglib at ``lib/kconfiglib/``; Katapult vendors its own
separate copy. A PyPI kconfiglib would silently disagree with the ``Kconfig``
files it is parsing. So each tree's copy is loaded from its own path, under its
own private module name, without touching ``sys.path``.

**Loading two trees yields two distinct module objects, and the hazard is
``isinstance``, not the constants.** The sentinels - ``MENU``, ``COMMENT``,
``BOOL``, ``STRING`` and so on - are plain small ints, equal across copies, so
comparing them across modules works fine. The *classes* are what differ:
``isinstance(node.item, other_copy.Symbol)`` is ``False``, so any code that
discriminates node kinds using a different copy's classes classifies every symbol
as "not a symbol" and reports nothing useful, with no error anywhere. That is why
:class:`Serializer` takes its module in the constructor and never reads a
module-level constant.

**No ``os.chdir`` is required.** Setting ``srctree`` and passing absolute paths is
enough for parsing, ``load_config`` and ``write_config`` alike - verified with the
cwd deliberately elsewhere and a ``source`` statement in play. That matters
because ``chdir`` is process-global and this runs inside a multithreaded agent, so
holding one for the duration of an operation would break any other thread using a
relative path. ``srctree`` is still an environment variable and therefore also
process-global, but it is only read while the ``Kconfig`` object is constructed -
so it is set, used and restored under a lock, which is a far narrower window than
a chdir would have been.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import threading
import time
from collections.abc import Iterator
from types import ModuleType
from typing import Any, Optional

from .errors import KconfigError
from .paths import Paths

#: Where each firmware tree keeps the copy of kconfiglib it expects to be used.
VENDORED_KCONFIGLIB = os.path.join("lib", "kconfiglib", "kconfiglib.py")

#: One module object per tree, keyed by realpath. Loading the same file twice
#: would produce two module objects whose classes are mutually unrecognisable, so
#: this cache is a correctness measure and not only an optimisation.
_modules: dict[str, ModuleType] = {}
_modules_lock = threading.Lock()

#: Serialises the srctree environment variable, which kconfiglib reads while a
#: Kconfig object is being constructed. Process-global state, so only ever held
#: around that construction.
_srctree_lock = threading.Lock()


def kconfiglib_path(fw_dir: str) -> str:
    return os.path.join(fw_dir, VENDORED_KCONFIGLIB)


def load_kconfiglib(fw_dir: str) -> ModuleType:
    """Import the kconfiglib vendored inside `fw_dir`.

    Under a private module name derived from the realpath, so two trees never
    collide in ``sys.modules`` and neither shadows a system-wide kconfiglib that
    might also be installed.
    """
    path = kconfiglib_path(fw_dir)
    if not os.path.isfile(path):
        raise KconfigError(
            f"no vendored kconfiglib at {path}. The firmware tree supplies the "
            f"library that understands its own Kconfig files, so this cannot fall "
            f"back to a system copy - a different version would disagree with the "
            f"files it is parsing.",
            path=path,
        )

    real = os.path.realpath(path)
    with _modules_lock:
        cached = _modules.get(real)
        if cached is not None:
            return cached

        key = "_ku_kconfiglib_" + hashlib.sha1(real.encode("utf-8")).hexdigest()[:10]
        spec = importlib.util.spec_from_file_location(key, real)
        if spec is None or spec.loader is None:
            raise KconfigError(f"could not load {real} as a module", path=real)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
            raise KconfigError(f"could not import {real}: {exc}", path=real) from exc
        _modules[real] = module
        return module


@contextlib.contextmanager
def _srctree(fw_dir: str) -> Iterator[None]:
    """Point kconfiglib at `fw_dir` for the duration of a parse.

    Restores the previous value, including restoring *absence*, so a tree parsed
    inside another tree's window cannot inherit the wrong root.
    """
    with _srctree_lock:
        previous = os.environ.get("srctree")
        os.environ["srctree"] = fw_dir
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("srctree", None)
            else:
                os.environ["srctree"] = previous


class Serializer:
    """Turns kconfiglib nodes into JSON, using one tree's own module.

    Constructed with the module that loaded the tree. Every class and constant it
    compares against comes from `self._m`, never from an import at the top of this
    file - because two trees' classes are different objects and cross-comparing
    them silently classifies everything as unknown.
    """

    def __init__(self, module: ModuleType) -> None:
        self._m = module

    # -- classification ----------------------------------------------------

    def is_menu(self, node: Any) -> bool:
        return node.item == self._m.MENU

    def is_comment(self, node: Any) -> bool:
        return node.item == self._m.COMMENT

    def is_symbol(self, node: Any) -> bool:
        return isinstance(node.item, self._m.Symbol)

    def is_choice(self, node: Any) -> bool:
        return isinstance(node.item, self._m.Choice)

    def kind(self, node: Any) -> str:
        """One of menu, comment, choice, bool, tristate, string, int, hex, unknown."""
        if self.is_menu(node):
            return "menu"
        if self.is_comment(node):
            return "comment"
        if self.is_choice(node):
            return "choice"
        if self.is_symbol(node):
            return self.type_name(node.item.orig_type)
        return "unknown"

    def type_name(self, orig_type: Any) -> str:
        m = self._m
        return {
            m.BOOL: "bool",
            m.TRISTATE: "tristate",
            m.STRING: "string",
            m.INT: "int",
            m.HEX: "hex",
        }.get(orig_type, "unknown")

    # -- predicates --------------------------------------------------------

    def visible(self, node: Any) -> bool:
        """Whether menuconfig would show this node.

        A direct port of the intent of kconfiglib's own ``menuconfig.py``: a menu
        or comment is shown when its dependencies hold, and a symbol or choice
        when it has a visible prompt - or when it has visible children even though
        it does not itself, which is how an invisible parent still surfaces the
        things underneath it.
        """
        m = self._m
        if not node.prompt:
            return False
        if m.expr_value(node.prompt[1]) == 0:
            return False
        if self.is_symbol(node) or self.is_choice(node):
            return node.item.visibility > 0 or self.has_visible_child(node)
        return True

    def has_visible_child(self, node: Any) -> bool:
        child = node.list
        while child:
            if self.visible(child):
                return True
            child = child.next
        return False

    def enterable(self, node: Any) -> bool:
        """Whether the panel should offer to descend into this node.

        Only a menu, or a ``menuconfig`` symbol that has children. Asking merely
        "does it have children?" was wrong twice over, and both showed up the first
        time this met a real Katapult tree:

        **A choice has children - its options.** Treating it as enterable meant the
        architecture choice rendered as a folder to click into rather than as a
        select, and descending showed the raw option symbols as individual
        switches. Every one of those is correctly unsettable on its own (you set
        the choice, not the option), so the screen was three padlocked toggles and
        no way to change anything.

        **A plain symbol's implicit dependency submenu is flattened inline**, at
        depth+1, the way menuconfig shows it. So offering to enter it as well would
        show the same children twice, in two different places.
        """
        if self.is_menu(node):
            return True
        # Checked before is_menuconfig, which kconfiglib also sets on a Choice
        # because it renders menu-like. That is what let choices be entered.
        if self.is_choice(node):
            return False
        return bool(getattr(node, "is_menuconfig", False)) and bool(node.list)

    # -- values ------------------------------------------------------------

    def value(self, node: Any) -> Optional[str]:
        if self.is_menu(node) or self.is_comment(node):
            return None
        item = node.item
        if self.is_choice(node):
            selected = item.selection
            return selected.name if selected is not None else None
        return item.str_value

    def assignable(self, node: Any) -> list[str]:
        """What this node can be set to *right now*.

        Taken from kconfiglib rather than inferred from the type, because that is
        the only thing that knows a symbol is currently held by a ``select`` and so
        cannot be changed at all. An empty list is the difference between "off" and
        "not yours to set".
        """
        if not (self.is_symbol(node) or self.is_choice(node)):
            return []
        item = node.item
        kind = self.kind(node)
        if kind == "choice":
            # A choice's own `assignable` is about whether the choice is *enabled*
            # - it reads ('y',) for any ordinary mandatory choice. What a caller
            # actually needs is which option can be picked, and those are the
            # children. Reporting the tri-values here made every choice look
            # unchangeable, because there was only ever one of them.
            return self.choice_options(node)
        if kind in ("bool", "tristate"):
            return [self._m.TRI_TO_STR[v] for v in sorted(getattr(item, "assignable", ()))]
        # A string, int or hex is editable whenever it is visible.
        return ["<value>"] if getattr(item, "visibility", 0) > 0 else []

    def choice_options(self, node: Any) -> list[str]:
        """The selectable option names of a choice, in declaration order.

        Only the visible ones: an option whose own dependencies are unmet is not a
        thing the user can pick, and offering it would produce a radio button that
        refuses to take.
        """
        out = []
        child = node.list
        while child:
            if (
                self.is_symbol(child)
                and getattr(child.item, "name", None)
                and child.item.visibility > 0
            ):
                out.append(child.item.name)
            child = child.next
        return out

    def editable(self, node: Any) -> bool:
        """Whether changing this would actually do anything.

        Not the same as a non-empty `assignable`. kconfiglib reports a symbol held
        on by a ``select`` as assignable to ``['y']`` - the forced value and nothing
        else - rather than to nothing at all. So "there is exactly one option and it
        is already the value" is the real "you cannot change this", and a control
        gated on `assignable` alone would render as an enabled switch that silently
        refuses to move.
        """
        options = self.assignable(node)
        if not options:
            return False
        if self.kind(node) in ("bool", "tristate", "choice"):
            return len(options) > 1
        return True

    def value_range(self, node: Any) -> Optional[dict[str, str]]:
        """The active range for an int or hex, with its bounds resolved.

        Ranges can be conditional and their bounds can themselves be symbols, so
        this reports what applies now rather than what is written in the file.
        """
        if not self.is_symbol(node):
            return None
        if self.kind(node) not in ("int", "hex"):
            return None
        m = self._m
        for low, high, cond in getattr(node.item, "ranges", ()):
            if m.expr_value(cond):
                return {
                    "min": low.str_value if hasattr(low, "str_value") else str(low),
                    "max": high.str_value if hasattr(high, "str_value") else str(high),
                }
        return None

    # -- payload -----------------------------------------------------------

    def node_id(self, node: Any) -> str:
        """A stable handle the panel can send back.

        Symbols and choices are named, so their name is the handle. A menu has no
        name, so it is identified by its prompt and the file it came from - stable
        across a reparse, which is what matters, since the panel round-trips these.
        """
        if self.is_symbol(node) or self.is_choice(node):
            name = getattr(node.item, "name", None)
            if name:
                return name
        prompt = node.prompt[0] if node.prompt else ""
        return f"@{os.path.basename(str(node.filename))}:{node.linenr}:{prompt}"

    def node(self, node: Any, depth: int = 0) -> dict[str, Any]:
        """One row. Help is deliberately excluded - see :func:`help_for`."""
        kind = self.kind(node)
        return {
            "id": self.node_id(node),
            "kind": kind,
            "name": getattr(node.item, "name", None) if kind != "menu" else None,
            "prompt": node.prompt[0] if node.prompt else "",
            "depth": depth,
            "value": self.value(node),
            "visible": self.visible(node),
            "assignable": self.assignable(node),
            "editable": self.editable(node),
            "range": self.value_range(node),
            "has_help": bool(getattr(node, "help", None)),
            "is_menuconfig": bool(getattr(node, "is_menuconfig", False)),
            "enterable": self.enterable(node),
        }

    def menu(self, node: Any) -> list[dict[str, Any]]:
        """A menu's contents as a flat, indented list.

        Flat with a `depth` per row rather than a nested tree, for two reasons: it
        is the shape ncurses menuconfig already shows, so it is what users
        recognise; and it keeps the Vue side a v-for rather than a recursive
        component.

        Implicit dependency submenus are flattened into the parent at depth+1,
        matching what menuconfig does - a symbol that only appears because another
        is enabled reads as indented under it, not as a separate screen.
        """
        rows: list[dict[str, Any]] = []
        self._collect(node, 0, rows)
        return rows

    def _collect(self, node: Any, depth: int, rows: list[dict[str, Any]]) -> None:
        while node:
            if self.visible(node):
                rows.append(self.node(node, depth))
                # Descend only into implicit submenus. A real menu or a
                # menuconfig is its own screen, reached by `enterable`.
                # Implicit dependency submenus only. A menu and a menuconfig
                # are their own screens; a choice is represented by its
                # options, not by rows of them.
                if (
                    node.list
                    and not self.is_menu(node)
                    and not node.is_menuconfig
                    and not self.is_choice(node)
                ):
                    self._collect(node.list, depth + 1, rows)
            node = node.next


def help_for(node: Any) -> str:
    """Help text, fetched per symbol rather than shipped with the tree.

    Klipper's full help runs to several hundred KB against 40-80 KB for the tree
    without it, and almost none of it is ever read.
    """
    return (getattr(node, "help", None) or "").strip()


# --------------------------------------------------------------------------
# editing
# --------------------------------------------------------------------------

#: kconfiglib's own return value is not enough to tell whether an assignment took.
#: `set_value("99")` on an `int` with `range 4 32` returns **True** and silently
#: leaves the symbol at its default. So every write is verified by reading the
#: value back, which also catches anything else the library declines quietly.
def _same_value(kind: str, requested: str, actual: str) -> bool:
    if kind == "hex":
        try:
            return int(requested, 16) == int(actual, 16)
        except ValueError:
            return requested.strip().lower() == actual.strip().lower()
    if kind == "int":
        try:
            return int(requested) == int(actual)
        except ValueError:
            return requested.strip() == actual.strip()
    return requested.strip() == actual.strip()


class KconfigSession:
    """One open configuration, navigated a menu at a time.

    Holds a parsed tree plus where the user currently is in it. Sessions exist
    because a parse costs a noticeable fraction of a second on a Pi and the panel
    makes many small calls against the same tree.

    Keyed by an opaque id rather than by (type, fw) so two browser tabs cannot end
    up sharing one Kconfig object and overwriting each other's edits.
    """

    def __init__(self, session_id: str, paths: Paths, mcu_type: str, fw: str) -> None:
        self.id = session_id
        self.paths = paths
        self.mcu_type = mcu_type
        self.fw = fw
        self.fw_dir = paths.fw_dir(fw)
        self.config_path = paths.config_file(mcu_type, fw)
        self.dirty = False
        self.created = time.time()
        self.touched = self.created
        #: Bumped on every change, so a client can tell cached menus are stale.
        self.revision = 0

        #: Held around every operation on this session. The agent serves requests
        #: from a worker pool, so two calls on one session could otherwise
        #: interleave halfway through an assignment and its dependency propagation.
        self.lock = threading.Lock()

        self._module = load_kconfiglib(self.fw_dir)
        self.serializer = Serializer(self._module)
        self._kconf = self._parse()
        #: Root-to-here as MenuNodes; the first entry is always the top menu.
        self._path: list[Any] = [self._kconf.top_node]

    # -- lifecycle ---------------------------------------------------------

    def _parse(self) -> Any:
        rel = os.path.join("src", "Kconfig")
        if not os.path.isfile(os.path.join(self.fw_dir, rel)):
            raise KconfigError(
                f"no {rel} in {self.fw_dir}. Is that a firmware source tree?",
                path=self.fw_dir,
            )
        with _srctree(self.fw_dir):
            try:
                kconf = self._module.Kconfig(rel, warn_to_stderr=False)
            except Exception as exc:  # noqa: BLE001 - a parse failure is fatal here
                raise KconfigError(
                    f"could not parse {rel} in {self.fw_dir}: {exc}", path=self.fw_dir
                ) from exc

        # A missing config file is normal, not an error: it means this type has
        # never been configured and the Kconfig defaults are the right place to
        # start from.
        if os.path.isfile(self.config_path):
            try:
                kconf.load_config(self.config_path)
            except Exception as exc:  # noqa: BLE001
                raise KconfigError(
                    f"could not read {self.config_path}: {exc}", path=self.config_path
                ) from exc
        return kconf

    def touch(self) -> None:
        self.touched = time.time()

    @property
    def age(self) -> float:
        return time.time() - self.touched

    # -- navigation --------------------------------------------------------

    @property
    def current(self) -> Any:
        return self._path[-1]

    def breadcrumb(self) -> list[dict[str, str]]:
        out = []
        for node in self._path:
            if node.prompt:
                prompt = node.prompt[0]
            else:
                prompt = self._kconf.mainmenu_text or "Configuration"
            out.append({"id": self.serializer.node_id(node), "prompt": prompt})
        return out

    def menu(self) -> dict[str, Any]:
        """The current screen: where we are, and what is on it."""
        self.touch()
        return {
            "session": self.id,
            "revision": self.revision,
            "type": self.mcu_type,
            "fw": self.fw,
            "dirty": self.dirty,
            "breadcrumb": self.breadcrumb(),
            "nodes": self.serializer.menu(self.current.list),
        }

    def _find(self, node_id: str) -> Any:
        """Locate a node by the id the panel was handed.

        Searched across the whole tree rather than the current menu, because a
        `set` can move or hide the node a client is talking about.
        """
        for node in _walk(self._kconf.top_node.list):
            if self.serializer.node_id(node) == node_id:
                return node
        raise KconfigError(f"no such config entry: {node_id}", node=node_id)

    def enter(self, node_id: str) -> dict[str, Any]:
        node = self._find(node_id)
        if not self.serializer.enterable(node):
            raise KconfigError(f"{node_id} has nothing to enter", node=node_id)
        self._path.append(node)
        return self.menu()

    def up(self) -> dict[str, Any]:
        if len(self._path) > 1:
            self._path.pop()
        return self.menu()

    def _reanchor(self) -> None:
        """Drop any part of the path a change has made invisible.

        Flipping a choice can make the menu the user is standing in vanish. Falling
        back to the nearest ancestor that still exists is what menuconfig does, and
        beats rendering an empty screen with no way out of it.
        """
        while len(self._path) > 1 and not self.serializer.visible(self.current):
            self._path.pop()

    # -- editing -----------------------------------------------------------

    def set_value(self, node_id: str, value: str) -> dict[str, Any]:
        """Assign one symbol, then prove the assignment took.

        Returns the whole current menu plus the names whose value or visibility
        moved. A structural delta was considered and rejected: flipping
        MACH_STM32 to MACH_RP2040 replaces essentially the entire tree, so any
        delta encoding degenerates to "replace everything" in exactly the common
        case, while needing a tree-patcher in TypeScript to consume it.
        """
        node = self._find(node_id)
        kind = self.serializer.kind(node)
        if kind in ("menu", "comment", "unknown"):
            raise KconfigError(f"{node_id} is not something with a value", node=node_id)
        if not self.serializer.editable(node):
            # An option inside a choice is not set directly - you set the choice to
            # the option's name. Saying "held by a select" here would send someone
            # looking for a select that does not exist.
            parent = getattr(node, "parent", None)
            if parent is not None and self.serializer.is_choice(parent):
                raise KconfigError(
                    f"{node_id} is one option of a choice, so it is not set on its "
                    f"own. Set the choice "
                    f"{self.serializer.node_id(parent)!r} to {node_id!r} instead.",
                    node=node_id,
                    choice=self.serializer.node_id(parent),
                )
            raise KconfigError(
                f"{node_id} cannot be changed right now - it is held by another "
                f"symbol's 'select', or its dependencies are not met.",
                node=node_id,
            )

        before = self._snapshot()
        wanted = str(value).strip()

        if kind == "choice":
            self._select_choice(node, wanted)
        else:
            allowed = self.serializer.assignable(node)
            if kind in ("bool", "tristate") and wanted not in allowed:
                raise KconfigError(
                    f"{node_id} accepts {allowed}, not {wanted!r}",
                    node=node_id,
                    allowed=allowed,
                )
            self._assign(node, node.item, kind, wanted)

        self.dirty = True
        self.revision += 1
        self._reanchor()
        payload = self.menu()
        payload["changed"] = self._diff(before)
        return payload

    def _select_choice(self, node: Any, wanted: str) -> None:
        options = {
            child.item.name: child
            for child in _iter_siblings(node.list)
            if self.serializer.is_symbol(child) and getattr(child.item, "name", None)
        }
        target = options.get(wanted)
        if target is None:
            raise KconfigError(
                f"{wanted!r} is not one of this choice's options: {sorted(options)}",
                node=self.serializer.node_id(node),
                allowed=sorted(options),
            )
        self._assign(target, target.item, "bool", "y")

    def _assign(self, node: Any, sym: Any, kind: str, wanted: str) -> None:
        rng = self.serializer.value_range(node)
        if rng is not None:
            self._check_range(node, kind, wanted, rng)

        accepted = sym.set_value(wanted)
        actual = sym.str_value
        # set_value returns False for a value of the wrong *shape*, but
        # True-and-silently-ignored for one that is merely out of range - it leaves
        # the symbol at its default and reports success.
        #
        # The explicit range check above catches that case first, so this read-back
        # is currently belt-and-braces for it: removing the read-back alone leaves
        # every test passing. It stays because it is the only guard that does not
        # need to anticipate *why* a value was rejected, and kconfiglib has already
        # been shown to reject one silently.
        if accepted is False or not _same_value(kind, wanted, actual):
            detail = f" (it is still {actual!r})" if accepted is not False else ""
            suffix = f" Allowed range: {rng['min']}..{rng['max']}." if rng else ""
            raise KconfigError(
                f"{self.serializer.node_id(node)} would not accept {wanted!r}"
                f"{detail}.{suffix}",
                node=self.serializer.node_id(node),
                requested=wanted,
                actual=actual,
            )

    def _check_range(self, node: Any, kind: str, wanted: str, rng: dict[str, str]) -> None:
        base = 16 if kind == "hex" else 10
        try:
            value = int(wanted, base)
            low = int(rng["min"], base)
            high = int(rng["max"], base)
        except ValueError:
            raise KconfigError(
                f"{wanted!r} is not a valid {kind} value",
                node=self.serializer.node_id(node),
                requested=wanted,
            ) from None
        if not low <= value <= high:
            raise KconfigError(
                f"{wanted} is outside the allowed range {rng['min']}..{rng['max']}",
                node=self.serializer.node_id(node),
                requested=wanted,
                allowed_range=rng,
            )

    def _snapshot(self) -> dict[str, tuple[str, bool]]:
        """Value and visibility of every defined symbol, for diffing one change."""
        out: dict[str, tuple[str, bool]] = {}
        for sym in self._kconf.unique_defined_syms:
            if sym.name:
                out[sym.name] = (sym.str_value, sym.visibility > 0)
        return out

    def _diff(self, before: dict[str, tuple[str, bool]]) -> list[str]:
        after = self._snapshot()
        return sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))

    # -- reading -----------------------------------------------------------

    def help(self, node_id: str) -> dict[str, Any]:
        node = self._find(node_id)
        self.touch()
        return {
            "id": node_id,
            "prompt": node.prompt[0] if node.prompt else "",
            "help": help_for(node),
        }

    def search(self, query: str, limit: int = 60) -> dict[str, Any]:
        """Visible symbols whose name or prompt contains `query`.

        Rows come back in the same shape as a menu, all at depth 0, so the panel
        renders results with the component it already has.
        """
        self.touch()
        needle = query.strip().lower()
        if not needle:
            return {"query": query, "nodes": [], "truncated": False}
        rows = []
        for node in _walk(self._kconf.top_node.list):
            if not self.serializer.visible(node):
                continue
            name = (getattr(node.item, "name", None) or "").lower()
            prompt = (node.prompt[0] if node.prompt else "").lower()
            if needle in name or needle in prompt:
                rows.append(self.serializer.node(node, 0))
                if len(rows) >= limit:
                    break
        return {"query": query, "nodes": rows, "truncated": len(rows) >= limit}

    # -- persistence -------------------------------------------------------

    def save(self) -> dict[str, Any]:
        """Write the answers out, without ever leaving a truncated file behind.

        kconfiglib's own ``write_config`` writes in place and non-atomically, so a
        crash part-way through leaves answers that cannot be recovered - and the
        saved answers are the one thing in this tool that genuinely cannot be
        regenerated. So: write to a temp file, keep one generation of backup, then
        rename into place.
        """
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        tmp = self.config_path + ".tmp"
        try:
            with _srctree(self.fw_dir):
                self._kconf.write_config(tmp)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise KconfigError(
                f"could not write {self.config_path}: {exc}", path=self.config_path
            ) from exc

        backup = None
        if os.path.isfile(self.config_path):
            backup = self.config_path + ".bak"
            try:
                # replace, not copy: one generation, and no window where neither
                # the old nor the new file is complete.
                os.replace(self.config_path, backup)
            except OSError:
                backup = None
        os.replace(tmp, self.config_path)

        self.dirty = False
        self.touch()
        return {"path": self.config_path, "backup": backup, "dirty": False}

    def reset(self) -> dict[str, Any]:
        """Throw away unsaved edits by reparsing from disk."""
        self._kconf = self._parse()
        self._path = [self._kconf.top_node]
        self.dirty = False
        self.revision += 1
        return self.menu()


def _iter_siblings(node: Any) -> Iterator[Any]:
    while node:
        yield node
        node = node.next


def _walk(node: Any) -> Iterator[Any]:
    while node:
        yield node
        if node.list:
            yield from _walk(node.list)
        node = node.next


class SessionStore:
    """The open configurations, bounded in both count and age.

    Each parsed Kconfig holds a few MB, and a browser tab that navigates away
    never tells anyone, so sessions have to expire on their own rather than be
    closed politely.
    """

    #: Long enough to read the help text and think; short enough that a forgotten
    #: tab does not pin memory until the agent restarts.
    TTL = 30 * 60

    #: Each tree costs a few MB parsed. Four is more tabs than anyone configures
    #: at once, and the oldest idle one is evicted rather than refusing a new one.
    MAX = 4

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._sessions: dict[str, KconfigSession] = {}
        self._next = 1
        self._lock = threading.Lock()

    def _reap(self) -> None:
        for sid in [s for s, sess in self._sessions.items() if sess.age > self.TTL]:
            self._sessions.pop(sid, None)

    def open(self, mcu_type: str, fw: str) -> KconfigSession:
        with self._lock:
            self._reap()
            if len(self._sessions) >= self.MAX:
                # Evict the least recently used *clean* session first; only fall
                # back to discarding unsaved work when everything is dirty.
                candidates = sorted(self._sessions.values(), key=lambda s: (s.dirty, s.touched))
                self._sessions.pop(candidates[0].id, None)
            sid = f"kc-{self._next}"
            self._next += 1
            session = KconfigSession(sid, self.paths, mcu_type, fw)
            self._sessions[sid] = session
            return session

    def get(self, session_id: str) -> KconfigSession:
        with self._lock:
            self._reap()
            session = self._sessions.get(session_id)
        if session is None:
            raise KconfigError(
                f"config session {session_id} is not open any more. It may have "
                f"expired after {self.TTL // 60} minutes idle - reopen it and try "
                f"again.",
                session=session_id,
            )
        return session

    def close(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def dirty_for(self, mcu_type: str, fw: str) -> Optional[KconfigSession]:
        """An existing session with unsaved edits for the same target, if any.

        Opening a second session on a target someone is already editing would let
        one save silently discard the other's work, so callers check first.
        """
        with self._lock:
            for session in self._sessions.values():
                if session.mcu_type == mcu_type and session.fw == fw and session.dirty:
                    return session
        return None
