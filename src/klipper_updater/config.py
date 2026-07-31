"""The MCU registry: ``~/mcus/mcus.json``.

Schema, as it actually exists on a live printer::

    {
      "flylllplusbuffer": {
        "chipset": "stm32f072xb",
        "katapult": {"installed": true, "extra_args": ""},
        "klipper":  {"makefile_patches": [{"file": "src/Makefile",
                                          "line": "src-y += buffer.c"}],
                     "extra_args": ""},
        "serials": ["4C0033000957465331323720-if00", ...]
      }
    }

Three per-firmware keys, and that is all: ``extra_args`` (appended to the make
command line), ``installed`` (katapult only), and ``makefile_patches``.

An older ``extra_src`` key predates ``makefile_patches`` and meant something
different - it was appended to ``src/Makefile``, because klipper's build system
has no way to add ``src-y +=`` lines from the command line. It is handled here
for anyone still carrying one, but note it is NOT a synonym for ``extra_args``:
feeding ``src-$(CONFIG_MACH_STM32F072) += buffer.c`` to make produces three
bogus goals and a failed build.

Round-trip fidelity is a hard requirement. This file is hand-edited, so
unrecognised keys and key ordering are preserved: every ``to_json`` starts from
the dict it was parsed from and only overwrites what it owns.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Iterable
from typing import Any, Optional

from .errors import (
    AmbiguousSerialError,
    ConfigCorruptError,
    DuplicateTypeError,
    SerialTrackedElsewhereError,
    UnknownSerialError,
    UnknownTypeError,
)
from .paths import FW_TARGETS, Paths

#: A legacy extra_src value shaped like a Makefile source line, e.g.
#: "src-y += buffer.c" or "src-$(CONFIG_MACH_STM32F072) += foo.c".
_MAKEFILE_SRC_LINE = re.compile(r"^\s*src-\S*\s*\+=")


@dataclasses.dataclass
class MakefilePatch:
    #: Relative to the firmware source tree, e.g. "src/stm32/Makefile".
    file: str
    line: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MakefilePatch:
        return cls(file=str(d.get("file", "")), line=str(d.get("line", "")))

    def to_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line}

    def is_valid(self) -> bool:
        return bool(self.file and self.line)


@dataclasses.dataclass
class FwConfig:
    extra_args: str = ""
    #: None means the key was absent. Only katapult blocks carry it.
    installed: Optional[bool] = None
    makefile_patches: list[MakefilePatch] = dataclasses.field(default_factory=list)
    #: Everything we didn't recognise, kept so a save doesn't destroy it.
    _raw: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Any) -> FwConfig:
        if not isinstance(d, dict):
            return cls()
        patches = [
            MakefilePatch.from_json(p)
            for p in d.get("makefile_patches") or []
            if isinstance(p, dict)
        ]
        extra_args = str(d.get("extra_args", "") or "")

        # Legacy extra_src: a Makefile source line becomes a patch; anything
        # else was almost certainly meant as make arguments.
        legacy = str(d.get("extra_src", "") or "").strip()
        if legacy:
            if _MAKEFILE_SRC_LINE.match(legacy):
                patches.append(MakefilePatch(file="src/Makefile", line=legacy))
            elif not extra_args:
                extra_args = legacy

        installed = d.get("installed")
        return cls(
            extra_args=extra_args,
            installed=bool(installed) if installed is not None else None,
            makefile_patches=patches,
            _raw=dict(d),
        )

    def to_json(self) -> dict[str, Any]:
        out = dict(self._raw)
        out.pop("extra_src", None)  # migrated into extra_args / makefile_patches
        out["extra_args"] = self.extra_args
        if self.installed is None:
            out.pop("installed", None)
        else:
            out["installed"] = self.installed
        valid = [p for p in self.makefile_patches if p.is_valid()]
        if valid:
            out["makefile_patches"] = [p.to_json() for p in valid]
        else:
            out.pop("makefile_patches", None)
        return out


@dataclasses.dataclass
class McuType:
    name: str
    chipset: str = ""
    serials: list[str] = dataclasses.field(default_factory=list)
    fws: dict[str, FwConfig] = dataclasses.field(default_factory=dict)
    _raw: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, name: str, d: Any) -> McuType:
        if not isinstance(d, dict):
            d = {}
        serials = [str(s) for s in (d.get("serials") or []) if s]
        fws = {fw: FwConfig.from_json(d.get(fw)) for fw in FW_TARGETS}
        return cls(
            name=name,
            chipset=str(d.get("chipset", "") or ""),
            serials=serials,
            fws=fws,
            _raw=dict(d),
        )

    def fw(self, fw: str) -> FwConfig:
        return self.fws.setdefault(fw, FwConfig())

    @property
    def katapult_installed(self) -> bool:
        """Absent `installed` is treated as True - a board with no bootloader is the exception."""
        val = self.fw("katapult").installed
        return True if val is None else val

    def to_json(self) -> dict[str, Any]:
        out = dict(self._raw)
        out["chipset"] = self.chipset
        for fw in FW_TARGETS:
            cfg = self.fws.get(fw)
            if cfg is None:
                continue
            # Only emit a firmware block if it was already there or has content.
            body = cfg.to_json()
            if fw in self._raw or body.get("extra_args") or body.get("makefile_patches"):
                out[fw] = body
        out["serials"] = list(self.serials)
        return out


class Registry:
    """In-memory view of mcus.json with faithful save semantics."""

    def __init__(self, types: dict[str, McuType], raw: dict[str, Any]) -> None:
        self.types = types
        self._raw = raw

    # --- construction / persistence ---

    @classmethod
    def load(cls, paths: Paths) -> Registry:
        path = paths.mcus_json
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return cls({}, {})
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigCorruptError(
                f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
                f"Fix or restore it - refusing to continue, because treating this as "
                f"'no MCU types configured' risks overwriting the whole registry.",
                path=path,
                line=exc.lineno,
                column=exc.colno,
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigCorruptError(
                f"{path} must contain a JSON object mapping type names to configs, "
                f"got {type(raw).__name__}",
                path=path,
            )
        types = {name: McuType.from_json(name, body) for name, body in raw.items()}
        return cls(types, raw)

    def save(self, paths: Paths) -> None:
        """Atomic write, matching the original's tmp+replace and indent=4."""
        out = dict(self._raw)
        for name in list(out):
            if name not in self.types:
                del out[name]
        for name, mcu in self.types.items():
            out[name] = mcu.to_json()

        os.makedirs(os.path.dirname(paths.mcus_json), exist_ok=True)
        tmp = paths.mcus_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=4)
        os.replace(tmp, paths.mcus_json)
        self._raw = out

    # --- lookups ---

    def __contains__(self, name: object) -> bool:
        return name in self.types

    def __len__(self) -> int:
        return len(self.types)

    def __bool__(self) -> bool:
        return bool(self.types)

    def names(self) -> list[str]:
        return sorted(self.types)

    def get(self, name: str) -> McuType:
        try:
            return self.types[name]
        except KeyError:
            raise UnknownTypeError(
                f"MCU type '{name}' does not exist.", type=name, known=self.names()
            ) from None

    def all_serials(self) -> set[str]:
        out: set[str] = set()
        for mcu in self.types.values():
            out.update(mcu.serials)
        return out

    def find_types_for_serial(self, serial: str) -> list[str]:
        """Types tracking this serial. Normally 0 or 1; >1 is a misconfiguration."""
        return [name for name, mcu in self.types.items() if serial in mcu.serials]

    def resolve_serial(self, serial: str, mcu_type: Optional[str] = None) -> str:
        """Work out which type a serial belongs to.

        With an explicit `mcu_type`, verifies the pairing. Raises
        SerialTrackedElsewhereError if the serial belongs to a *different* type
        - that is a much stronger signal of "wrong -t" than "this is a new
        device", so it is refused outright rather than offered as an add.
        Raises UnknownSerialError if it is simply untracked; the caller decides
        whether to offer adding it.
        """
        if mcu_type is not None:
            mcu = self.get(mcu_type)
            if serial in mcu.serials:
                return mcu_type
            elsewhere = self.find_types_for_serial(serial)
            if elsewhere:
                raise SerialTrackedElsewhereError(
                    f"serial '{serial}' is already tracked under '{elsewhere[0]}', "
                    f"not '{mcu_type}'. Did you mean -t {elsewhere[0]}?",
                    serial=serial,
                    requested=mcu_type,
                    tracked_under=elsewhere,
                )
            raise UnknownSerialError(
                f"serial '{serial}' isn't tracked under '{mcu_type}' yet.",
                serial=serial,
                requested=mcu_type,
            )

        matches = self.find_types_for_serial(serial)
        if not matches:
            raise UnknownSerialError(
                f"serial '{serial}' isn't tracked under any MCU type.", serial=serial
            )
        if len(matches) > 1:
            raise AmbiguousSerialError(
                f"serial '{serial}' is tracked under multiple types "
                f"({', '.join(sorted(matches))}) - pass -t to disambiguate.",
                serial=serial,
                tracked_under=sorted(matches),
            )
        return matches[0]

    # --- mutation ---

    def add_type(
        self,
        name: str,
        chipset: str,
        *,
        klipper_args: str = "",
        katapult_args: str = "",
        katapult_installed: bool = True,
        overwrite: bool = False,
    ) -> McuType:
        if name in self.types and not overwrite:
            raise DuplicateTypeError(
                f"MCU type '{name}' already exists.", type=name
            )
        mcu = McuType(
            name=name,
            chipset=chipset,
            serials=[],
            fws={
                "katapult": FwConfig(extra_args=katapult_args, installed=katapult_installed),
                "klipper": FwConfig(extra_args=klipper_args),
            },
        )
        self.types[name] = mcu
        return mcu

    def remove_type(self, name: str) -> McuType:
        mcu = self.get(name)
        del self.types[name]
        return mcu

    def add_serial(self, name: str, serial: str) -> bool:
        """Returns True if it was added, False if already present."""
        mcu = self.get(name)
        if serial in mcu.serials:
            return False
        mcu.serials.append(serial)
        return True

    def remove_serial(self, name: str, serial: str) -> bool:
        """Returns True if it was removed, False if it wasn't tracked."""
        mcu = self.get(name)
        if serial not in mcu.serials:
            return False
        mcu.serials.remove(serial)
        return True

    def items(self) -> Iterable[tuple[str, McuType]]:
        return self.types.items()
