"""Tool settings, at ``~/printer_data/config/mcu-updater/updater.conf``.

Deliberately a separate file from ``mcus.cfg``: that file is a
type-keyed map the user hand-edits, so a ``_settings`` key in it would be both
ugly and liable to collide with a board called "settings". INI format to match
the surrounding klipper/moonraker ecosystem.

Everything has a default, so the file is optional and may be partial.
"""

from __future__ import annotations

import configparser
import dataclasses
import os

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

    #: Agent-only safety gate. The CLI ignores this entirely - it has always
    #: been able to flash and Phase 0 must not change that. Defaults off so
    #: that when the web flash path lands, nobody starts flashing from a
    #: browser by accident on upgrade day.
    enable_flashing: bool = False

    #: Bypass the "is a print running?" check. Almost never what you want.
    allow_flash_while_printing: bool = False

    #: Per-job log ring buffer size, in lines.
    log_ring_size: int = 2000

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
_STR_FIELDS = {"service", "service_backend"}


def load_settings(path: str) -> Settings:
    """Read updater.conf. A missing file yields defaults; a broken one raises.

    Silently ignoring a malformed settings file means the user's `dry_run: true`
    quietly doesn't apply, which is the kind of surprise that flashes a board.
    """
    s = Settings()
    if not os.path.exists(path):
        return s

    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"could not parse {path}: {exc}", path=path) from exc

    if not parser.has_section(SECTION):
        return s

    for key in parser.options(SECTION):
        name = key.replace("-", "_")
        try:
            if name in _BOOL_FIELDS:
                setattr(s, name, parser.getboolean(SECTION, key))
            elif name in _INT_FIELDS:
                setattr(s, name, parser.getint(SECTION, key))
            elif name in _STR_FIELDS:
                setattr(s, name, parser.get(SECTION, key).strip())
            # Unknown keys are ignored rather than fatal: a newer version of the
            # tool may have written a setting this version doesn't know yet.
        except ValueError as exc:
            raise ConfigError(
                f"bad value for '{key}' in {path}: {exc}", path=path, key=key
            ) from exc

    if s.service_backend not in ("moonraker", "systemd", "null"):
        raise ConfigError(
            f"service_backend must be moonraker/systemd/null, got '{s.service_backend}'",
            path=path,
            key="service_backend",
        )
    return s


def save_settings(path: str, settings: Settings) -> None:
    parser = configparser.ConfigParser()
    parser[SECTION] = {
        f.name: str(getattr(settings, f.name)) for f in dataclasses.fields(settings)
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        parser.write(fh)
    os.replace(tmp, path)
