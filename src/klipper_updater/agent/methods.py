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
from ..errors import UpdaterError
from ..paths import FW_TARGETS, Paths
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
    ) -> None:
        self.paths = paths
        # Injected so this class never touches the transport directly, which is
        # what makes it testable without a Moonraker.
        self._call = call
        # None in a read-only deployment; the build methods then report that the
        # capability is absent rather than half-working.
        self.runner = runner
        self._log = logger

    # -- helpers -----------------------------------------------------------

    def settings(self) -> Settings:
        """Re-read every time: the user may have edited updater.conf."""
        try:
            return load_settings(self.paths.settings_file)
        except UpdaterError as exc:
            if self._log is not None:
                self._log.warning(f"updater.conf is invalid, using defaults: {exc}")
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

    def is_printing(self) -> Optional[bool]:
        res = self._probe("printer.objects.query", {"objects": {"print_stats": ["state"]}})
        try:
            state = res["status"]["print_stats"]["state"]
        except (TypeError, KeyError):
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
                "settings_dir": self.paths.settings_dir,
            },
            "now": time.time(),
        }

    def status(self, args: dict) -> dict[str, Any]:
        """One call paints the whole panel."""
        reg = self.registry()
        s = self.settings()
        current = self.runner.current() if self.runner else None
        return {
            "types": [self.type_status(reg, n) for n in reg.names()],
            "bus": self.bus(reg),
            "job": current.to_dict() if current else None,
            "recent": [j.to_dict() for j in self.runner.recent(10)] if self.runner else [],
            "locked_by": self.lock_holder(),
            "klipper_service": self.klipper_service_state(),
            "printing": self.is_printing(),
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
        reg = self.registry()
        devices = self.bus(reg)
        if args.get("only_untracked"):
            devices = [d for d in devices if d["tracked_by"] is None]
        chipset = args.get("chipset")
        if chipset:
            devices = [d for d in devices if d["chipset"] == chipset]
        return {"devices": devices}

    def artifacts(self, args: dict) -> dict[str, Any]:
        name = args.get("name")
        if not name:
            raise RpcError("'name' is required", ERR_INVALID_PARAMS)
        reg = self.registry()
        reg.get(str(name))  # raises UnknownTypeError for an unknown type
        return {fw: self.artifact(str(name), fw) for fw in FW_TARGETS}

    def settings_get(self, args: dict) -> dict[str, Any]:
        return {"settings": dataclasses.asdict(self.settings())}

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
        "fw.job.get": "job_get",
        "fw.job.cancel": "job_cancel",
    }

    #: Registered with Moonraker only when a runner is present, so a read-only
    #: deployment doesn't advertise controls it cannot honour.
    JOB_METHODS = ("fw.build", "fw.job.get", "fw.job.cancel")

    def available_methods(self) -> dict[str, str]:
        if self.runner is not None:
            return dict(self.METHODS)
        return {k: v for k, v in self.METHODS.items() if k not in self.JOB_METHODS}

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
