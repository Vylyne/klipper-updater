"""A Klipper-style ``.cfg`` document that survives being written back.

``configparser`` reads this format fine, but writing with it throws away every
comment, blank line and bit of key ordering in the file. That is unacceptable
here: the registry lives in ``printer_data/config`` where people hand-edit it and
annotate it, and the panel edits the same file structurally. A user's note about
why a board needs a particular Makefile patch must not vanish because they added
a serial from their phone.

So this keeps the file as *lines*, remembers where each section and option lives,
and splices edits into place. Anything it doesn't recognise - comments, blank
lines, keys from a future version - is carried through untouched.

Format supported (a deliberate subset of what Klipper/Moonraker use)::

    # a comment
    [section name]
    key: value
    other = value            ; '=' works too
    multi:
        first
        second

Continuation lines are indented and non-blank, matching Klipper's own configs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Optional

_SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
_OPTION_RE = re.compile(r"^(?P<key>[^\s:=#;][^:=]*?)\s*[:=](?P<value>.*)$")
_COMMENT_RE = re.compile(r"^\s*[#;]")

INDENT = "    "


def _is_comment(line: str) -> bool:
    return bool(_COMMENT_RE.match(line))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_continuation(line: str) -> bool:
    """Indented and non-blank: part of the option above."""
    return bool(line) and line[0] in " \t" and not _is_blank(line)


class Option:
    """One key and the span of lines it occupies."""

    __slots__ = ("key", "start", "end", "value")

    def __init__(self, key: str, start: int, end: int, value: str) -> None:
        self.key = key
        self.start = start  # index of the "key:" line
        self.end = end  # exclusive
        self.value = value


class Section:
    __slots__ = ("name", "header", "end", "options")

    def __init__(self, name: str, header: int) -> None:
        self.name = name
        self.header = header  # index of the "[name]" line
        self.end = header + 1  # exclusive; grows as the section is parsed
        self.options: dict[str, Option] = {}


class CfgDocument:
    """Parsed .cfg with faithful write-back."""

    def __init__(self, text: str = "") -> None:
        self.lines: list[str] = text.splitlines() if text else []
        self.sections: dict[str, Section] = {}
        self._parse()

    # -- parsing -----------------------------------------------------------

    def _parse(self) -> None:
        self.sections = {}
        current: Optional[Section] = None
        current_option: Optional[Option] = None

        for index, line in enumerate(self.lines):
            match = _SECTION_RE.match(line)
            if match:
                current = Section(match.group("name").strip(), index)
                # A duplicate section name keeps the first; last-wins would make
                # a hand-edit silently shadow an earlier board.
                self.sections.setdefault(current.name, current)
                current_option = None
                continue

            if current is None:
                continue  # preamble comments before any section

            current.end = index + 1

            if _is_comment(line) or _is_blank(line):
                current_option = None
                continue

            if current_option is not None and _is_continuation(line):
                current_option.end = index + 1
                current_option.value += "\n" + line.strip()
                continue

            opt_match = _OPTION_RE.match(line)
            if opt_match:
                key = opt_match.group("key").strip()
                value = opt_match.group("value").strip()
                current_option = Option(key, index, index + 1, value)
                current.options.setdefault(key, current_option)
                continue

            current_option = None

    # -- reading -----------------------------------------------------------

    def has_section(self, name: str) -> bool:
        return name in self.sections

    def section_names(self, prefix: Optional[str] = None) -> list[str]:
        """Section names in file order, optionally only those starting with a word."""
        names = sorted(self.sections, key=lambda n: self.sections[n].header)
        if prefix is None:
            return names
        return [n for n in names if n == prefix or n.startswith(prefix + " ")]

    def get(self, section: str, key: str, default: Optional[str] = None) -> Optional[str]:
        sec = self.sections.get(section)
        if sec is None:
            return default
        opt = sec.options.get(key)
        return default if opt is None else opt.value

    def get_list(self, section: str, key: str) -> list[str]:
        """A multi-line value as a list, blank entries dropped."""
        raw = self.get(section, key)
        if not raw:
            return []
        return [part.strip() for part in raw.splitlines() if part.strip()]

    def options(self, section: str) -> list[str]:
        sec = self.sections.get(section)
        return [] if sec is None else list(sec.options)

    # -- writing -----------------------------------------------------------

    @staticmethod
    def _render(key: str, value: object) -> list[str]:
        if isinstance(value, (list, tuple)):
            items = [str(v) for v in value if str(v).strip()]
            if not items:
                return [f"{key}:"]
            return [f"{key}:"] + [f"{INDENT}{item}" for item in items]
        text = str(value)
        if "\n" in text:
            parts = [p.strip() for p in text.splitlines() if p.strip()]
            return [f"{key}:"] + [f"{INDENT}{p}" for p in parts]
        return [f"{key}: {text}"]

    def _splice(self, start: int, end: int, replacement: list[str]) -> None:
        self.lines[start:end] = replacement
        # Line numbers everywhere else are now wrong, so rebuild. The files are
        # tens of lines; correctness beats cleverness here.
        self._parse()

    def set(self, section: str, key: str, value: object) -> None:
        sec = self.sections.get(section)
        if sec is None:
            self.add_section(section)
            sec = self.sections[section]

        rendered = self._render(key, value)
        opt = sec.options.get(key)
        if opt is not None:
            self._splice(opt.start, opt.end, rendered)
            return

        # New key: append after the section's last non-blank line, so it lands
        # inside the section rather than after the blank line separating it from
        # the next one.
        insert_at = sec.end
        while insert_at > sec.header + 1 and _is_blank(self.lines[insert_at - 1]):
            insert_at -= 1
        self._splice(insert_at, insert_at, rendered)

    def remove_option(self, section: str, key: str) -> bool:
        sec = self.sections.get(section)
        if sec is None:
            return False
        opt = sec.options.get(key)
        if opt is None:
            return False
        self._splice(opt.start, opt.end, [])
        return True

    def add_section(self, name: str) -> None:
        if name in self.sections:
            return
        block = []
        if self.lines and not _is_blank(self.lines[-1]):
            block.append("")
        block.append(f"[{name}]")
        self._splice(len(self.lines), len(self.lines), block)

    def remove_section(self, name: str) -> bool:
        sec = self.sections.get(name)
        if sec is None:
            return False
        end = sec.end
        # Take one trailing blank line with it, so removing a section doesn't
        # leave a growing gap behind.
        if end < len(self.lines) and _is_blank(self.lines[end]):
            end += 1
        self._splice(sec.header, end, [])
        return True

    # -- output ------------------------------------------------------------

    def render(self) -> str:
        text = "\n".join(self.lines)
        return text if text.endswith("\n") or not text else text + "\n"

    def __iter__(self) -> Iterator[str]:
        return iter(self.section_names())

    def __contains__(self, name: object) -> bool:
        return name in self.sections
