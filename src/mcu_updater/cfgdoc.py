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

#: A comment may follow the header. Klipper's own parser allows it, so a config
#: sitting next to printer.cfg has to as well - and without this the line simply
#: did not match, which is silent: the section was never registered, every option
#: under it was attributed to the section above, and the type or display it
#: declared just did not exist. `[display knomi_toolchanger]  # env name` is how
#: the README suggests writing it.
_SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*(?:[#;].*)?$")
_OPTION_RE = re.compile(r"^(?P<key>[^\s:=#;][^:=]*?)\s*[:=](?P<value>.*)$")
_COMMENT_RE = re.compile(r"^\s*[#;]")

INDENT = "    "

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_bool(raw: Optional[str], default: Optional[bool] = False) -> Optional[bool]:
    """Klipper-style truthiness. Returns None when the value is unrecognised, so
    a caller can tell "not set" from "set to nonsense".

    The registry passes a real default and treats an unrecognised value as that
    default; settings pass ``default=None`` so both "absent" and "nonsense" come
    back as None and the nonsense can be raised on."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


#: An inline comment, matching Klipper's own configparser
#: (`inline_comment_prefixes=(';', '#')`): a `#` or `;` that either starts the
#: text or follows whitespace. Requiring the whitespace is what lets a value
#: containing a bare `#` mid-token survive - a makefile patch line, say.
_INLINE_COMMENT_RE = re.compile(r"(?:(?<=\s)|^)[#;].*$")


def _strip_inline_comment(text: str) -> str:
    """`290055...-if00  # EBBT1` -> `290055...-if00`.

    Without this the comment became part of the serial, so the board matched
    nothing on the bus and read as permanently offline.
    """
    return _INLINE_COMMENT_RE.sub("", text).rstrip()


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
        #: Names appearing more than once. First wins, so the later copy is dead
        #: text - which is silent and confusing enough that callers refuse on it.
        self.duplicate_sections: list[str] = []
        self._parse()

    # -- parsing -----------------------------------------------------------

    def _parse(self) -> None:
        self.sections = {}
        self.duplicate_sections = []
        current: Optional[Section] = None
        current_option: Optional[Option] = None

        for index, line in enumerate(self.lines):
            match = _SECTION_RE.match(line)
            if match:
                current = Section(match.group("name").strip(), index)
                # A duplicate section name keeps the first; last-wins would make
                # a hand-edit silently shadow an earlier board. Record it either
                # way so a loader can refuse rather than quietly drop half the
                # file - appending a second [updater] block instead of editing
                # the existing one is an easy and otherwise invisible mistake.
                if current.name in self.sections:
                    if current.name not in self.duplicate_sections:
                        self.duplicate_sections.append(current.name)
                else:
                    self.sections[current.name] = current
                current_option = None
                continue

            if current is None:
                continue  # preamble comments before any section

            current.end = index + 1

            if _is_comment(line):
                # An INDENTED comment sits inside a multi-line value's block, so
                # it must not end the option. Ending it here meant every item
                # below a `# label` line was silently dropped - the serials after
                # it simply vanished from the registry, and the type came back
                # with "no boards tracked".
                if current_option is not None and _is_continuation(line):
                    current_option.end = index + 1
                    continue
                current_option = None
                continue

            if _is_blank(line):
                current_option = None
                continue

            if current_option is not None and _is_continuation(line):
                current_option.end = index + 1
                item = _strip_inline_comment(line.strip())
                if item:
                    current_option.value += "\n" + item
                continue

            opt_match = _OPTION_RE.match(line)
            if opt_match:
                key = opt_match.group("key").strip()
                value = _strip_inline_comment(opt_match.group("value").strip())
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
    def _decoration(existing: Optional[list[str]]) -> tuple[dict, dict, list]:
        """The comments a multi-line block already carries.

        Rewriting an option splices the whole block, so without this, adopting one
        board would erase the `# EBBT0` labels the user had put beside every other
        one. Which is worse than it sounds: those labels are how you know which
        physical toolhead a serial belongs to.

        Returns (inline per item, standalone comments preceding each item, and any
        left trailing at the end of the block).
        """
        inline: dict[str, str] = {}
        before: dict[str, list[str]] = {}
        pending: list[str] = []
        if not existing:
            return inline, before, pending

        # existing[0] is the `key:` line itself.
        for raw in existing[1:]:
            text = raw.strip()
            if not text:
                continue
            if _COMMENT_RE.match(raw):
                pending.append(text)
                continue
            item = _strip_inline_comment(text)
            if not item:
                continue
            comment = text[len(item) :].strip()
            if comment:
                inline[item] = comment
            if pending:
                before[item] = pending
                pending = []
        return inline, before, pending

    @classmethod
    def _render(cls, key: str, value: object, existing: Optional[list[str]] = None) -> list[str]:
        inline, before, trailing = cls._decoration(existing)

        def block(items: list[str]) -> list[str]:
            if not items:
                return [f"{key}:"]
            out = [f"{key}:"]
            for item in items:
                out.extend(f"{INDENT}{c}" for c in before.get(item, []))
                comment = inline.get(item)
                out.append(f"{INDENT}{item}" + (f"  {comment}" if comment else ""))
            # Comments that followed the last item, or the whole block if every
            # item is gone. Dropping them would lose a note about a board that
            # was just removed, which is exactly when it is worth keeping.
            out.extend(f"{INDENT}{c}" for c in trailing)
            return out

        if isinstance(value, (list, tuple)):
            return block([str(v) for v in value if str(v).strip()])
        text = str(value)
        if "\n" in text:
            return block([p.strip() for p in text.splitlines() if p.strip()])

        # Single-line value: keep a trailing comment it already had.
        comment = ""
        if existing and len(existing) == 1:
            body = existing[0].split(":", 1)[-1].split("=", 1)[-1]
            stripped = _strip_inline_comment(body)
            comment = body[len(stripped) :].strip()
        return [f"{key}: {text}" + (f"  {comment}" if comment else "")]

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

        opt = sec.options.get(key)
        if opt is not None:
            # Hand it the lines being replaced, so any comments in them survive.
            rendered = self._render(key, value, self.lines[opt.start : opt.end])
            self._splice(opt.start, opt.end, rendered)
            return

        rendered = self._render(key, value)

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
