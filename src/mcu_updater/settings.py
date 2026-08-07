"""Tool settings: the ``[updater]`` section of ``mcu-updater.cfg``.

Shares a file with the MCU registry. They were separate while the registry was
JSON - a settings key inside a dict keyed by board name would have been ugly and
liable to collide with a board called "settings" - but ``.cfg`` sections namespace
cleanly, so there is one file to find and one file to edit.

Reads and writes go through :mod:`cfgdoc`, which means editing a setting from the
panel does not throw away the ``[mcu ...]`` sections, the comments, or anything
else in the file.

Everything has a default, so the section is optional and may be partial.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Optional

from .cfgdoc import CfgDocument, parse_bool
from .errors import ConfigError

SECTION = "updater"


@dataclasses.dataclass
class Settings:
    #: 0 means "pass no -j flag at all", which is what the original script did.
    #: Opt in explicitly rather than silently changing everyone's build.
    make_jobs: int = 0

    #: `make clean` before every build. Keep this on: skipping it after a
    #: .config change is exactly how you get a stale-object mismatch and flash
    #: a subtly wrong binary.
    clean_before_build: bool = True

    #: systemd unit name. KIAUH multi-instance setups use klipper-1, klipper-2...
    service: str = "klipper"

    #: "moonraker" | "systemd" | "null". Only the agent honours "moonraker";
    #: the CLI always uses systemd since it has no Moonraker connection.
    service_backend: str = "moonraker"

    #: Echo commands and fake their output instead of running them.
    dry_run: bool = False

    #: Agent-only safety gate. The CLI ignores this entirely - it has always been
    #: able to flash and that did not change. Defaults off so installing an
    #: update never silently grants a browser the ability to reflash the printer.
    enable_flashing: bool = False

    #: Bypass the "is the printer busy?" check. Almost never what you want.
    allow_flash_while_printing: bool = False

    #: Per-job log ring buffer size, in lines.
    log_ring_size: int = 2000

    #: Default PlatformIO source tree for every [display <name>] section, so one
    #: repo shared by every env is written once. A section's own `source:` wins.
    display_source: str = ""

    #: PlatformIO launcher, if it is somewhere `pio` on PATH and the standard
    #: ~/.platformio/penv/bin/pio will not find it.
    platformio_bin: str = ""

    @property
    def resolved_jobs(self) -> int:
        """make_jobs, or a sensible auto value if it was set to a negative."""
        if self.make_jobs < 0:
            return os.cpu_count() or 1
        return self.make_jobs

    def make_flags(self) -> list[str]:
        n = self.resolved_jobs
        return [f"-j{n}"] if n > 0 else []


_BOOL_FIELDS = {
    "clean_before_build",
    "dry_run",
    "enable_flashing",
    "allow_flash_while_printing",
}
_INT_FIELDS = {"make_jobs", "log_ring_size"}
_STR_FIELDS = {"service", "service_backend", "display_source", "platformio_bin"}
_BACKENDS = ("moonraker", "systemd", "null")


def _read(path: str) -> CfgDocument:
    if not os.path.exists(path):
        return CfgDocument()
    try:
        with open(path, encoding="utf-8") as fh:
            return CfgDocument(fh.read())
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}", path=path) from exc


def load_settings(path: str) -> Settings:
    """Read the [updater] section. A missing file or section yields defaults.

    A *malformed value* still raises. Silently ignoring `dry_run = maybe` means
    the user's dry run quietly does not apply, which is the kind of surprise that
    ends up flashing a board.
    """
    s = Settings()
    doc = _read(path)

    # Appending a second [updater] block rather than editing the existing one is
    # an easy mistake, and first-wins would mean `enable_flashing: true` silently
    # doing nothing. Klipper refuses duplicate sections; so do we.
    if SECTION in doc.duplicate_sections:
        raise ConfigError(
            f"{path} has more than one [{SECTION}] section. Only the first is read, so "
            f"the settings in the later one would be silently ignored - merge them.",
            path=path,
        )

    if not doc.has_section(SECTION):
        return s

    for key in doc.options(SECTION):
        name = key.replace("-", "_")
        raw = doc.get(SECTION, key)
        if raw is None:
            continue
        try:
            if name in _BOOL_FIELDS:
                value = parse_bool(raw, default=None)
                if value is None:
                    raise ValueError(f"expected a boolean, got {raw!r}")
                setattr(s, name, value)
            elif name in _INT_FIELDS:
                setattr(s, name, int(raw.strip()))
            elif name in _STR_FIELDS:
                setattr(s, name, raw.strip())
            # Unknown keys are ignored rather than fatal: a newer version of the
            # tool may have written a setting this version does not know yet.
        except ValueError as exc:
            raise ConfigError(
                f"bad value for '{key}' in [{SECTION}] of {path}: {exc}", path=path, key=key
            ) from exc

    if s.service_backend not in _BACKENDS:
        raise ConfigError(
            f"service_backend must be one of {'/'.join(_BACKENDS)}, got '{s.service_backend}'",
            path=path,
            key="service_backend",
        )
    return s


def save_settings(path: str, settings: Settings) -> None:
    """Write the [updater] section, leaving the rest of the file alone.

    Load-modify-write against what is on disk rather than a cached document, so
    this cannot clobber [mcu ...] sections written in the meantime.
    """
    doc = _read(path)
    for field in dataclasses.fields(settings):
        value: Any = getattr(settings, field.name)
        if isinstance(value, bool):
            value = "true" if value else "false"
        doc.set(SECTION, field.name, value)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(doc.render())
    os.replace(tmp, path)


def legacy_settings_warning(paths: Any) -> Optional[str]:
    """A stale updater.conf from before the merge is now ignored.

    Not fatal - losing settings reverts to safe defaults rather than destroying
    anything - but `enable_flashing` silently going back to false is worth saying
    out loud rather than leaving someone to wonder why the flash buttons vanished.
    """
    legacy = paths.legacy_settings_file
    if os.path.exists(legacy) and legacy != paths.main_config:
        return (
            f"{legacy} is no longer read: settings moved into the [updater] section "
            f"of {paths.main_config}. Copy anything you had set across, then delete it."
        )
    return None
