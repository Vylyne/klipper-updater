"""The MCU registry: ``~/printer_data/config/mcu-updater/mcu-updater.cfg``.

Klipper-style, because it lives next to ``printer.cfg`` and gets hand-edited::

    # Toolhead boards. The buffer patch is specific to this batch.
    [mcu flylllplusbuffer]
    chipset: stm32f072xb
    serials:
        4C0033000957465331323720-if00
        3F0037000957465331323720-if00
    klipper_makefile_patches:
        src/Makefile -> src-y += buffer.c

Per-type keys, and that is all:

``chipset``
    Required. Matches the chipset segment of the /dev/serial/by-id name.
``serials``
    One tracked board per line.
``katapult_installed``
    Only written when false; a board with no bootloader is the exception.
``<fw>_extra_args``
    Appended to the make command line.
``<fw>_makefile_patches``
    ``<file> -> <line>`` per line. Appended to that Makefile for the duration of
    one build, then reverted. This exists because Klipper's build system has no
    way to add ``src-y +=`` lines from the command line, and a permanent edit
    would leak into every other type sharing the chipset.

Writes go through :mod:`cfgdoc`, so comments, ordering and unrecognised keys
survive the panel editing the file.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable
from typing import Any, Optional

from .cfgdoc import CfgDocument, parse_bool
from .errors import (
    AmbiguousSerialError,
    ConfigCorruptError,
    ConfigError,
    DuplicateTypeError,
    SerialTrackedElsewhereError,
    UnknownSerialError,
    UnknownTypeError,
)
from .paths import FW_TARGETS, Paths

SECTION_PREFIX = "mcu"
PATCH_SEPARATOR = "->"

@dataclasses.dataclass
class MakefilePatch:
    #: Relative to the firmware source tree, e.g. "src/stm32/Makefile".
    file: str
    line: str

    @classmethod
    def parse(cls, raw: str) -> Optional[MakefilePatch]:
        if PATCH_SEPARATOR not in raw:
            return None
        target, _, line = raw.partition(PATCH_SEPARATOR)
        patch = cls(file=target.strip(), line=line.strip())
        return patch if patch.is_valid() else None

    def render(self) -> str:
        return f"{self.file} {PATCH_SEPARATOR} {self.line}"

    def to_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line}

    def is_valid(self) -> bool:
        return bool(self.file and self.line)


@dataclasses.dataclass
class FwConfig:
    extra_args: str = ""
    #: None means "not specified". Only katapult blocks carry it.
    installed: Optional[bool] = None
    makefile_patches: list[MakefilePatch] = dataclasses.field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"extra_args": self.extra_args}
        if self.installed is not None:
            out["installed"] = self.installed
        if self.makefile_patches:
            out["makefile_patches"] = [p.to_json() for p in self.makefile_patches]
        return out


@dataclasses.dataclass
class McuType:
    name: str
    chipset: str = ""
    serials: list[str] = dataclasses.field(default_factory=list)
    fws: dict[str, FwConfig] = dataclasses.field(default_factory=dict)

    def fw(self, fw: str) -> FwConfig:
        return self.fws.setdefault(fw, FwConfig())

    @property
    def katapult_installed(self) -> bool:
        """Absent means True - a board with no bootloader is the exception."""
        val = self.fw("katapult").installed
        return True if val is None else val

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"chipset": self.chipset}
        for fw in FW_TARGETS:
            cfg = self.fws.get(fw)
            if cfg is not None:
                out[fw] = cfg.to_json()
        out["serials"] = list(self.serials)
        return out


def section_name(mcu_type: str) -> str:
    return f"{SECTION_PREFIX} {mcu_type}"


class Registry:
    """In-memory view of mcu-updater.cfg, backed by a comment-preserving document."""

    def __init__(self, types: dict[str, McuType], doc: CfgDocument) -> None:
        self.types = types
        self._doc = doc

    # --- construction / persistence ---

    @classmethod
    def load(cls, paths: Paths) -> Registry:
        path = paths.registry_file
        if not os.path.exists(path):
            cls._refuse_if_legacy(paths)
            return cls({}, CfgDocument())

        try:
            with open(path, encoding="utf-8") as fh:
                doc = CfgDocument(fh.read())
        except OSError as exc:
            raise ConfigCorruptError(f"could not read {path}: {exc}", path=path) from exc

        if doc.duplicate_sections:
            dupes = ", ".join(f"[{name}]" for name in doc.duplicate_sections)
            raise ConfigCorruptError(
                f"{path}: duplicate section(s) {dupes}. Only the first copy is read, so "
                f"everything in the later one is silently ignored - merge them into one.",
                path=path,
                value=doc.duplicate_sections,
            )

        types: dict[str, McuType] = {}
        for section in doc.section_names(SECTION_PREFIX):
            name = section[len(SECTION_PREFIX) :].strip()
            if not name:
                continue
            mcu = McuType(name=name, chipset=(doc.get(section, "chipset") or "").strip())
            mcu.serials = doc.get_list(section, "serials")
            for fw in FW_TARGETS:
                cfg = mcu.fw(fw)
                cfg.extra_args = (doc.get(section, f"{fw}_extra_args") or "").strip()
                for raw in doc.get_list(section, f"{fw}_makefile_patches"):
                    patch = MakefilePatch.parse(raw)
                    if patch is None:
                        raise ConfigCorruptError(
                            f"{path}: could not parse a makefile patch for '{name}': {raw!r}. "
                            f"Expected '<file> {PATCH_SEPARATOR} <line>'.",
                            path=path,
                            type=name,
                            value=raw,
                        )
                    cfg.makefile_patches.append(patch)
            installed = doc.get(section, "katapult_installed")
            if installed is not None:
                mcu.fw("katapult").installed = parse_bool(installed, True)
            types[name] = mcu

        return cls(types, doc)

    @staticmethod
    def _refuse_if_legacy(paths: Paths) -> None:
        """A pre-0.10 install has its registry somewhere we no longer look.

        Silently reporting "no MCU types configured" would be a data-loss shaped
        surprise: the next add-type would write a fresh file while the real one
        sat untouched in the old location.
        """
        legacy = next((p for p in paths.legacy_locations if os.path.exists(p)), None)
        if legacy is None:
            return
        raise ConfigError(
            f"found a registry at the old location {legacy}, but nothing at "
            f"{paths.registry_file}.\n"
            f"The layout moved: hand-edited config now lives under "
            f"{paths.config_dir} and build artifacts under {paths.data_dir}.\n"
            f"Convert the old file (see docs/layout.md) or move your .config "
            f"files across and re-add the types - then delete {legacy}.\n"
            f"Refusing to continue so an empty registry can't overwrite anything.",
            legacy=legacy,
            expected=paths.registry_file,
        )

    def save(self, paths: Paths) -> None:
        """Atomic write, preserving everything the document already had."""
        doc = self._doc

        for section in doc.section_names(SECTION_PREFIX):
            name = section[len(SECTION_PREFIX) :].strip()
            if name not in self.types:
                doc.remove_section(section)

        for name, mcu in self.types.items():
            section = section_name(name)
            doc.set(section, "chipset", mcu.chipset)
            doc.set(section, "serials", list(mcu.serials))

            # Only written when it differs from the default, to keep the file
            # readable rather than full of restated defaults.
            if mcu.fw("katapult").installed is False:
                doc.set(section, "katapult_installed", "false")
            else:
                doc.remove_option(section, "katapult_installed")

            for fw in FW_TARGETS:
                cfg = mcu.fws.get(fw)
                args_key = f"{fw}_extra_args"
                patch_key = f"{fw}_makefile_patches"
                if cfg is not None and cfg.extra_args.strip():
                    doc.set(section, args_key, cfg.extra_args.strip())
                else:
                    doc.remove_option(section, args_key)

                valid = [p for p in (cfg.makefile_patches if cfg else []) if p.is_valid()]
                if valid:
                    doc.set(section, patch_key, [p.render() for p in valid])
                else:
                    doc.remove_option(section, patch_key)

        os.makedirs(os.path.dirname(paths.registry_file), exist_ok=True)
        tmp = paths.registry_file + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(doc.render())
        os.replace(tmp, paths.registry_file)

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
        SerialTrackedElsewhereError if the serial belongs to a *different* type -
        that is a much stronger signal of a wrong selection than "this is a new
        device", so it is refused outright. Raises UnknownSerialError if it is
        simply untracked; the caller decides whether to offer adding it.
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
            raise DuplicateTypeError(f"MCU type '{name}' already exists.", type=name)
        mcu = McuType(
            name=name,
            chipset=chipset,
            serials=[],
            fws={
                "katapult": FwConfig(
                    extra_args=katapult_args,
                    installed=None if katapult_installed else False,
                ),
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
