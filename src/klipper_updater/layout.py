"""Gathering the per-type config folders under ``config_dir/types/``.

They used to sit directly in ``~/printer_data/config/mcu-updater``, beside
``mcu-updater.cfg``. That directory is what Mainsail's config editor opens, so a
printer with six board types showed six folders above the one file anyone
actually edits - and the folders hold ``.config`` files that are only ever
written by ``menuconfig``.

This runs once per process, at the entry points, rather than lazily when a
config file is read. Resolving each read against both locations would work and
would need no migration, but it would leave the old folders sitting in the
browser forever, which is the entire thing being fixed.

Nothing here is destructive: a folder is *moved*, never merged or deleted, and
only when it carries a saved config of ours. Anything else in ``config_dir`` is
somebody else's and is left exactly where it is.
"""

from __future__ import annotations

import os
import shutil

from .errors import ConfigError
from .paths import FW_TARGETS, TYPE_SUBDIR, Paths


def _has_saved_config(path: str) -> bool:
    """Does this directory hold a saved menuconfig answer file?

    The marker that a directory is one of ours. ``config_dir`` is a place a user
    can put anything - it is served by Moonraker and opens in a file browser -
    so moving a directory on the strength of its position alone would eventually
    relocate somebody's notes.
    """
    try:
        entries = set(os.listdir(path))
    except OSError:
        return False
    return any(f"{fw}.config" in entries for fw in FW_TARGETS)


def _refuse_if_types_is_itself_a_type(paths: Paths) -> None:
    """A type named `types` predating this change collides with the new root.

    Vanishingly unlikely - but the failure mode if it happened silently is that
    the migration moves every other type *inside* somebody's config folder, so
    it is worth the four lines.

    This is also the only reason the loop below needs no special case for the
    new root: once already migrated, `types/` holds one folder per type and no
    `.config` of its own, so it fails the "is this one of ours" test like any
    other directory.
    """
    root = paths.type_root
    if os.path.isdir(root) and _has_saved_config(root):
        raise ConfigError(
            f"{root} holds saved menuconfig answers directly, which means you have "
            f"an MCU type named '{TYPE_SUBDIR}' from before per-type folders moved "
            f"under it.\n"
            f"Rename that type (or its folder) and re-run - refusing to migrate on "
            f"top of it.",
            path=root,
        )


def migrate_type_dirs(paths: Paths) -> list[str]:
    """Move any legacy per-type folder under ``types/``. Returns what moved.

    Idempotent, and safe to call from two processes at once: the loser of a race
    finds the source already gone and moves on.
    """
    root = paths.config_dir
    if not os.path.isdir(root):
        return []

    _refuse_if_types_is_itself_a_type(paths)

    moved: list[str] = []
    for name in sorted(os.listdir(root)):
        legacy = os.path.join(root, name)
        if not os.path.isdir(legacy) or not _has_saved_config(legacy):
            continue

        target = paths.type_dir(name)
        if os.path.exists(target):
            # Two folders claiming to be the same type. Picking one would mean
            # building from a config the user cannot see, which is the exact
            # failure this project exists to stop happening.
            raise ConfigError(
                f"cannot move {legacy} to {target}: something is already there.\n"
                f"Both hold config for the type '{name}'. Keep whichever is current, "
                f"delete the other, and re-run.",
                type=name,
                legacy=legacy,
                expected=target,
            )

        os.makedirs(paths.type_root, exist_ok=True)
        try:
            shutil.move(legacy, target)
        except OSError as exc:
            if not os.path.isdir(legacy):
                continue  # another process migrated it between our check and now
            raise ConfigError(
                f"could not move {legacy} to {target}: {exc}", type=name, path=legacy
            ) from exc
        moved.append(name)

    return moved
