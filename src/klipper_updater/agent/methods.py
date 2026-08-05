"""The agent's JSON-RPC surface.

Every method takes one object and returns one object, and **every method returns
in well under a second**. That is a hard rule, not a guideline: Moonraker awaits
our reply with no timeout, so anything slow would hold a front end's HTTP request
open. Long-running work (build, flash) returns a job id immediately instead -
those arrive in a later phase.

The shapes here are the contract with the Mainsail panel. They are documented in
``docs/agent-api.md`` and version-gated by ``fw.ping``'s ``api_version``.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import time
from typing import Any, Callable, Optional

from .. import API_VERSION, __version__
from ..build import read_sidecar, staleness
from ..config import Registry
from ..devices import (
    STATE_KATAPULT,
    STATE_OFFLINE,
    BusDevice,
    device_state,
    parse_entry,
    scan,
)
from ..errors import (
    DfuPermissionError,
    OperationCancelled,
    SerialTrackedElsewhereError,
    ToolMissingError,
    UpdaterError,
)
from ..paths import FW_TARGETS, REENUMERATE_TIMEOUT, Paths
from ..settings import Settings, load_settings, save_settings
from .rpc import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, MethodNotFound, RpcError

#: How long a Moonraker query may block before we give up and report unknown.
#: Small on purpose - these are best-effort enrichments of fw.status, and the
#: whole call has a sub-second budget.
PROBE_TIMEOUT = 1.5


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


#: git describe embeds the commit as a g<hex> token. Anything after it - notably
#: `-dirty`, which a makefile-patched build always carries - is noise here.
_FW_SHA_RE = re.compile(r"(?:^|-)g([0-9a-f]{7,40})(?:-|$)")

#: The MCU object list only changes when Klipper restarts, so it is worth caching:
#: it is a whole extra round trip and fw.status has a sub-second budget.
MCU_NAMES_TTL = 60.0


def _running_sha(version: str) -> Optional[str]:
    match = _FW_SHA_RE.search(version or "")
    return match.group(1) if match else None


def _serial_from_path(path: str) -> Optional[str]:
    """The serial component of a /dev/serial/by-id path.

    Reuses the bus parser rather than string-slicing, so the two cannot disagree
    about what counts as a serial.
    """
    parsed = parse_entry(os.path.basename(path), os.path.dirname(path))
    return parsed.serial if parsed is not None else None


def serialize_device(dev: BusDevice, tracked_by: Optional[str] = None) -> dict[str, Any]:
    return {
        "fw": dev.fw,
        "chipset": dev.chipset,
        "serial": dev.serial,
        "path": dev.path,
        "state": dev.state,
        "tracked_by": tracked_by,
        # Whether "track this" may be offered for it. Anything with two
        # underscores in its by-id name parses as a device, so the list also
        # contains USB serial adapters - and offering to adopt a Knomi's CH340 is
        # one tap from building Klipper firmware for a display.
        "is_mcu": dev.is_mcu,
    }


class Api:
    """Read-only view of the tool's state, exposed over JSON-RPC."""

    def __init__(
        self,
        paths: Paths,
        *,
        call: Optional[Callable[[str, Any, float], Any]] = None,
        runner: Optional[Any] = None,
        logger: Any = None,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.paths = paths
        # Called after a mutation so every connected client re-syncs. Injected
        # rather than reached for, keeping this class free of the transport.
        self._on_change = on_change
        # Injected so this class never touches the transport directly, which is
        # what makes it testable without a Moonraker.
        self._call = call
        # None in a read-only deployment; the build methods then report that the
        # capability is absent rather than half-working.
        self.runner = runner
        self._log = logger
        # Created on first kconfig call; an agent that never opens one pays
        # neither the import nor the memory.
        self._kconfig_sessions: Optional[Any] = None
        # Cached printer-object names; see _mcu_object_names.
        self._mcu_names: Optional[list[str]] = None
        self._mcu_names_at = 0.0

    # -- helpers -----------------------------------------------------------

    def settings(self) -> Settings:
        """Re-read every time: the user may have edited mcu-updater.cfg."""
        try:
            return load_settings(self.paths.settings_file)
        except UpdaterError as exc:
            if self._log is not None:
                self._log.warning(f"the [updater] section is invalid, using defaults: {exc}")
            return Settings()

    def registry(self) -> Registry:
        return Registry.load(self.paths)

    def _probe(self, method: str, params: Any = None) -> Any:
        """Ask Moonraker something, tolerating any failure."""
        if self._call is None:
            return None
        try:
            return self._call(method, params, PROBE_TIMEOUT)
        except Exception:  # noqa: BLE001 - enrichment only, never fatal
            return None

    def klipper_service_state(self) -> Optional[str]:
        info = self._probe("machine.system_info")
        try:
            return info["system_info"]["service_state"]["klipper"]["active_state"]
        except (TypeError, KeyError):
            return None

    def is_printing(self, activity: Optional[dict] = None) -> Optional[bool]:
        state = (activity or self._printer_activity()).get("print_state")
        if state is None:
            return None
        return state in ("printing", "paused")

    # -- serialisers -------------------------------------------------------

    def artifact(self, mcu_type: str, fw: str) -> dict[str, Any]:
        cfg = self.paths.config_file(mcu_type, fw)
        binary = self.paths.bin_file(mcu_type, fw)
        uf2 = self.paths.uf2_file(mcu_type, fw)
        side = read_sidecar(self.paths, mcu_type, fw) or {}
        stale, reason = staleness(self.paths, mcu_type, fw)

        from ..build import git_head

        return {
            "has_config": os.path.exists(cfg),
            "config_mtime": _mtime(cfg),
            "has_bin": os.path.exists(binary),
            "bin_mtime": _mtime(binary),
            "bin_size": _size(binary),
            "has_uf2": os.path.exists(uf2),
            "built_fw_sha": side.get("fw_sha"),
            "current_fw_sha": git_head(self.paths.fw_dir(fw)),
            "stale": stale,
            "stale_reason": reason,
            "last_build_seconds": side.get("duration"),
            "last_build_at": side.get("timestamp"),
            # True when make ran olddefconfig over our saved answers, which
            # silently changes settings after a klipper git pull.
            "config_rewritten": bool(side.get("config_rewritten")),
        }

    def type_status(
        self,
        reg: Registry,
        name: str,
        versions: Optional[dict[str, dict[str, str]]] = None,
        fw_head: Optional[str] = None,
    ) -> dict[str, Any]:
        """One type's state, including what each of its boards is *running*.

        `versions` and `fw_head` are passed in rather than looked up here, because
        each costs a Moonraker round trip and a caller with ten types would
        otherwise make ten of them.
        """
        from ..build import git_head

        mcu = reg.get(name)
        if versions is None:
            versions = self.mcu_info()
        if fw_head is None:
            fw_head = git_head(self.paths.fw_dir("klipper"))

        # Read once per type, not per board: it is one small file, but a ten-board
        # type would otherwise open it ten times.
        from ..build import FlashLog

        flashlog = FlashLog(self.paths)
        artifact_sha = (read_sidecar(self.paths, name, "klipper") or {}).get("bin_sha256")

        serials = []
        for serial in mcu.serials:
            state, path = device_state(self.paths, mcu.chipset, serial)
            entry = {"serial": serial, "state": state, "path": path}
            entry.update(
                self.flash_state(
                    serial,
                    versions,
                    fw_head,
                    state=state,
                    artifact_sha=artifact_sha,
                    flashlog=flashlog,
                )
            )
            serials.append(entry)

        out: dict[str, Any] = {
            "name": name,
            "chipset": mcu.chipset,
            "serials": serials,
            "artifacts": {fw: self.artifact(name, fw) for fw in FW_TARGETS},
            "katapult_installed": mcu.katapult_installed,
            # True when at least one board is behind the source tree. Distinct from
            # the artifact being stale: "needs rebuilding" and "needs flashing" are
            # different questions, and reporting only the first is what let a board
            # 90 commits behind show as up to date.
            "needs_flash": any(s.get("needs_flash") for s in serials),
        }
        for fw in FW_TARGETS:
            cfg = mcu.fw(fw)
            block: dict[str, Any] = {
                "extra_args": cfg.extra_args,
                "makefile_patches": [p.to_json() for p in cfg.makefile_patches],
            }
            if fw == "katapult":
                block["installed"] = mcu.katapult_installed
            out[fw] = block
        return out

    def bus(self, reg: Registry) -> list[dict[str, Any]]:
        owner: dict[str, str] = {}
        for name, mcu in reg.items():
            for serial in mcu.serials:
                owner[serial] = name
        return [serialize_device(d, owner.get(d.serial)) for d in scan(self.paths)]

    # -- methods -----------------------------------------------------------

    def ping(self, args: dict) -> dict[str, Any]:
        s = self.settings()
        return {
            "api_version": API_VERSION,
            "version": __version__,
            "dry_run": s.dry_run,
            "enable_flashing": s.enable_flashing,
            "phase": 2 if self.runner is not None else 1,
            "capabilities": sorted(self.available_methods()),
            "host": {
                "nproc": os.cpu_count(),
                "python": platform.python_version(),
                "config_dir": self.paths.config_dir,
                "data_dir": self.paths.data_dir,
            },
            "now": time.time(),
        }

    def status(self, args: dict) -> dict[str, Any]:
        """One call paints the whole panel."""
        reg = self.registry()
        s = self.settings()
        current = self.runner.current() if self.runner else None
        activity = self._printer_activity()
        from ..build import git_head

        versions = self.mcu_info()
        fw_head = git_head(self.paths.fw_dir("klipper"))
        return {
            "types": [self.type_status(reg, n, versions, fw_head) for n in reg.names()],
            "bus": self.bus(reg),
            "job": current.to_dict() if current else None,
            "recent": [j.to_dict() for j in self.runner.recent(10)] if self.runner else [],
            "locked_by": self.lock_holder(),
            # Per firmware tree, so the panel can hide the configure button
            # rather than offer one that fails on a host missing the source.
            "kconfig_available": self.kconfig_available(),
            "klipper_service": self.klipper_service_state(),
            "printing": self.is_printing(activity),
            # idle_timeout.state. The panel needs this as well as `printing`:
            # print_stats stays "standby" through a manual home or QGL, and
            # stopping klipper mid-motion is just as destructive.
            "idle_state": activity.get("idle_state"),
            "settings": dataclasses.asdict(s),
            # True while nothing here can build or flash. The panel uses
            # `capabilities` from fw.ping for per-control gating.
            "read_only": self.runner is None,
        }

    def lock_holder(self) -> Optional[dict[str, Any]]:
        from ..lock import ExclusiveLock

        return ExclusiveLock(self.paths).holder()

    def type_list(self, args: dict) -> dict[str, Any]:
        from ..build import git_head

        reg = self.registry()
        versions = self.mcu_info()
        fw_head = git_head(self.paths.fw_dir("klipper"))
        return {"types": [self.type_status(reg, n, versions, fw_head) for n in reg.names()]}

    def bus_scan(self, args: dict) -> dict[str, Any]:
        """Everything on the bus, plus the subset worth offering to track.

        `adoptable` is what a "track this" affordance should iterate. It is a
        separate key rather than a filter applied to `devices` so the panel can
        still *show* the other entries - a user hunting for a board that has not
        appeared is better served by seeing what did appear than by an empty list.
        """
        reg = self.registry()
        devices = self.bus(reg)
        if args.get("only_untracked"):
            devices = [d for d in devices if d["tracked_by"] is None]
        chipset = args.get("chipset")
        if chipset:
            devices = [d for d in devices if d["chipset"] == chipset]
        return {
            "devices": devices,
            "adoptable": [
                d for d in devices if d["is_mcu"] and d["tracked_by"] is None
            ],
        }

    def artifacts(self, args: dict) -> dict[str, Any]:
        name = args.get("name")
        if not name:
            raise RpcError("'name' is required", ERR_INVALID_PARAMS)
        reg = self.registry()
        reg.get(str(name))  # raises UnknownTypeError for an unknown type
        return {fw: self.artifact(str(name), fw) for fw in FW_TARGETS}

    def settings_get(self, args: dict) -> dict[str, Any]:
        return {"settings": dataclasses.asdict(self.settings())}

    #: Settings the panel may change. Everything here is a *behaviour* preference.
    #:
    #: `service` and `service_backend` are deliberately absent. They describe how
    #: this host is wired, not what the user wants, and getting them wrong breaks
    #: the agent's ability to stop Klipper - `service_backend: null` in particular
    #: would let a real flash proceed *without* stopping it, which fails at best
    #: and corrupts a board at worst. Nothing about a browser form makes that a
    #: sensible thing to offer; editing the cfg is the right amount of friction.
    SETTABLE = (
        "make_jobs",
        "clean_before_build",
        "dry_run",
        "enable_flashing",
        "allow_flash_while_printing",
        "log_ring_size",
    )

    #: Changing either of these changes what a flash is allowed to do, so they are
    #: logged at warning level: the agent log is the only audit trail there is.
    LOUD_SETTINGS = ("enable_flashing", "allow_flash_while_printing")

    def settings_set(self, args: dict) -> dict[str, Any]:
        """Change tool settings. Only the keys supplied are touched.

        Writes through `save_settings`, which load-modify-writes the ``[updater]``
        section via CfgDocument - so the ``[mcu ...]`` sections and every comment
        in the shared file survive.
        """
        patch = args.get("settings")
        if not isinstance(patch, dict) or not patch:
            raise RpcError(
                "'settings' must be a non-empty object of the values to change",
                ERR_INVALID_PARAMS,
            )

        unknown = [k for k in patch if k not in self.SETTABLE]
        if unknown:
            raise RpcError(
                f"cannot set {', '.join(sorted(unknown))} from here. Settable: "
                f"{', '.join(self.SETTABLE)}. 'service' and 'service_backend' "
                f"describe how this host is wired and are edited in "
                f"{self.paths.settings_file}.",
                data={
                    "code": "setting_not_settable",
                    "message": "one or more settings cannot be changed remotely",
                    "data": {"rejected": sorted(unknown), "settable": list(self.SETTABLE)},
                },
            )

        current = self.settings()
        changed: dict[str, Any] = {}
        for key, raw in patch.items():
            value = self._coerce_setting(key, raw)
            if value != getattr(current, key):
                changed[key] = value
            setattr(current, key, value)

        save_settings(self.paths.settings_file, current)

        for key in self.LOUD_SETTINGS:
            if key in changed and self._log is not None:
                self._log.warning(f"{key} was changed to {changed[key]} from the panel")

        self._changed()
        return {"settings": dataclasses.asdict(current), "changed": sorted(changed)}

    @staticmethod
    def _coerce_setting(key: str, raw: Any) -> Any:
        """Validate one setting, refusing rather than clamping.

        Silently clamping a value the user typed means the UI shows one thing and
        the tool does another - the same class of quiet disagreement that made a
        working QGL refusal look like a dead agent.
        """
        if key in ("clean_before_build", "dry_run", "enable_flashing", "allow_flash_while_printing"):
            if not isinstance(raw, bool):
                raise RpcError(f"'{key}' must be true or false", ERR_INVALID_PARAMS)
            return raw

        if isinstance(raw, bool) or not isinstance(raw, int):
            # bool is an int subclass, so it would otherwise sail through.
            raise RpcError(f"'{key}' must be a whole number", ERR_INVALID_PARAMS)

        if key == "make_jobs":
            # -1 and below mean "one job per CPU"; 0 means pass no -j flag at all.
            if raw < -1 or raw > 64:
                raise RpcError(
                    "'make_jobs' must be between -1 and 64 (-1 = one per CPU, 0 = no -j flag)",
                    ERR_INVALID_PARAMS,
                )
            return raw

        if key == "log_ring_size":
            if raw < 100 or raw > 100_000:
                raise RpcError("'log_ring_size' must be between 100 and 100000", ERR_INVALID_PARAMS)
            return raw

        raise RpcError(f"'{key}' cannot be set", ERR_INVALID_PARAMS)

    # -- registry mutation -------------------------------------------------

    def _changed(self) -> None:
        """Announce a mutation. Never lets a broadcast failure undo a good write.

        The registry is already saved by the time this runs, so raising here would
        report a failure for something that succeeded - and the client would then
        show stale state *and* an error.
        """
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:  # noqa: BLE001
            if self._log is not None:
                self._log.warning(f"could not emit state after a change: {exc}")

    @staticmethod
    def _require_str(args: dict, key: str) -> str:
        value = args.get(key)
        if not value or not str(value).strip():
            raise RpcError(f"'{key}' is required", ERR_INVALID_PARAMS)
        return str(value).strip()

    def serial_add(self, args: dict) -> dict[str, Any]:
        """Track a physical board under an existing type.

        Touches nothing but the registry: no build, no flash, no board.
        """
        name = self._require_str(args, "name")
        serial = self._require_str(args, "serial")

        # The panel only offers `adoptable` devices, but the panel is not the only
        # possible caller - enforce the same rule here so a direct RPC cannot add
        # a Knomi's CH340 as a board. Only refused when we can actually see it:
        # a serial for a board that is currently unplugged is legitimate.
        present = next((d for d in scan(self.paths) if d.serial == serial), None)
        if present is not None and not present.is_mcu:
            raise RpcError(
                f"{serial} is on the bus but does not look like a Klipper or Katapult "
                f"board (it enumerates as '{present.fw}'). Refusing to track it - "
                f"building firmware for a USB serial adapter cannot end well.",
                data={
                    "code": "not_an_mcu",
                    "message": "device is not a Klipper or Katapult board",
                    "data": {"serial": serial, "fw": present.fw, "path": present.path},
                },
            )

        with Registry.mutate(self.paths, f"add serial {serial}") as reg:
            mcu = reg.get(name)  # UnknownTypeError if the type doesn't exist
            # One board tracked under two types would get flashed twice with
            # different firmware, so this is refused rather than merged.
            elsewhere = [t for t in reg.find_types_for_serial(serial) if t != name]
            if elsewhere:
                raise SerialTrackedElsewhereError(
                    f"serial '{serial}' is already tracked under '{elsewhere[0]}'. "
                    f"Remove it from there first if it really belongs to '{name}'.",
                    serial=serial,
                    requested=name,
                    tracked_under=elsewhere,
                )
            added = reg.add_serial(name, serial)
            chipset = mcu.chipset

        self._changed()
        return {"name": name, "serial": serial, "added": added, "chipset": chipset}

    def type_add(self, args: dict) -> dict[str, Any]:
        """Register a board model.

        The name is validated by the model, not here - it becomes both a config
        section and a directory, and the CLI must apply the same rule.
        """
        name = self._require_str(args, "name")
        chipset = self._require_str(args, "chipset")
        installed = args.get("katapult_installed")

        with Registry.mutate(self.paths, f"add type {name}") as reg:
            reg.add_type(
                name,
                chipset,
                klipper_args=str(args.get("klipper_extra_args") or "").strip(),
                katapult_args=str(args.get("katapult_extra_args") or "").strip(),
                katapult_installed=True if installed is None else bool(installed),
            )

        self._changed()
        return {"name": name, "chipset": chipset}

    def type_update(self, args: dict) -> dict[str, Any]:
        """Edit a type in place. Only the keys supplied are touched.

        Renaming is deliberately not offered. The name is also a directory holding
        the saved menuconfig answers, so a rename is a filesystem migration rather
        than a config edit - and the answers are the one thing here that cannot be
        regenerated.
        """
        name = self._require_str(args, "name")
        if args.get("new_name"):
            raise RpcError(
                "renaming a type isn't supported here: the name is also the "
                "directory holding its saved menuconfig answers. Add a type with "
                "the new name, move that directory across, then remove the old one.",
                data={"code": "rename_unsupported", "message": "renaming is a migration"},
            )

        warnings: list[str] = []
        with Registry.mutate(self.paths, f"update type {name}") as reg:
            mcu = reg.get(name)

            if "chipset" in args:
                chipset = self._require_str(args, "chipset")
                if chipset != mcu.chipset:
                    # Staleness compares the source commit and a hash of the
                    # .config, neither of which changes when the chipset does - so
                    # a binary built for the old chip would keep reporting itself
                    # as fresh. Say so rather than let it be flashed.
                    if self.artifact(name, "klipper").get("has_bin"):
                        warnings.append(
                            f"the built firmware for '{name}' was compiled for "
                            f"{mcu.chipset}. Rebuild before flashing - staleness "
                            f"cannot detect a chipset change on its own."
                        )
                    mcu.chipset = chipset

            for fw in FW_TARGETS:
                key = f"{fw}_extra_args"
                if key in args:
                    mcu.fw(fw).extra_args = str(args.get(key) or "").strip()

            if "katapult_installed" in args:
                installed = bool(args.get("katapult_installed"))
                # Only stored when false; absent means true, which keeps the file
                # free of restated defaults.
                mcu.fw("katapult").installed = None if installed else False

            result: dict[str, Any] = {"name": name, "chipset": mcu.chipset}

        self._changed()
        result["warnings"] = warnings
        return result

    def type_remove(self, args: dict) -> dict[str, Any]:
        """Stop tracking a board model.

        Removes the registry section and nothing else. The saved menuconfig
        answers stay on disk, which matters because they are the one thing here
        that genuinely cannot be regenerated - so re-adding the same name gets
        everything back.

        Refuses while boards are still tracked under it unless forced: removing a
        type with live boards is far more often a misclick than an intention.
        """
        name = self._require_str(args, "name")
        force = bool(args.get("force"))

        with Registry.mutate(self.paths, f"remove type {name}") as reg:
            mcu = reg.get(name)
            count = len(mcu.serials)
            if count and not force:
                raise RpcError(
                    f"'{name}' still tracks {count} board(s). Remove them first, or "
                    f"confirm to remove the type and its serials together.",
                    data={
                        "code": "type_has_serials",
                        "message": "type still tracks boards",
                        "data": {"type": name, "serials": list(mcu.serials)},
                    },
                )
            reg.remove_type(name)

        self._changed()
        return {
            "name": name,
            "removed_serials": count,
            # The panel promises this, so it is part of the contract rather than
            # only a comment.
            "kept_config_dir": self.paths.type_dir(name),
        }

    def serial_remove(self, args: dict) -> dict[str, Any]:
        """Stop tracking a board.

        Deliberately non-destructive, and the panel should say so: the board keeps
        its firmware, the type keeps its saved .config and its built artifacts, and
        re-adding the serial makes it flashable again with nothing to rebuild.
        """
        name = self._require_str(args, "name")
        serial = self._require_str(args, "serial")

        with Registry.mutate(self.paths, f"remove serial {serial}") as reg:
            reg.get(name)  # UnknownTypeError if the type doesn't exist
            removed = reg.remove_serial(name, serial)

        self._changed()
        return {"name": name, "serial": serial, "removed": removed}

    # -- jobs --------------------------------------------------------------

    def _require_runner(self):
        if self.runner is None:
            raise RpcError(
                "this agent is running read-only; no job runner is available",
                ERR_METHOD_NOT_FOUND,
            )
        return self.runner

    def build(self, args: dict) -> dict[str, Any]:
        """Start a build. Returns a job id immediately - never blocks."""
        runner = self._require_runner()
        name = args.get("name")
        fw = args.get("fw")
        if not name or fw not in FW_TARGETS:
            raise RpcError(
                f"'name' is required and 'fw' must be one of {list(FW_TARGETS)}",
                ERR_INVALID_PARAMS,
            )
        name, fw = str(name), str(fw)

        reg = self.registry()
        reg.get(name)  # fail fast on an unknown type, before creating a job
        if not os.path.exists(self.paths.config_file(name, fw)):
            # menuconfig is ncurses and cannot run here. Say so precisely rather
            # than starting a job that dies immediately.
            raise RpcError(
                f"{name} has no saved {fw} config. Run "
                f"'updatefw menuconfig -t {name} -f {fw}' over SSH once first.",
                data={
                    "code": "no_saved_config",
                    "message": "menuconfig has never been run for this type",
                    "data": {"type": name, "fw": fw},
                },
            )

        jobs = args.get("jobs")
        clean = args.get("clean")

        def run(ctx) -> dict[str, Any]:
            from ..build import build as do_build

            ctx.step(f"Building {fw} for {name}", 0, 1)
            result = do_build(
                self.paths,
                self.registry(),
                self.settings(),
                name,
                fw,
                reporter=ctx.reporter,
                cancel=ctx.cancel,
                jobs=int(jobs) if jobs is not None else None,
                clean=bool(clean) if clean is not None else None,
            )
            ctx.step(f"Built {fw} for {name}", 1, 1)
            return {
                "type": name,
                "fw": fw,
                "bin_path": result.bin_path,
                "uf2_path": result.uf2_path,
                "duration": round(result.duration, 2),
                "fw_sha": result.fw_sha,
                "config_rewritten": result.config_rewritten,
            }

        job = runner.submit("build", {"name": name, "fw": fw}, run)
        return {"job_id": job.id, "job": job.to_dict()}

    def flash(self, args: dict) -> dict[str, Any]:
        """Flash one board. Returns a job id immediately.

        Every refusal happens *here*, synchronously, before a job exists - so the
        caller gets a real explanation instead of a job that fails a second later.
        In order: capability gate, argument validation, type/serial pairing,
        artifact present, board actually attached, and finally the print gate.
        """
        runner = self._require_runner()
        settings = self.settings()

        # Deliberately off by default: updating the agent must never silently
        # grant a browser the ability to reflash the printer.
        if not settings.enable_flashing:
            raise RpcError(
                "flashing from the web UI is disabled. Set 'enable_flashing = true' in "
                f"{self.paths.settings_file} and restart the agent to allow it.",
                data={
                    "code": "flashing_disabled",
                    "message": "enable_flashing is false",
                    "data": {"settings_file": self.paths.settings_file},
                },
            )

        serial = args.get("serial")
        if not serial:
            raise RpcError("'serial' is required", ERR_INVALID_PARAMS)
        serial = str(serial)
        name = args.get("name")
        force = bool(args.get("force"))

        reg = self.registry()
        # resolve_serial raises unknown_serial / ambiguous_serial /
        # serial_tracked_elsewhere, all of which the panel switches on by code.
        mcu_type = reg.resolve_serial(serial, str(name) if name else None)
        mcu = reg.get(mcu_type)

        fw_bin = self.paths.bin_file(mcu_type, "klipper")
        if not os.path.exists(fw_bin):
            raise RpcError(
                f"no built firmware for {mcu_type} at {fw_bin}. Build it first.",
                data={
                    "code": "no_artifact",
                    "message": "firmware has not been built",
                    "data": {"type": mcu_type, "path": fw_bin},
                },
            )

        # Fail now if the board isn't on the bus, rather than after stopping
        # klipper. Katapult means it's already in the bootloader.
        from ..devices import find_device

        if find_device(self.paths, mcu.chipset, serial) is None:
            raise RpcError(
                f"{serial} is not attached (looked for chipset {mcu.chipset}). "
                f"Is it plugged in and powered?",
                data={
                    "code": "device_not_found",
                    "message": "board is not on the bus",
                    "data": {"serial": serial, "chipset": mcu.chipset},
                },
            )

        # Last gate. Covers a running print *and* any other klipper activity -
        # homing, QGL, a macro - because stopping klipper mid-motion is just as
        # destructive as interrupting a print, and print_stats alone misses it.
        from ..service import assert_printer_idle

        assert_printer_idle(
            settings, activity=self._printer_activity, force=force, reporter=self._log_reporter
        )

        def run(ctx) -> dict[str, Any]:
            from ..devices import KLIPPER_FW_NAME, wait_for_device
            from ..errors import BootloaderTimeoutError
            from ..flash import flash_katapult
            from ..service import klipper_stopped, make_controller

            settings_now = self.settings()
            svc = make_controller(settings_now, call=self._call_for_service)
            ctx.step(f"Stopping {svc.name}", 0, 4)
            with klipper_stopped(self.paths, svc, f"flash {serial}", reporter=ctx.reporter):
                ctx.step(f"Flashing {serial}", 1, 4)
                # No cancel is threaded into the write on purpose - interrupting
                # flashtool leaves half an image on the board.
                flash_katapult(
                    self.paths,
                    settings_now,
                    mcu_type,
                    mcu.chipset,
                    serial,
                    fw_bin=fw_bin,
                    reporter=ctx.reporter,
                )

                # The board reboots into the new firmware and re-enumerates over
                # USB, which takes a couple of seconds. Starting klipper before
                # the device node exists means klipper cannot find its MCU and
                # comes up in an error state.
                ctx.step(f"Waiting for {serial} to come back", 2, 4)
                if not settings_now.dry_run:
                    try:
                        wait_for_device(
                            self.paths,
                            mcu.chipset,
                            serial,
                            KLIPPER_FW_NAME,
                            timeout=REENUMERATE_TIMEOUT,
                            settle=1.0,
                        )
                        ctx.reporter("info", f"{serial} is back as a Klipper device.")
                    except BootloaderTimeoutError as exc:
                        # Not fatal here: klipper still has to be started, and it
                        # may yet find the board. The readiness check below is the
                        # real verdict.
                        ctx.reporter("warn", str(exc))

                ctx.step(f"Restarting {svc.name}", 3, 4)

            # klipper_stopped has started the service by now. Being *active* is
            # not the same as being ready, so confirm - and firmware-restart if
            # the MCU came back shut down.
            klippy_state = self._await_klippy_ready(ctx.reporter)
            ctx.step("Done", 4, 4)
            return {
                "type": mcu_type,
                "serial": serial,
                "fw_bin": fw_bin,
                "klippy_state": klippy_state,
            }

        job = runner.submit("flash", {"name": mcu_type, "serial": serial}, run)
        return {"job_id": job.id, "job": job.to_dict()}

    def _log_reporter(self, stream: str, line: str) -> None:
        """Send a core Reporter's output to the agent log.

        Used for checks that run outside a job, where there is no job log to
        collect into but the message still matters - e.g. "could not determine
        print state, continuing".
        """
        if self._log is None:
            return
        if stream in ("warn", "error"):
            self._log.warning(line)
        else:
            self._log.debug(line)

    def _printer_activity(self) -> dict[str, Optional[str]]:
        """Both states that mean "don't touch the printer right now".

        print_stats.state only knows about virtual_sdcard print jobs, so it stays
        "standby" during a manual home or QGL. idle_timeout.state is the one that
        reads "Printing" whenever klipper is executing anything.
        """
        res = self._probe(
            "printer.objects.query",
            {"objects": {"print_stats": ["state"], "idle_timeout": ["state"]}},
        )
        status = (res or {}).get("status") or {}
        return {
            "print_state": (status.get("print_stats") or {}).get("state"),
            "idle_state": (status.get("idle_timeout") or {}).get("state"),
        }

    def _print_state(self) -> Optional[str]:
        return self._printer_activity().get("print_state")

    def _klippy_state(self) -> tuple[Optional[str], str]:
        info = self._probe("printer.info")
        if not isinstance(info, dict):
            return None, ""
        return info.get("state"), str(info.get("state_message") or "")

    #: How long to wait for klippy to report "ready" after the service starts,
    #: and again after a firmware restart. Class attributes so tests can shrink
    #: them without patching a call site.
    KLIPPY_READY_TIMEOUT = 45.0
    KLIPPY_RESTART_TIMEOUT = 60.0
    KLIPPY_POLL_INTERVAL = 1.0

    def _await_klippy_ready(
        self,
        reporter: Any,
        *,
        timeout: Optional[float] = None,
        after_restart: Optional[float] = None,
    ) -> Optional[str]:
        """Wait for klipper to actually be usable, restarting firmware if needed.

        `systemctl is-active klipper` going green is **not** the same as klipper
        being ready. A board that was mid-motion when we stopped the service comes
        back with its MCU in a shutdown state, so klippy reaches "error" or
        "shutdown" and the printer needs a FIRMWARE_RESTART before it will move.
        Doing that automatically is exactly what a human does by hand.
        """
        if self._call is None:
            return None
        timeout = self.KLIPPY_READY_TIMEOUT if timeout is None else timeout
        after_restart = self.KLIPPY_RESTART_TIMEOUT if after_restart is None else after_restart

        state = self._poll_klippy(timeout)
        if state == "ready":
            reporter("info", "Klipper is ready.")
            return state

        message = self._klippy_state()[1]
        reporter(
            "warn",
            f"Klipper came up in state '{state}'"
            + (f": {message}" if message else "")
            + " - issuing a firmware restart (the MCU was reset by the flash).",
        )
        try:
            self._call("printer.firmware_restart", None, 30.0)
        except Exception as exc:  # noqa: BLE001
            reporter("error", f"firmware restart failed: {exc}")
            return state

        state = self._poll_klippy(after_restart)
        if state == "ready":
            reporter("info", "Klipper is ready after the firmware restart.")
        else:
            # Deliberately not a job failure: the write itself succeeded, and
            # Mainsail's own Klippy panel will be showing this loudly. But say
            # exactly what to do next.
            reporter(
                "error",
                f"Klipper is still in state '{state}' after a firmware restart. "
                f"Check the Klippy panel, then run FIRMWARE_RESTART from the console.",
            )
        return state

    def _poll_klippy(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state, _ = self._klippy_state()
            if state == "ready":
                return state
            # "startup" just means it hasn't finished connecting yet.
            if state in ("error", "shutdown"):
                return state
            time.sleep(self.KLIPPY_POLL_INTERVAL)
        return state

    def _call_for_service(self, method: str, params: Any) -> Any:
        """Adapter: ServiceController wants (method, params); _call takes a timeout.

        Service calls get a longer budget than status probes - stopping klipper
        genuinely takes a moment, and timing out here would look like a failure
        and abort a flash that was about to be fine.
        """
        if self._call is None:
            raise RpcError("no moonraker connection")
        return self._call(method, params, 30.0)

    def job_get(self, args: dict) -> dict[str, Any]:
        """A job plus a slice of its log.

        `log_from` is how the panel recovers from a gap: batched log events carry
        the sequence of their first line, and any mismatch against what the client
        expected means it asks for the range it missed.
        """
        runner = self._require_runner()
        job_id = args.get("job_id")
        job = runner.get(str(job_id)) if job_id else runner.current()
        if job is None:
            raise RpcError(
                f"no such job: {job_id}" if job_id else "no job is running",
                data={"code": "unknown_job", "message": "job not found", "data": {}},
            )

        raw_from = args.get("log_from")
        try:
            log_from = max(int(raw_from), 0) if raw_from is not None else 0
        except (TypeError, ValueError):
            raise RpcError("'log_from' must be an integer", ERR_INVALID_PARAMS) from None

        lines, served_from, log_next = job.log_since(log_from)
        return {
            "job": job.to_dict(),
            "log": [line.to_dict() for line in lines],
            # May exceed log_from when the ring buffer already evicted the
            # requested range; the panel shows a "lines omitted" marker.
            "log_from": served_from,
            "log_next": log_next,
            "log_dropped": job.dropped,
        }

    def job_cancel(self, args: dict) -> dict[str, Any]:
        runner = self._require_runner()
        job_id = args.get("job_id")
        if not job_id:
            current = runner.current()
            if current is None:
                raise RpcError("no job is running", ERR_INVALID_PARAMS)
            job_id = current.id
        return runner.cancel(str(job_id))

    #: method name -> bound attribute name. Registered with Moonraker verbatim,
    #: and dotted names are fine (Moonraker's own example is
    #: "moontest.hello_world").
    METHODS: dict[str, str] = {
        "fw.ping": "ping",
        "fw.status": "status",
        "fw.type.list": "type_list",
        "fw.bus.scan": "bus_scan",
        "fw.dfu.scan": "dfu_scan",
        "fw.artifacts": "artifacts",
        "fw.settings.get": "settings_get",
        "fw.settings.set": "settings_set",
        "fw.build": "build",
        "fw.flash": "flash",
        "fw.job.get": "job_get",
        "fw.job.cancel": "job_cancel",
        "fw.build_all": "build_all",
        "fw.flash_all": "flash_all",
        "fw.update_all": "update_all",
        "fw.serial.add": "serial_add",
        "fw.serial.remove": "serial_remove",
        "fw.type.add": "type_add",
        "fw.type.update": "type_update",
        "fw.type.remove": "type_remove",
        "fw.kconfig.open": "kconfig_open",
        "fw.kconfig.menu": "kconfig_menu",
        "fw.kconfig.enter": "kconfig_enter",
        "fw.kconfig.up": "kconfig_up",
        "fw.kconfig.set": "kconfig_set",
        "fw.kconfig.help": "kconfig_help",
        "fw.kconfig.search": "kconfig_search",
        "fw.kconfig.reset": "kconfig_reset",
        "fw.kconfig.save": "kconfig_save",
        "fw.kconfig.close": "kconfig_close",
    }

    #: Registered with Moonraker only when a runner is present, so a read-only
    #: deployment doesn't advertise controls it cannot honour.
    JOB_METHODS = (
        "fw.build",
        "fw.flash",
        "fw.job.get",
        "fw.job.cancel",
        "fw.build_all",
        "fw.flash_all",
        "fw.update_all",
    )

    #: Advertised only when enable_flashing is on. The panel hides its flash
    #: buttons accordingly, rather than offering something that gets refused.
    FLASH_METHODS = ("fw.flash", "fw.flash_all", "fw.update_all")

    def available_methods(self) -> dict[str, str]:
        out = dict(self.METHODS)
        if self.runner is None:
            for name in self.JOB_METHODS:
                out.pop(name, None)
            return out
        if not self.settings().enable_flashing:
            for name in self.FLASH_METHODS:
                out.pop(name, None)
        return out




    # -- DFU: what is waiting to be adopted ---------------------------------

    #: Why a DFU flash cannot start right now. Stable codes; the panel switches on
    #: them, and each maps to a different physical thing for the user to do.
    DFU_NO_TOOL = "no_tool"
    DFU_PERMISSION_DENIED = "permission_denied"
    DFU_NONE = "none"
    DFU_AMBIGUOUS = "ambiguous"

    def dfu_scan(self, args: dict) -> dict[str, Any]:
        """What is sitting in DFU mode, and can this agent actually open it?

        Deliberately **reports** failures instead of raising them. Every other
        method treats a refusal as an error because the caller asked for work to
        happen; here, describing the situation *is* the work. "dfu-util is not
        installed" is this method's answer, not its failure.

        The distinctions are not cosmetic - each sends the user somewhere else:

        ``no_tool``
            `apt install dfu-util`. Nothing to do with the board.
        ``permission_denied``
            libusb saw a board and could not claim it. **The boot jumper worked.**
            Reporting this as "no device found" is what once sent a user back to
            redo the one step that had succeeded. The udev rule tags `uaccess`,
            which grants the *seated* user - and this agent is a daemon, not a
            login session - so in practice it rides on `GROUP="plugdev"` and the
            service user being in that group.
        ``none``
            Genuinely nothing in DFU. Fit the boot jumper and replug.
        ``ambiguous``
            More than one board in DFU. dfu-util addresses by VID:PID only, so
            there is no way to say which one is meant - unplug the others.
        """
        from ..flash import DFU_VID_PID, dfu_devices

        out: dict[str, Any] = {
            "vid_pid": DFU_VID_PID,
            "devices": [],
            "count": 0,
            "ready": False,
            "reason": None,
            "message": None,
        }

        try:
            devices = dfu_devices(reporter=self._log_reporter)
        except ToolMissingError as exc:
            out["reason"] = self.DFU_NO_TOOL
            out["message"] = str(exc)
            return out
        except DfuPermissionError as exc:
            out["reason"] = self.DFU_PERMISSION_DENIED
            out["message"] = str(exc)
            # The raw dfu-util output, because a permissions diagnosis is exactly
            # the case where the operator wants to see what the tool actually said.
            # UpdaterError keeps its extras in .data, not as attributes.
            out["output"] = exc.data.get("output")
            return out
        except UpdaterError as exc:
            out["reason"] = exc.code
            out["message"] = str(exc)
            return out

        out["devices"] = devices
        out["count"] = len(devices)
        if not devices:
            out["reason"] = self.DFU_NONE
            out["message"] = (
                "No board is in DFU mode. Fit the boot jumper (or hold BOOT0) and "
                "replug the board."
            )
            return out
        if len(devices) > 1:
            out["reason"] = self.DFU_AMBIGUOUS
            out["message"] = (
                f"{len(devices)} boards are in DFU mode. dfu-util can only address "
                f"them by USB id, so there is no way to say which one is meant - "
                f"unplug all but the target board."
            )
            return out

        out["ready"] = True
        return out

    # -- bulk operations ----------------------------------------------------

    #: Scopes shared by every bulk operation.
    #:
    #: ``stale`` - only what the provenance says needs doing. Correct *because* of
    #:   the flash log: a rebuilt artifact makes its boards stale even when the
    #:   klipper commit has not moved, which is the case a version comparison
    #:   cannot see.
    #: ``all`` - everything in scope regardless. For when you know something the
    #:   provenance cannot, such as having edited an untracked source file.
    SCOPES = ("stale", "all")

    def _scope(self, args: dict) -> str:
        scope = str(args.get("scope") or "stale")
        if scope not in self.SCOPES:
            raise RpcError(
                f"'scope' must be one of {list(self.SCOPES)}", ERR_INVALID_PARAMS
            )
        return scope

    def _types_to_build(self, reg: Registry, fw: str, scope: str) -> list[str]:
        """Which types a build_all should touch, and in a stable order.

        A type with no saved config is skipped rather than failed: menuconfig is
        ncurses and cannot run here, so there is nothing this could do about it, and
        failing the whole batch over one unconfigured type would be worse.
        """
        from ..build import staleness

        out = []
        for name in reg.names():
            if not os.path.exists(self.paths.config_file(name, fw)):
                continue
            if scope == "all":
                out.append(name)
                continue
            stale, _ = staleness(self.paths, name, fw)
            if stale:
                out.append(name)
        return out

    def _boards_to_flash(self, reg: Registry, scope: str, only: Optional[str] = None) -> list[dict]:
        """Which boards a flash_all should write, with the reason for each.

        Offline boards are never included: a flash needs the device on the bus, so
        including them would only produce a guaranteed failure partway through a
        batch that has already stopped Klipper.
        """
        from ..build import FlashLog, git_head, read_sidecar

        versions = self.mcu_info()
        fw_head = git_head(self.paths.fw_dir("klipper"))
        flashlog = FlashLog(self.paths)

        out: list[dict] = []
        for name in reg.names():
            if only is not None and name != only:
                continue
            mcu = reg.get(name)
            if not os.path.exists(self.paths.bin_file(name, "klipper")):
                continue
            artifact_sha = (read_sidecar(self.paths, name, "klipper") or {}).get("bin_sha256")
            for serial in mcu.serials:
                state, _ = device_state(self.paths, mcu.chipset, serial)
                if state == STATE_OFFLINE:
                    continue
                info = self.flash_state(
                    serial,
                    versions,
                    fw_head,
                    state=state,
                    artifact_sha=artifact_sha,
                    flashlog=flashlog,
                )
                if scope == "all" or info["needs_flash"] is True:
                    out.append(
                        {
                            "type": name,
                            "serial": serial,
                            "chipset": mcu.chipset,
                            "state": state,
                            "reason": info["reason"] if scope != "all" else "forced",
                        }
                    )
        return out

    def _do_build_all(self, ctx: Any, fw: str, names: list[str]) -> dict[str, Any]:
        """Build each type in turn, reporting failures rather than stopping.

        Matches what the CLI's update-all has always done. One type failing to
        compile is usually about that type, and abandoning the rest would turn a
        one-board problem into a whole-fleet one.
        """
        from ..build import build as do_build

        built: list[str] = []
        failures: list[dict[str, str]] = []
        total = len(names)
        for index, name in enumerate(names):
            ctx.check_cancelled()
            ctx.step(f"Building {fw} for {name}", index, total)
            try:
                do_build(
                    self.paths,
                    self.registry(),
                    self.settings(),
                    name,
                    fw,
                    reporter=ctx.reporter,
                    cancel=ctx.cancel,
                )
                built.append(name)
            except OperationCancelled:
                raise
            except UpdaterError as exc:
                ctx.reporter("warn", f"{name}: {exc}")
                failures.append({"type": name, "error": str(exc)})
        ctx.step(f"Built {len(built)} of {total}", total, total)
        return {"fw": fw, "built": built, "failures": failures}

    def _do_flash_all(self, ctx: Any, boards: list[dict]) -> dict[str, Any]:
        """Write every selected board, with Klipper stopped once for the batch.

        Once per batch rather than once per board: ten stop/start cycles would take
        far longer and give ten chances for the restart to be the thing that fails.

        Cancellation is honoured *between* boards only. Interrupting a flashtool
        write leaves half an image on a board, so the check is at the top of each
        iteration and never inside one.
        """
        from ..devices import KLIPPER_FW_NAME, wait_for_device
        from ..errors import BootloaderTimeoutError
        from ..flash import flash_katapult
        from ..service import klipper_stopped, make_controller

        settings = self.settings()
        svc = make_controller(settings, call=self._call_for_service)
        flashed: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        total = len(boards)

        with klipper_stopped(self.paths, svc, f"flash {total} board(s)", reporter=ctx.reporter):
            for index, board in enumerate(boards):
                # Between boards, never inside a write.
                ctx.check_cancelled()
                ctx.step(f"Flashing {board['serial']} ({board['type']})", index, total)
                try:
                    flash_katapult(
                        self.paths,
                        settings,
                        board["type"],
                        board["chipset"],
                        board["serial"],
                        reporter=ctx.reporter,
                    )
                    flashed.append({"type": board["type"], "serial": board["serial"]})
                    # Same contract as the single flash: the board reboots into
                    # the new firmware and re-enumerates over USB, and starting
                    # Klipper before its device node exists brings it up in an
                    # error state. Per board rather than once at the end - the
                    # last board of a batch would otherwise have nothing at all
                    # between its write and the service restart.
                    if not settings.dry_run:
                        try:
                            wait_for_device(
                                self.paths,
                                board["chipset"],
                                board["serial"],
                                KLIPPER_FW_NAME,
                                timeout=REENUMERATE_TIMEOUT,
                                settle=1.0,
                            )
                        except BootloaderTimeoutError as exc:
                            # Not fatal, and deliberately not counted as a
                            # failure: the write succeeded, and the readiness
                            # check after the batch is the real verdict.
                            ctx.reporter("warn", str(exc))
                except OperationCancelled:
                    raise
                except UpdaterError as exc:
                    ctx.reporter("warn", f"{board['serial']}: {exc}")
                    failures.append({"serial": board["serial"], "error": str(exc)})
            ctx.step(f"Flashed {len(flashed)} of {total}", total, total)

        # klipper_stopped has started the service again by now; confirm it really
        # came back, which is the release gate for every flashing path.
        ctx.reporter("info", "Waiting for Klipper to be ready...")
        self._await_klippy_ready(ctx.reporter)
        return {"flashed": flashed, "failures": failures}

    def build_all(self, args: dict) -> dict[str, Any]:
        """Build every type that needs it. Touches no board and stops nothing."""
        runner = self._require_runner()
        scope = self._scope(args)
        fw = str(args.get("fw") or "klipper")
        if fw not in FW_TARGETS:
            raise RpcError(f"'fw' must be one of {list(FW_TARGETS)}", ERR_INVALID_PARAMS)

        names = self._types_to_build(self.registry(), fw, scope)
        if not names:
            raise RpcError(
                f"nothing to build: no type has a saved {fw} config that is out of date. "
                f"Use scope 'all' to rebuild regardless.",
                data={
                    "code": "nothing_to_do",
                    "message": "no types need building",
                    "data": {"fw": fw, "scope": scope},
                },
            )

        def run(ctx) -> dict[str, Any]:
            return self._do_build_all(ctx, fw, names)

        job = runner.submit("build_all", {"fw": fw, "scope": scope, "types": names}, run)
        return {"job_id": job.id, "job": job.to_dict(), "types": names}

    def flash_all(self, args: dict) -> dict[str, Any]:
        """Flash every board that needs it, or every board of one type.

        `name` narrows it to a single type - that is `flash_type`, which is the same
        operation with a filter rather than a second implementation of it.
        """
        runner = self._require_runner()
        settings = self.settings()
        if not settings.enable_flashing:
            raise RpcError(
                "flashing from the web UI is disabled. Set 'enable_flashing = true' in "
                f"{self.paths.settings_file} to allow it.",
                data={
                    "code": "flashing_disabled",
                    "message": "enable_flashing is false",
                    "data": {"settings_file": self.paths.settings_file},
                },
            )

        scope = self._scope(args)
        only = args.get("name")
        reg = self.registry()
        if only is not None:
            reg.get(str(only))  # fail fast on a typo, before a job exists
            only = str(only)

        boards = self._boards_to_flash(reg, scope, only)
        if not boards:
            raise RpcError(
                "nothing to flash: every online board already matches its built "
                "firmware. Use scope 'all' to flash regardless.",
                data={
                    "code": "nothing_to_do",
                    "message": "no boards need flashing",
                    "data": {"scope": scope, "name": only},
                },
            )

        # The print gate, once, before a job exists - so the refusal carries a real
        # explanation rather than arriving as a job that dies a second later.
        from ..service import assert_printer_idle

        assert_printer_idle(
            settings,
            activity=self._printer_activity,
            force=bool(args.get("force")),
            reporter=self._log_reporter,
        )

        def run(ctx) -> dict[str, Any]:
            return self._do_flash_all(ctx, boards)

        job = runner.submit(
            "flash_all",
            {"scope": scope, "name": only, "count": len(boards)},
            run,
        )
        return {"job_id": job.id, "job": job.to_dict(), "boards": boards}

    def update_all(self, args: dict) -> dict[str, Any]:
        """Build what is stale, then flash what is behind - one Klipper stop.

        Composed from the same two routines the individual operations use, rather
        than a third implementation of the loop. Its purpose is a klipper update, so
        `stale` here means the source tree moved; the artifact-hash precision
        matters more to flash_type after a patch change.
        """
        runner = self._require_runner()
        settings = self.settings()
        if not settings.enable_flashing:
            raise RpcError(
                "flashing from the web UI is disabled, so update-all cannot run. Set "
                f"'enable_flashing = true' in {self.paths.settings_file} to allow it.",
                data={
                    "code": "flashing_disabled",
                    "message": "enable_flashing is false",
                    "data": {"settings_file": self.paths.settings_file},
                },
            )

        scope = self._scope(args)
        reg = self.registry()
        names = self._types_to_build(reg, "klipper", scope)

        from ..service import assert_printer_idle

        assert_printer_idle(
            settings,
            activity=self._printer_activity,
            force=bool(args.get("force")),
            reporter=self._log_reporter,
        )

        def run(ctx) -> dict[str, Any]:
            build_result = self._do_build_all(ctx, "klipper", names) if names else {
                "fw": "klipper",
                "built": [],
                "failures": [],
            }
            # Selected *after* building, because a build is what makes boards stale:
            # choosing the boards up front would use provenance that the build has
            # just invalidated.
            boards = self._boards_to_flash(self.registry(), scope)
            if not boards:
                ctx.reporter("info", "No board needs flashing.")
                return {"build": build_result, "flash": {"flashed": [], "failures": []}}

            # Gate again. The check before submission was minutes ago - a whole
            # fleet build - and the printer may have started moving since. This
            # is the last moment before Klipper gets stopped.
            assert_printer_idle(
                settings,
                activity=self._printer_activity,
                force=bool(args.get("force")),
                reporter=ctx.reporter,
            )
            return {"build": build_result, "flash": self._do_flash_all(ctx, boards)}

        job = runner.submit("update_all", {"scope": scope, "types": names}, run)
        return {"job_id": job.id, "job": job.to_dict(), "types": names}

    # -- what is actually running on the boards -----------------------------

    def _mcu_object_names(self) -> list[str]:
        """Klipper's printer objects that are MCUs, cached.

        `printer.objects.list` is a separate round trip and the answer only changes
        when Klipper restarts, so it is cached - otherwise fw.status would cost two
        probes instead of one and blow its sub-second budget.
        """
        now = time.time()
        if self._mcu_names is not None and now - self._mcu_names_at < MCU_NAMES_TTL:
            return self._mcu_names
        res = self._probe("printer.objects.list")
        objects = (res or {}).get("objects")
        if not isinstance(objects, list):
            # Leave the cache alone on a failed probe: a stale list beats none, and
            # the next call will try again.
            return self._mcu_names or []
        names = [o for o in objects if o == "mcu" or str(o).startswith("mcu ")]
        self._mcu_names = names
        self._mcu_names_at = now
        return names

    def mcu_info(self) -> dict[str, dict[str, str]]:
        """Tracked serial -> the firmware version that board is actually running.

        This is the thing staleness could not tell you. `staleness()` compares the
        built .bin against the source tree, which answers "do I need to rebuild?".
        It says nothing about what is on the boards, so a board flashed months ago
        and never touched again reported "up to date" as long as nobody had pulled
        klipper since. Two boards of the *same type* can be on different versions,
        which a per-type answer cannot express at all.

        Klipper reports each MCU's `mcu_version` (a git describe, e.g.
        v0.13.0-711-gd7cea5bb) and its configured serial, so the two can be joined.
        """
        names = self._mcu_object_names()
        if not names:
            return {}

        query: dict[str, Any] = {"configfile": ["settings"]}
        for name in names:
            query[name] = ["mcu_version"]
        res = self._probe("printer.objects.query", {"objects": query})
        status = (res or {}).get("status")
        if not isinstance(status, dict):
            return {}

        settings = (status.get("configfile") or {}).get("settings") or {}
        # Klipper lowercases config section names, while the printer object keeps
        # the case from the config file - so `[mcu EBBT0]` is object "mcu EBBT0" and
        # setting "mcu ebbt0". Match on the lowered form.
        serial_by_section = {}
        for section, values in settings.items():
            if not (section == "mcu" or section.startswith("mcu ")):
                continue
            path = (values or {}).get("serial")
            if isinstance(path, str) and path:
                serial_by_section[section.lower()] = _serial_from_path(path)

        out: dict[str, dict[str, str]] = {}
        for name in names:
            version = (status.get(name) or {}).get("mcu_version")
            serial = serial_by_section.get(name.lower())
            if isinstance(version, str) and version and serial:
                # The object name verbatim - "mcu", "mcu EBBT0" - because that is
                # exactly what Mainsail's own System Loads panel shows, and a serial
                # is meaningless until you know which MCU it is.
                out[serial] = {"version": version, "mcu": name}
        return out

    def flash_state(
        self,
        serial: str,
        info: dict[str, dict[str, str]],
        fw_head: Optional[str],
        *,
        state: Optional[str] = None,
        artifact_sha: Optional[str] = None,
        flashlog: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Whether this board wants flashing, and why.

        `needs_flash` is None for "cannot tell" rather than False: an offline board
        or an unreachable Klippy is not evidence that a board is current, and
        claiming otherwise is the bug this whole area exists to fix.

        `reason` is the useful part, because the answers are not equivalent:

        ``in_bootloader``
            Sitting in Katapult, so it reports no klipper version at all. That is
            not "unknown" - a board waiting in its bootloader is the strongest
            possible signal that it wants firmware.
        ``source_changed``
            Running an older klipper commit than the source tree.
        ``artifact_changed``
            Same commit, different binary. This is the one a version comparison
            structurally cannot see: an edited makefile-patch source or a changed
            .config produces a different build from an identical commit, and the
            board cannot tell us which one it holds. Only our own flash record can.
        ``offline`` / ``unknown_version``
            Genuinely no answer.
        """
        entry = info.get(serial) or {}
        version = entry.get("version")
        mcu = entry.get("mcu")
        running = _running_sha(version or "")
        out: dict[str, Any] = {
            "mcu": mcu,
            "running_version": version,
            "running_sha": running,
            "needs_flash": None,
            "reason": None,
        }

        if state == STATE_OFFLINE:
            out["reason"] = "offline"
            return out
        if state == STATE_KATAPULT:
            out["needs_flash"] = True
            out["reason"] = "in_bootloader"
            return out
        if version is None or running is None or not fw_head:
            out["reason"] = "unknown_version"
            return out

        # `-dirty` is normal and must not read as a mismatch: a type with makefile
        # patches is dirty by construction, because the patch is in place while
        # klipper stamps its version.
        if not fw_head.startswith(running):
            out["needs_flash"] = True
            out["reason"] = "source_changed"
            return out

        # The commit matches, so only our own record can distinguish two builds of
        # it. Used to *add* confidence and never to remove it: with no record, the
        # commit match stands rather than degrading every board to unknown.
        if flashlog is not None and artifact_sha:
            record = flashlog.entry_for(serial, running)
            flashed = (record or {}).get("bin_sha256")
            if record is not None and flashed and flashed != artifact_sha:
                out["needs_flash"] = True
                out["reason"] = "artifact_changed"
                return out

        out["needs_flash"] = False
        return out

    # -- kconfig -----------------------------------------------------------

    def _sessions(self) -> Any:
        """The session store, created on first use.

        Lazily, because an agent that never opens a config should not pay for the
        import or hold the state.
        """
        if self._kconfig_sessions is None:
            from ..kconfig import SessionStore

            self._kconfig_sessions = SessionStore(self.paths)
        return self._kconfig_sessions

    def kconfig_available(self) -> dict[str, bool]:
        """Which firmware trees can be configured from here.

        A stat per tree, so it is cheap enough for fw.status. Lets the panel hide
        the button rather than offer one that fails on a host where the source tree
        is missing.
        """
        from ..kconfig import kconfiglib_path

        out = {}
        for fw in FW_TARGETS:
            fw_dir = self.paths.fw_dir(fw)
            out[fw] = os.path.isfile(kconfiglib_path(fw_dir)) and os.path.isfile(
                os.path.join(fw_dir, "src", "Kconfig")
            )
        return out

    def _session(self, args: dict) -> Any:
        return self._sessions().get(self._require_str(args, "session"))

    def kconfig_open(self, args: dict) -> dict[str, Any]:
        """Parse a firmware tree and start a configuration session.

        The one method here that can approach a second: a full Klipper Kconfig
        parse is a few hundred milliseconds on a Pi. Every other kconfig call works
        against the tree this leaves in memory, which is the reason sessions exist
        at all.
        """
        name = self._require_str(args, "name")
        fw = self._require_str(args, "fw")
        if fw not in FW_TARGETS:
            raise RpcError(f"'fw' must be one of {', '.join(FW_TARGETS)}", ERR_INVALID_PARAMS)

        # The type has to exist: the answers are saved per type, and inventing a
        # directory for a typo is not a helpful thing to do.
        self.registry().get(name)

        store = self._sessions()
        if not bool(args.get("force")):
            clash = store.dirty_for(name, fw)
            if clash is not None:
                raise RpcError(
                    f"another session ({clash.id}) has unsaved changes to "
                    f"{name}/{fw}. Opening a second one risks one save discarding "
                    f"the other's work - finish or discard that one first, or pass "
                    f"force to take it over.",
                    data={
                        "code": "kconfig_session_conflict",
                        "message": "another session has unsaved changes",
                        "data": {"session": clash.id, "type": name, "fw": fw},
                    },
                )

        session = store.open(name, fw)
        with session.lock:
            payload = session.menu()
        payload["available"] = self.kconfig_available()
        return payload

    def kconfig_menu(self, args: dict) -> dict[str, Any]:
        """Re-read the current screen, for a client that lost its copy."""
        session = self._session(args)
        with session.lock:
            return session.menu()

    def kconfig_enter(self, args: dict) -> dict[str, Any]:
        session = self._session(args)
        node_id = self._require_str(args, "id")
        with session.lock:
            return session.enter(node_id)

    def kconfig_up(self, args: dict) -> dict[str, Any]:
        session = self._session(args)
        with session.lock:
            return session.up()

    def kconfig_set(self, args: dict) -> dict[str, Any]:
        """Assign one symbol and return the menu it leaves behind."""
        session = self._session(args)
        node_id = self._require_str(args, "id")
        if "value" not in args:
            raise RpcError("'value' is required", ERR_INVALID_PARAMS)
        with session.lock:
            return session.set_value(node_id, str(args.get("value")))

    def kconfig_help(self, args: dict) -> dict[str, Any]:
        session = self._session(args)
        node_id = self._require_str(args, "id")
        with session.lock:
            return session.help(node_id)

    def kconfig_search(self, args: dict) -> dict[str, Any]:
        session = self._session(args)
        with session.lock:
            return session.search(str(args.get("query") or ""))

    def kconfig_reset(self, args: dict) -> dict[str, Any]:
        """Discard unsaved edits by reparsing from disk."""
        session = self._session(args)
        with session.lock:
            return session.reset()

    def kconfig_save(self, args: dict) -> dict[str, Any]:
        """Write the answers, optionally kicking off a build.

        Takes the *build* lock, not the registry one, because this genuinely
        conflicts with a build: `build()` hashes the .config to record what a binary
        was compiled from, so changing it underneath would leave provenance that
        does not match the artifact - and staleness would then report a wrong
        binary as fresh.
        """
        from ..lock import ExclusiveLock

        session = self._session(args)
        want_build = bool(args.get("build"))

        with session.lock:
            lock = ExclusiveLock(self.paths)
            try:
                lock.acquire(f"save config {session.mcu_type}/{session.fw}")
            except UpdaterError:
                raise
            try:
                result = session.save()
            finally:
                lock.release()
            result["menu"] = session.menu()

        self._changed()

        if want_build:
            # Deliberately after the lock is released: build() takes it itself, and
            # holding it across both would deadlock.
            started = self.build({"name": session.mcu_type, "fw": session.fw})
            result["job_id"] = started.get("job_id")
        return result

    def kconfig_close(self, args: dict) -> dict[str, Any]:
        session_id = self._require_str(args, "session")
        return {"session": session_id, "closed": self._sessions().close(session_id)}

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, method: str, params: Any = None) -> Any:
        attr = self.available_methods().get(method)
        if attr is None:
            raise MethodNotFound(method)

        if params is None:
            args: dict = {}
        elif isinstance(params, dict):
            args = params
        elif isinstance(params, list):
            # Moonraker relays whatever the caller passed as "arguments". A
            # non-empty positional list is unusable here, so say so plainly
            # rather than silently ignoring it.
            if params:
                raise RpcError(
                    f"{method} takes named arguments, got a positional list",
                    ERR_INVALID_PARAMS,
                )
            args = {}
        else:
            raise RpcError(
                f"{method} expects an object of arguments, got {type(params).__name__}",
                ERR_INVALID_PARAMS,
            )

        try:
            return getattr(self, attr)(args)
        except UpdaterError as exc:
            # Surface the stable .code so the panel can switch on it instead of
            # parsing English.
            raise RpcError(exc.message, data=exc.to_dict()) from exc
