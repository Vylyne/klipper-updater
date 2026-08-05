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
import time
from typing import Any, Callable, Optional

from .. import API_VERSION, __version__
from ..build import read_sidecar, staleness
from ..config import Registry
from ..devices import BusDevice, device_state, scan
from ..errors import SerialTrackedElsewhereError, UpdaterError
from ..paths import FW_TARGETS, REENUMERATE_TIMEOUT, Paths
from ..settings import Settings, load_settings
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

    def type_status(self, reg: Registry, name: str) -> dict[str, Any]:
        mcu = reg.get(name)
        serials = []
        for serial in mcu.serials:
            state, path = device_state(self.paths, mcu.chipset, serial)
            serials.append({"serial": serial, "state": state, "path": path})

        out: dict[str, Any] = {
            "name": name,
            "chipset": mcu.chipset,
            "serials": serials,
            "artifacts": {fw: self.artifact(name, fw) for fw in FW_TARGETS},
            "katapult_installed": mcu.katapult_installed,
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
        return {
            "types": [self.type_status(reg, n) for n in reg.names()],
            "bus": self.bus(reg),
            "job": current.to_dict() if current else None,
            "recent": [j.to_dict() for j in self.runner.recent(10)] if self.runner else [],
            "locked_by": self.lock_holder(),
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
        reg = self.registry()
        return {"types": [self.type_status(reg, n) for n in reg.names()]}

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
        "fw.artifacts": "artifacts",
        "fw.settings.get": "settings_get",
        "fw.build": "build",
        "fw.flash": "flash",
        "fw.job.get": "job_get",
        "fw.job.cancel": "job_cancel",
        "fw.serial.add": "serial_add",
        "fw.serial.remove": "serial_remove",
        "fw.type.add": "type_add",
        "fw.type.update": "type_update",
        "fw.type.remove": "type_remove",
    }

    #: Registered with Moonraker only when a runner is present, so a read-only
    #: deployment doesn't advertise controls it cannot honour.
    JOB_METHODS = ("fw.build", "fw.flash", "fw.job.get", "fw.job.cancel")

    #: Advertised only when enable_flashing is on. The panel hides its flash
    #: buttons accordingly, rather than offering something that gets refused.
    FLASH_METHODS = ("fw.flash",)

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
