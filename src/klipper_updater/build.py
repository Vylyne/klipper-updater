"""Building firmware, with output streamed line by line.

The original invoked ``subprocess.run`` and let the child inherit the parent's
stdout. That works fine for a terminal but gives a caller no way to capture,
forward, or cancel the output - which a daemon streaming a build log to a
browser needs. Everything here reports through a callback instead.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable, Optional

from .config import McuType, Registry
from .errors import (
    BuildError,
    ConfigNotFoundError,
    OperationCancelled,
    SourceTreeMissingError,
    ToolMissingError,
    TtyRequiredError,
)
from .paths import Paths
from .settings import Settings

#: (stream, line). stream is one of:
#:   "cmd"    a command about to run
#:   "stdout" a line of child output (stderr is merged in)
#:   "info"   progress narration from us
#:   "warn"   non-fatal problem
#:   "error"  fatal problem, about to raise
Reporter = Callable[[str, str], None]

_POSIX = sys.platform != "win32"
_SENTINEL = object()

#: Pacing for the dry-run fake build log. Non-zero on purpose: replaying at a
#: realistic speed is what exercises log streaming, batching, sequence numbering
#: and autoscroll. Tests set this to 0.
FAKE_BUILD_DELAY = 0.05


def null_reporter(stream: str, line: str) -> None:
    """Discards output. Useful in tests and for read-only queries."""


# --------------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------------


def _terminate(proc: subprocess.Popen, grace: float, reporter: Reporter) -> None:
    """Kill the whole process tree.

    Terminating only `make` leaves its arm-none-eabi-gcc children running and
    holding the build directory, so cancel has to hit the process group. This is
    why the child is started with start_new_session=True.
    """
    reporter("warn", "cancel requested - terminating build")
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass


_FAKE_BUILD_LINES = [
    "  Creating symbolic link out/board",
    "  Building out/autoconf.h",
    "  Compiling out/src/sched.o",
    "  Compiling out/src/command.o",
    "  Compiling out/src/basecmd.o",
    "  Compiling out/src/gpiocmds.o",
    "  Compiling out/src/stepper.o",
    "  Compiling out/src/endstop.o",
    "  Compiling out/src/trsync.o",
    "  Compiling out/src/adccmds.o",
    "  Compiling out/src/spicmds.o",
    "  Compiling out/src/i2ccmds.o",
    "  Compiling out/src/pwmcmds.o",
    "  Compiling out/src/buttons.o",
    "  Compiling out/src/tmcuart.o",
    "  Compiling out/src/neopixel.o",
    "  Compiling out/src/generic/crc16_ccitt.o",
    "  Compiling out/src/generic/armcm_boot.o",
    "  Compiling out/src/generic/armcm_irq.o",
    "  Compiling out/src/generic/timer_irq.o",
    "  Building out/compile_time_request.o",
    "Version: v0.13.0-dry-run",
    "  Preprocessing out/src/generic/armcm_link.ld",
    "  Linking out/klipper.elf",
    "  Creating bin file out/klipper.bin",
]


def _emit_fake_build_log(reporter: Reporter, delay: float, cancel: Optional[threading.Event]) -> None:
    """Replay a plausible build log at a realistic pace.

    Not cosmetic: this is what exercises log streaming, batching, sequence
    numbering, autoscroll and cancel end-to-end with no toolchain and no risk.
    """
    for i in range(8):
        for line in _FAKE_BUILD_LINES:
            if cancel is not None and cancel.is_set():
                raise OperationCancelled("dry-run build cancelled")
            reporter("stdout", line if i == 0 else f"{line}  [pass {i + 1}]")
            if delay:
                time.sleep(delay)


def run_streamed(
    cmd: list[str],
    *,
    cwd: str,
    reporter: Reporter,
    cancel: Optional[threading.Event] = None,
    env: Optional[dict[str, str]] = None,
    dry_run: bool = False,
    grace: float = 5.0,
    poll: float = 0.25,
    fake_delay: Optional[float] = None,
) -> int:
    """Run a command, forwarding each output line to `reporter` as it arrives.

    Returns the exit code. Raises OperationCancelled if `cancel` was set.

    stderr is merged into stdout deliberately: splitting them reorders the log
    relative to the compile lines and makes a build failure much harder to read.

    A reader thread feeds a queue so the cancel check runs on a timer rather than
    only when a line arrives - otherwise a child that goes quiet couldn't be
    cancelled at all.
    """
    reporter("cmd", " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        delay = FAKE_BUILD_DELAY if fake_delay is None else fake_delay
        _emit_fake_build_log(reporter, delay, cancel)
        return 0

    full_env = dict(os.environ)
    # TERM=dumb plus a non-tty stdout suppresses colour escapes in the log.
    full_env.update({"TERM": "dumb", "LC_ALL": "C"})
    if env:
        full_env.update(env)

    extra: dict[str, Any] = {"start_new_session": True} if _POSIX else {}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            env=full_env,
            **extra,
        )
    except FileNotFoundError as exc:
        # A host without build-essential, or without dfu-util. Report it as a
        # missing tool rather than letting a raw traceback out.
        raise ToolMissingError(
            f"'{cmd[0]}' was not found. Is it installed and on PATH?", tool=cmd[0]
        ) from exc
    except OSError as exc:
        raise BuildError(f"could not run '{cmd[0]}': {exc}", tool=cmd[0]) from exc

    lines: queue.Queue = queue.Queue()

    def _pump() -> None:
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    lines.put(raw.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            lines.put(_SENTINEL)

    pump = threading.Thread(target=_pump, name="run_streamed", daemon=True)
    pump.start()

    cancelled = False
    while True:
        try:
            item = lines.get(timeout=poll)
        except queue.Empty:
            if cancel is not None and cancel.is_set() and not cancelled:
                _terminate(proc, grace, reporter)
                cancelled = True
            continue
        if item is _SENTINEL:
            break
        reporter("stdout", item)
        if cancel is not None and cancel.is_set() and not cancelled:
            _terminate(proc, grace, reporter)
            cancelled = True

    rc = proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()
    pump.join(timeout=2.0)

    if cancelled:
        raise OperationCancelled("build cancelled")
    return rc


# --------------------------------------------------------------------------
# makefile patching
# --------------------------------------------------------------------------


@contextlib.contextmanager
def makefile_patches(
    paths: Paths, mcu: McuType, fw: str, reporter: Reporter, *, dry_run: bool = False
) -> Iterator[None]:
    """Temporarily append configured lines to source-tree Makefiles.

    Restores the original file *bytes* afterwards, even if the build raises.
    Deliberately not a permanent edit: a permanent line gated on e.g.
    CONFIG_MACH_STM32F072 leaks into every other MCU type sharing that chipset,
    and a tracked file like src/stm32/Makefile conflicts on the next git pull of
    klipper anyway.

    Note the side effect on version stamping: while a patch is applied the
    source tree is dirty, so klipper's build stamps the firmware version with a
    `-dirty` suffix. That is expected and not a sign of local modifications.
    """
    fw_dir = paths.fw_dir(fw)
    patches = [p for p in mcu.fw(fw).makefile_patches if p.is_valid()]
    backups: list[tuple[str, Optional[bytes]]] = []
    try:
        for patch in patches:
            target = os.path.join(fw_dir, patch.file)
            line = patch.line
            if not os.path.exists(target):
                reporter("warn", f"patch target {target} not found, skipping '{line}'")
                continue
            if dry_run:
                reporter("info", f"[dry-run] would patch {target}: add '{line}'")
                continue
            with open(target, "rb") as fh:
                original = fh.read()
            if line.encode() in original:
                # Left over from an interrupted run. Deliberately not reverted:
                # we don't know whether it was ours, and removing a line the
                # user put there by hand would be worse than leaving it.
                reporter(
                    "warn",
                    f"'{line}' already present in {target} (left over from an "
                    f"interrupted run?) - leaving it alone, not reverting it.",
                )
                backups.append((target, None))
                continue
            backups.append((target, original))
            with open(target, "ab") as fh:
                if not original.endswith(b"\n"):
                    fh.write(b"\n")
                fh.write((line + "\n").encode())
            reporter("info", f"Temporarily patched {target}: added '{line}'")
        yield
    finally:
        for target, saved in backups:
            if saved is None:
                continue
            try:
                with open(target, "wb") as fh:
                    fh.write(saved)
                reporter("info", f"Restored {target} to its original contents")
            except OSError as exc:
                # Loud, because a half-restored klipper tree affects every
                # subsequent build, not just this one.
                reporter("error", f"FAILED to restore {target}: {exc}")


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def git_head(directory: str) -> Optional[str]:
    """Short HEAD sha of a source tree, or None if it isn't a git checkout."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@dataclasses.dataclass
class BuildResult:
    bin_path: str
    uf2_path: Optional[str]
    duration: float
    fw_sha: Optional[str]
    config_sha256: Optional[str]
    #: True if `make` rewrote our .config (klipper runs olddefconfig when
    #: src/Kconfig is newer than the config, e.g. right after a git pull).
    config_rewritten: bool = False

    def to_sidecar(self) -> dict[str, Any]:
        return {
            "fw_sha": self.fw_sha,
            "config_sha256": self.config_sha256,
            "duration": round(self.duration, 2),
            "timestamp": time.time(),
            "config_rewritten": self.config_rewritten,
        }


def read_sidecar(paths: Paths, mcu_type: str, fw: str) -> Optional[dict[str, Any]]:
    try:
        with open(paths.sidecar_file(mcu_type, fw), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def staleness(paths: Paths, mcu_type: str, fw: str) -> tuple[bool, Optional[str]]:
    """(stale, reason) for a type's built artifact.

    reason is one of None, "never_built", "config_changed", "source_changed".
    Compares recorded provenance rather than mtimes, so a `touch` doesn't lie
    and a git pull of klipper is correctly reported as making every board stale.
    """
    if not os.path.exists(paths.bin_file(mcu_type, fw)):
        return True, "never_built"
    side = read_sidecar(paths, mcu_type, fw)
    if side is None:
        return True, "never_built"

    cfg_hash = _sha256_file(paths.config_file(mcu_type, fw))
    if cfg_hash and side.get("config_sha256") and cfg_hash != side["config_sha256"]:
        return True, "config_changed"

    head = git_head(paths.fw_dir(fw))
    if head and side.get("fw_sha") and head != side["fw_sha"]:
        return True, "source_changed"

    return False, None


# --------------------------------------------------------------------------
# menuconfig / build
# --------------------------------------------------------------------------


def menuconfig_tty(paths: Paths, mcu_type: str, fw: str, *, pause: bool = True) -> None:
    """Run `make menuconfig` against this type's saved .config.

    Requires a real terminal - ncurses cannot be driven over a socket. This
    guard is the hard barrier between the interactive path and the daemon; the
    agent must never reach here.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise TtyRequiredError(
            "menuconfig needs an interactive terminal (it runs an ncurses UI). "
            "Run it over SSH, not from a service or a pipe.",
            type=mcu_type,
            fw=fw,
        )

    fw_dir = paths.fw_dir(fw)
    if not os.path.isdir(fw_dir):
        raise SourceTreeMissingError(
            f"source directory {fw_dir} not found.", fw=fw, path=fw_dir
        )

    config_file = paths.config_file(mcu_type, fw)
    os.makedirs(os.path.dirname(config_file), exist_ok=True)

    print(f"Making config for {mcu_type} with {fw}")
    if pause:
        input("Press Enter to continue to menuconfig...")

    subprocess.run(
        ["make", f"KCONFIG_CONFIG={config_file}", "menuconfig"],
        cwd=fw_dir,
        stdin=sys.stdin,
        stdout=sys.stdout,
    )


def build(
    paths: Paths,
    registry: Registry,
    settings: Settings,
    mcu_type: str,
    fw: str,
    *,
    reporter: Reporter = null_reporter,
    cancel: Optional[threading.Event] = None,
    jobs: Optional[int] = None,
    clean: Optional[bool] = None,
) -> BuildResult:
    """Compile one type/firmware pair and stage the artifacts under ~/mcus.

    Raises rather than returning None (the original returned None so update_all
    could continue; that decision now belongs to the caller, which is what makes
    this safe to call from a daemon). The auto-launch-menuconfig-if-unconfigured
    behaviour also moved to the CLI - see cli.py.
    """
    mcu = registry.get(mcu_type)
    fw_dir = paths.fw_dir(fw)
    if not os.path.isdir(fw_dir):
        raise SourceTreeMissingError(
            f"source directory {fw_dir} not found - is {fw} installed?", fw=fw, path=fw_dir
        )

    config_file = paths.config_file(mcu_type, fw)
    if not os.path.exists(config_file):
        raise ConfigNotFoundError(
            f"no saved config for {mcu_type} ({fw}) at {config_file}. "
            f"Run 'menuconfig -t {mcu_type} -f {fw}' once first.",
            type=mcu_type,
            fw=fw,
            path=config_file,
        )

    dry_run = settings.dry_run
    extra_args = shlex.split(mcu.fw(fw).extra_args or "")
    if extra_args:
        reporter("info", f"Extra make args: {extra_args}")

    do_clean = settings.clean_before_build if clean is None else clean
    if jobs is None:
        make_flags = settings.make_flags()
    else:
        make_flags = [f"-j{jobs}"] if jobs > 0 else []

    kconfig_arg = f"KCONFIG_CONFIG={config_file}"
    config_before = _sha256_file(config_file)
    started = time.monotonic()

    reporter("info", f"Building {fw} for {mcu_type}...")
    with makefile_patches(paths, mcu, fw, reporter, dry_run=dry_run):
        if do_clean:
            run_streamed(
                ["make", kconfig_arg, "clean"],
                cwd=fw_dir,
                reporter=reporter,
                cancel=cancel,
                dry_run=dry_run,
                fake_delay=0.0,
            )
        rc = run_streamed(
            ["make", kconfig_arg, *make_flags, *extra_args],
            cwd=fw_dir,
            reporter=reporter,
            cancel=cancel,
            dry_run=dry_run,
        )

    duration = time.monotonic() - started

    if rc != 0:
        raise BuildError(
            f"firmware build failed for {mcu_type} ({fw}): make exited {rc}.",
            type=mcu_type,
            fw=fw,
            returncode=rc,
        )

    config_after = _sha256_file(config_file)
    rewritten = bool(config_before and config_after and config_before != config_after)
    if rewritten:
        # Klipper's Makefile reruns olddefconfig when src/Kconfig is newer than
        # the .config, which silently changes saved answers. Pre-existing
        # behaviour, but invisible until now.
        reporter(
            "warn",
            "make rewrote the saved .config (klipper ran olddefconfig, most likely "
            "because src/Kconfig changed in a git pull). Review your settings.",
        )

    os.makedirs(paths.type_dir(mcu_type), exist_ok=True)
    bin_out = paths.bin_file(mcu_type, fw)
    compiled = paths.built_artifact(fw, "bin")

    if dry_run:
        # A real (if inert) file, so artifact/staleness logic downstream is
        # exercised for real instead of being special-cased.
        with open(bin_out, "wb") as fh:
            fh.write(b"\0" * 1024)
        reporter("info", f"[dry-run] wrote stub firmware to {bin_out}")
    else:
        if not os.path.exists(compiled):
            raise BuildError(
                f"make succeeded but {compiled} was not produced.",
                type=mcu_type,
                fw=fw,
                expected=compiled,
            )
        shutil.copyfile(compiled, bin_out)
        reporter("info", f"Firmware built and copied to {bin_out}")

    # RP2040 BOOTSEL mass storage only accepts .uf2 - a .bin copied to the mount
    # is accepted and silently ignored - so stage it whenever the build made one.
    uf2_out: Optional[str] = None
    compiled_uf2 = paths.built_artifact(fw, "uf2")
    if not dry_run and os.path.exists(compiled_uf2):
        uf2_out = paths.uf2_file(mcu_type, fw)
        shutil.copyfile(compiled_uf2, uf2_out)
        reporter("info", f"Also staged {uf2_out}")

    result = BuildResult(
        bin_path=bin_out,
        uf2_path=uf2_out,
        duration=duration,
        fw_sha=git_head(fw_dir),
        config_sha256=config_after,
        config_rewritten=rewritten,
    )
    try:
        with open(paths.sidecar_file(mcu_type, fw), "w", encoding="utf-8") as fh:
            json.dump(result.to_sidecar(), fh, indent=2)
    except OSError as exc:
        reporter("warn", f"could not write build sidecar: {exc}")

    return result
