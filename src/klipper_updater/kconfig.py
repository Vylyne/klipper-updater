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
from collections.abc import Iterator
from types import ModuleType
from typing import Any, Optional

from .errors import KconfigError

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

        A menu always is. A ``menuconfig`` symbol is when it has children. A plain
        symbol with an implicit dependency submenu is too - which is why this asks
        about children rather than about `is_menuconfig`.
        """
        if self.is_menu(node):
            return True
        return bool(node.list) and not self.is_comment(node)

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
        if kind in ("bool", "tristate", "choice"):
            return [self._m.TRI_TO_STR[v] for v in sorted(getattr(item, "assignable", ()))]
        # A string, int or hex is editable whenever it is visible.
        return ["<value>"] if getattr(item, "visibility", 0) > 0 else []

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
                if node.list and not self.is_menu(node) and not node.is_menuconfig:
                    self._collect(node.list, depth + 1, rows)
            node = node.next


def help_for(node: Any) -> str:
    """Help text, fetched per symbol rather than shipped with the tree.

    Klipper's full help runs to several hundred KB against 40-80 KB for the tree
    without it, and almost none of it is ever read.
    """
    return (getattr(node, "help", None) or "").strip()
