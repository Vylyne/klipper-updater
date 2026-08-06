# Agent API

The contract between `mcu-updater` (Python) and the Mainsail panel
(TypeScript). Both sides are hand-written, so **this file is the single source of
truth** — and `tests/test_agent_methods.py` is what stops them drifting.

- Agent name: `klipper_updater` — a protocol identifier, deliberately unchanged
  when the project was renamed to `mcu-updater`; the panel matches on it.
- `api_version`: **1**
- Every planned capability has shipped: build, flash, bulk build/flash/update,
  registry and settings editing, Kconfig in the browser, and DFU setup of a new
  board. The flashing ones stay behind `enable_flashing`, off by default.

**Gate controls on `capabilities` from `fw.ping`, not on `phase`.** An agent
without a job runner does not register or advertise the job methods at all, so a
newer panel talking to an older agent must hide its build buttons rather than
offer something that returns `-32601`.

## Transport

The agent connects to `~/printer_data/comms/moonraker.sock` and identifies itself:

```json
{"jsonrpc": "2.0", "id": 1, "method": "server.connection.identify",
 "params": {"client_name": "klipper_updater", "version": "0.9.0",
            "type": "agent", "url": "https://github.com/Vylyne/mcu-updater"}}
```

All four params are required. No `api_key`/`access_token` — unix socket
connections are pre-authenticated. The reply is `{"connection_id": <int>}`.

Each message on the socket is UTF-8 JSON **terminated by an ETX byte (`0x03`)**,
not a newline. One read may contain several messages; one message may span
several reads.

After identifying, the agent registers every method name with
`connection.register_remote_method`. **Registrations are per-connection** and are
dropped when the socket closes, so they are re-sent on every reconnect.

### Calling a method

From a front end, over Mainsail's existing websocket:

```ts
this.$socket.emit('server.extensions.request', {
    agent: 'klipper_updater',
    method: 'fw.status',
    arguments: {},
})
```

Or over HTTP: `POST /server/extensions/request` with the same body.

> ⚠️ **Moonraker's `call_method_with_response` has no timeout.** If the agent
> fails to answer, the caller's HTTP request never completes. The agent
> guarantees exactly one response per request, and every method returns in well
> under a second. Clients should still arm their own timeout (the panel uses 15s)
> so a wedged agent can't leave a spinner running forever.

### Errors

```json
{"jsonrpc": "2.0", "id": 9, "error": {
    "code": -32000,
    "message": "MCU type 'nope' does not exist.",
    "data": {"code": "unknown_type", "message": "...", "data": {"known": ["bttebb36"]}}}}
```

Switch on `error.data.code`, never on the message text. Those codes are stable
API; the prose is not. Codes come from `errors.py`: `config_corrupt`,
`unknown_type`, `unknown_serial`, `ambiguous_serial`, `serial_tracked_elsewhere`,
`no_saved_config`, `source_missing`, `build_failed`, `flash_failed`,
`device_not_found`, `bootloader_timeout`, `ambiguous_dfu`, `tool_missing`,
`unsupported_chipset`, `busy`, `print_in_progress`, `cancelled`, `tty_required`,
`invalid_type_name`, `dfu_permission_denied`, `kconfig`.

JSON-RPC codes: `-32601` unknown method, `-32602` bad params, `-32000`
application error (see `data.code`), `-32603` internal.

## Methods

| Method | Arguments | Returns |
| --- | --- | --- |
| `fw.ping` | — | version/capability handshake |
| `fw.status` | — | everything the panel needs, in one call |
| `fw.type.list` | — | `{types: [TypeStatus]}` |
| `fw.bus.scan` | `only_untracked?`, `chipset?` | `{devices: [BusDevice]}` |
| `fw.dfu.scan` | — | `{devices, count, ready, reason, message}` — read-only |
| `fw.add_mcu.start` | `name`, `dfu_serial?` | `{job_id, job, dfu_serial}` — **off by default** |
| `fw.artifacts` | `name` (required) | `{klipper: Artifact, katapult: Artifact}` |
| `fw.settings.get` | — | `{settings: Settings}` |
| `fw.build` | `name`, `fw`, `jobs?`, `clean?` | `{job_id, job}` — returns immediately |
| `fw.flash` | `serial`, `name?`, `force?` | `{job_id, job}` — **off by default**, see below |
| `fw.build_all` | `fw?`, `scope?` | `{job_id, job, types}` — builds only, touches no board |
| `fw.flash_all` | `scope?`, `name?`, `force?` | `{job_id, job, boards}` — **off by default** |
| `fw.update_all` | `scope?`, `name?`, `force?` | `{job_id, job, types}` — **off by default** |
| `fw.display.list` | — | `{displays, reachable}` — read-only |
| `fw.display.build` | `name` | `{job_id, job}` — PlatformIO, touches no screen |
| `fw.display.flash` | `name`, `port?`, `force?` | `{job_id, job, displays}` — **off by default** |
| `fw.job.get` | `job_id?`, `log_from?` | `{job, log, log_from, log_next, log_dropped}` |
| `fw.job.cancel` | `job_id?` | `{cancelling, immediate}` |

### `fw.ping`

```json
{"api_version": 1, "version": "0.9.0", "dry_run": false, "enable_flashing": false,
 "phase": 1, "capabilities": ["fw.artifacts", "fw.bus.scan", "..."],
 "host": {"nproc": 4, "python": "3.9.2",
          "config_dir": "/home/biqu/printer_data/config/mcu-updater",
          "data_dir": "/home/biqu/printer_data/mcu-updater"},
 "now": 1785412345.6}
```

The panel should refuse to render if `api_version` exceeds what it knows, and use
`capabilities` to decide which controls to show — that is how a Phase-1 agent and
a Phase-3 panel coexist without either lying to the user.

### `fw.status`

```json
{"types": [TypeStatus], "bus": [BusDevice],
 "job": null, "recent": [],
 "locked_by": null,
 "klipper_service": "active",
 "printing": false,
 "settings": {...},
 "read_only": true}
```

`job` and `recent` are always `null`/`[]` in Phase 1; the keys exist now so the
shape doesn't change when jobs arrive. `klipper_service` and `printing` are
**best-effort** — they come from querying Moonraker, and are `null` when it can't
be reached. Never treat them as load-bearing.

`locked_by` is non-null when a CLI build or flash is running on the host:
`{"pid": 1234, "label": "build klipper/bttebb36", "since": 1785412000.0}`.

### `TypeStatus`

```json
{"name": "bttebb36",
 "chipset": "stm32g0b1xx",
 "katapult_installed": true,
 "klipper":  {"extra_args": "", "makefile_patches": []},
 "katapult": {"extra_args": "", "makefile_patches": [], "installed": true},
 "serials": [
   {"serial": "290055001850304158373620-if00", "state": "klipper",
    "path": "/dev/serial/by-id/usb-Klipper_stm32g0b1xx_290055001850304158373620-if00"},
   {"serial": "230048001750304158373620-if00", "state": "offline", "path": null}],
 "artifacts": {"klipper": Artifact, "katapult": Artifact}}
```

`state` ∈ `"klipper"` | `"katapult"` | `"offline"`. Case in the firmware name is
not dependable on the bus, so matching is case-insensitive and `path` is the real
on-disk path, never a reconstructed one.

### `Artifact`

```json
{"has_config": true, "config_mtime": 1785400000.0,
 "has_bin": true, "bin_mtime": 1785410000.0, "bin_size": 43120,
 "has_uf2": false,
 "built_fw_sha": "a1b2c3d", "current_fw_sha": "e4f5a6b",
 "stale": true, "stale_reason": "source_changed",
 "last_build_seconds": 74.2, "last_build_at": 1785410000.0,
 "config_rewritten": false}
```

`stale_reason` ∈ `null` | `"never_built"` | `"config_changed"` |
`"source_changed"`.

**This is the field the whole panel exists for**: it answers "do I need to
reflash after that Klipper update?" at a glance. It compares recorded provenance
— the source-tree commit and a hash of the `.config` actually used — not
timestamps, so a `touch` doesn't lie and a `git pull` of Klipper correctly marks
every board stale.

`config_rewritten` is true when `make` ran `olddefconfig` over the saved config,
which silently changes menuconfig answers after Klipper's `src/Kconfig` changes.
Worth surfacing; users otherwise get "why did my CAN setting move?".

### `BusDevice`

```json
{"fw": "Klipper", "chipset": "stm32g0b1xx", "serial": "1100...-if00",
 "path": "/dev/serial/by-id/usb-Klipper_...", "state": "klipper",
 "tracked_by": "bttebb36"}
```

`tracked_by` is `null` for a device on the bus that no MCU type claims — that's
the "new board, want to track it?" case.

## Jobs

Builds and flashes are long, so they never block an RPC call. `fw.build` returns
a `job_id` immediately and progress arrives as events.

**One job at a time, and it is not a queue.** Submitting while something runs
fails with `busy`, carrying the incumbent in `error.data.current`. A queue would
be worse than a refusal: the user asks for one thing, walks away, and returns to
find a second operation they'd forgotten about had started itself.

The same exclusive file lock guards the CLI, and it is taken *before* the job is
created — so if someone is mid-`updatefw build` over SSH, `fw.build` fails
immediately with `busy` and names them, rather than producing a job that dies a
moment later.

```jsonc
// Job (never includes the log - that travels separately and is large)
{
  "id": "job-7",
  "kind": "build",
  "params": {"name": "bttebb36", "fw": "klipper"},
  "state": "running",          // queued|running|succeeded|failed|cancelled
  "created": 1785412000.0, "started": 1785412000.1, "finished": null,
  "duration": 12.4,            // live while running
  "progress": {"step": "Building klipper for bttebb36", "index": 0, "total": 1},
  "result": null,              // set on success
  "error": null,               // {code, message, data} on failure
  "cancel_requested": false,
  "log_next": 431,             // next sequence number that will be assigned
  "log_dropped": 0             // lines evicted by the ring buffer
}
```

### Cancellation is not uniform

`fw.job.cancel` returns `immediate`, and the panel must say which it got:

| kind | behaviour |
| --- | --- |
| `build`, `build_all` | **Immediate.** The whole `make` process group is killed. Worst case is a half-written object file that `make` will redo. |
| `flash`, `flash_all` | **Deferred** — honoured only *between* devices. Interrupting a `flashtool -f` write leaves a board with half an image. Show "cancelling after the current board finishes…". |
| `update_all` | **Deferred**, because it may reach the flashing half. Cancelling during its build phase still waits for the current type's `make` to be killed and the loop to come round. |

### `fw.flash` — the dangerous one

Flashing stops Klipper and writes to a board. It is therefore **not advertised
unless it is explicitly switched on**:

```ini
# ~/printer_data/config/mcu-updater/mcu-updater.cfg
[updater]
enable_flashing: true
```

Off by default so that updating the agent never silently grants a browser the
ability to reflash the printer. While off, `fw.flash` is absent from
`capabilities` and returns `-32601`; called directly it returns
`flashing_disabled`. **The CLI ignores this setting entirely** — it has always
been able to flash, and Phase 0 didn't change that.

Every refusal happens synchronously, before a job exists, so the caller gets a
real explanation instead of a job that dies a second later. In order:

| Check | Error code |
| --- | --- |
| capability gate | `flashing_disabled` |
| `serial` present | `-32602` |
| serial resolves to a type | `unknown_serial` / `ambiguous_serial` / `serial_tracked_elsewhere` |
| firmware has been built | `no_artifact` |
| board is on the bus | `device_not_found` |
| printer idle | `print_in_progress` (bypass with `force: true`) |

The idle check looks at **two** fields, and needs both:

- `print_stats.state` — refuses `printing` / `paused`. Only knows about
  virtual_sdcard print jobs.
- `idle_timeout.state` — refuses `"Printing"`. This is the one that means
  "Klipper is executing commands", and it is the only one that catches a manual
  home, a quad-gantry-level, a bed mesh, or a macro run from the console.

`print_stats` alone is not enough, and that gap was found on real hardware: a
flash went ahead during a QGL, Klipper was stopped mid-motion, and the MCU came
back shut down. `error.data.reason` is `"print"` or `"busy"` so a client can word
its message correctly.

The board check is up front on purpose: discovering a detached board *after*
stopping Klipper would mean an outage for nothing.

Once running, the job stops Klipper, writes, waits for the board to come back,
restarts Klipper, and then confirms Klipper is actually usable. Klipper is down
only for the write itself.

**The stop is verified.** If Klipper is still running after the stop request (no
passwordless sudo, Moonraker unreachable, a wedged unit) the job aborts with
`service_control` rather than flashing anyway, because Klipper holds the serial
port and writing into that contention is unsafe.

**The board is waited for.** After the write it reboots into the new firmware and
re-enumerates over USB, which takes a couple of seconds. Starting Klipper before
the device node exists means Klipper cannot find its MCU and comes up in an error
state.

**`systemctl is-active klipper` is not the same as Klipper being ready.** A board
that was mid-motion when the service stopped comes back with its MCU shut down, so
Klippy reaches `error` or `shutdown` and the printer will not move until a
`FIRMWARE_RESTART`. The job polls `printer.info`, and if Klippy is not `ready` it
issues `printer.firmware_restart` once and polls again — the same thing a human
does by hand. The final state is reported as `result.klippy_state`, and if it is
still broken the job log says exactly what to run. The flash itself is not marked
failed, because the write did succeed.

Cancellation is **deferred** for a flash (`immediate: false`) — see the table
above. Show "cancelling after the current board finishes…", not a spinner.

#### Getting Klipper back is the release gate

Four independent layers, because this is the one failure that leaves a printer
dead until a human notices:

1. the `finally` in `klipper_stopped()`;
2. `MoonrakerService.start()` falling through to `sudo systemctl start klipper`
   if Moonraker itself has gone away;
3. a journal at `~/printer_data/mcu-updater/.updater.state`, written before the stop and cleared
   after the start, which the agent reconciles on startup — this is what covers
   `kill -9`, where no `finally` ever runs;
4. `ExecStopPost` on the systemd unit.

The agent also **refuses to exit while a flash is in progress** (SIGTERM is
deferred up to `--shutdown-grace`, under the unit's `TimeoutStopSec`), because
`systemctl restart mcu-updater` mid-write would otherwise leave half an image
on a board.

### Bulk operations

Three methods, but only two implementations: `fw.update_all` is `build_all`
followed by `flash_all`, sharing their bodies rather than reimplementing the loop.

| Method | Stops Klipper | Writes to boards |
| --- | --- | --- |
| `fw.build_all` | no | no |
| `fw.flash_all` | yes, **once for the whole batch** | yes |
| `fw.update_all` | yes, once, after the builds | yes |

`fw.build_all` needs only a runner. The two that write are gated on
`enable_flashing` exactly like `fw.flash`, and are absent from `capabilities`
while it is off.

#### `scope`

Both scopes exist because provenance and intent are different things.

| `scope` | Meaning |
| --- | --- |
| `"stale"` (default) | Only what the recorded provenance says needs doing. |
| `"all"` | Everything in scope regardless. |

`stale` is more precise than a version comparison, not less: a rebuilt artifact
makes its boards stale even when the Klipper commit has not moved, which is what
`artifact_changed` reports and what a `mcu_version` comparison structurally
cannot see.

`all` is for when you know something the provenance cannot — an edited source
file that is not tracked, or a makefile patch whose effect you want on every
board of a type whatever the records say. It overrides the *judgement*, never the
physics: an offline board is still excluded, because a flash needs the board on
the bus and including it would only guarantee a failure partway through a batch
that has already stopped Klipper.

An unrecognised `scope` is refused with `-32602` rather than falling back to
`stale`, since a silent fallback would mean a user asking for `all` quietly
getting nothing.

#### What gets selected

Both selections walk the **registry**, so an adopted-but-untracked board can
never be swept into a bulk flash — it has no type, and therefore no firmware.

`build_all` takes every type with a saved `.config` for that tree. A type that has
never been through `menuconfig` is **skipped, not failed**: menuconfig is ncurses
and cannot run in the agent, so there is nothing the batch could do about it, and
failing over one unconfigured type would turn a one-type problem into a
fleet-wide one.

`flash_all` takes every tracked serial that is online, belongs to a type with a
built artifact, and (under `stale`) has `needs_flash: true`. It is per-serial, not
per-type: two boards of one model genuinely do run different firmware. Passing
`name` narrows it to a single type — that is "flash this type", implemented as
this same operation with a filter.

Both refuse with `nothing_to_do` when the selection comes out empty, rather than
starting a job that does nothing and reads as a bug.

`fw.flash_all` returns the selection up front, so the panel can name the boards
in its confirmation:

```json
{"job_id": "job-9", "job": {...},
 "boards": [{"type": "flylllplusbuffer", "serial": "4C00...-if00",
             "chipset": "stm32f072xb", "state": "klipper",
             "reason": "artifact_changed"}]}
```

#### Failures do not abandon the batch

One type failing to compile is usually about that type, so the loop continues and
reports what happened — matching what the CLI's `update-all` has always done:

```json
{"build": {"fw": "klipper", "built": ["bttebb36"],
           "failures": [{"type": "bttmmbv1", "error": "make failed (exit 2)"}]},
 "flash": {"flashed": [{"type": "bttebb36", "serial": "2900...-if00"}],
           "failures": []}}
```

A job with failures still ends `succeeded`; `result.*.failures` is the thing to
render, not the job state.

#### Two things `update_all` does that a naive composition would not

**The boards are chosen after the build, not before.** A build is what makes
boards stale, so selecting up front would use provenance the build is about to
invalidate.

**The idle gate is checked twice.** Once before the job is created, and again
after the builds finish — a fleet build takes minutes, and the check that passed
at submission is stale by the time anything is about to be written. If the printer
has started moving, the job fails with `print_in_progress` and Klipper is never
stopped at all.

Each board is also waited for individually after its write, exactly as in a single
flash: the last board of a batch would otherwise have nothing between its write
and the service restart.

### Setting up a brand-new board

A board with no bootloader on it is reached over **DFU**, and the whole flow is
four calls of which only one is new:

| Step | Call | New? |
| --- | --- | --- |
| 1. What is in DFU? | `fw.dfu.scan` | new, read-only |
| 2. Put Katapult on it | `fw.add_mcu.start {name}` | **new** |
| 3. Adopt what appeared | `fw.serial.add {name, serial}` | existing |
| 4. Put Klipper on it | `fw.flash {serial}` | existing |

There is no `fw.add_mcu.confirm`. Adopting the board is exactly what
`fw.serial.add` already does, validation included, so a confirm method would be a
second implementation to keep in step with the first — the same reason
`fw.flash_type` is just `fw.flash_all {name}`.

#### `fw.dfu.scan`

Reports rather than raises, because describing the situation *is* the work here.
`ready` is true only when exactly one board is present and openable.

| `reason` | What it means, and what to do |
| --- | --- |
| `null` | Ready. One board, openable. |
| `no_tool` | `apt install dfu-util`. Nothing to do with the board. |
| `permission_denied` | libusb saw a board and could not claim it. **The boot jumper worked** — this is the udev rule, not the hardware. |
| `none` | Nothing in DFU. Fit the jumper and replug. |
| `ambiguous` | More than one, and none named. Not a dead end — see below. |

`permission_denied` is kept apart from `none` deliberately. Collapsing them is
what once told a user "no DFU device detected, hold BOOT0 and replug" when the
board was sitting there perfectly, sending them to redo the one step that had
worked.

Each device carries `serial`, `path`, `devnum` and the raw line. A DFU board
exposes no `/dev/serial/by-id` name, so those are the only identity it has.
**`path` is the one to show prominently** — it is the only field corresponding to
a physical port, and therefore the only hint about which board is which.

#### `fw.add_mcu.start`

Writes Katapult to the board in DFU, waits for it to re-enumerate, and reports
what appeared:

```json
{"type": "bttebb36", "chipset": "stm32g0b1xx", "dfu_serial": "3941335F3434",
 "candidates":      [{"serial": "2D0043...-if00", "path": "...", "state": "katapult"}],
 "already_tracked": []}
```

`candidates` are boards that appeared and are **not** in the registry — the ones
to adopt. `already_tracked` are boards that appeared and already belong to a
type, which is the normal case when re-installing a bootloader: such a board sits
`offline` in the registry precisely because it had no firmware. Both mean the
flash worked; only `candidates` leaves anything to do.

Both empty means nothing came back at all, which is the only case worth
investigating.

**It cannot take a serial for the new board, because there isn't one yet.** A DFU
device has no by-id name, so the identity to adopt does not exist until Katapult
is on it. That is why this snapshots the bus first and diffs afterwards.

**Klipper is never stopped.** A board that is not in `printer.cfg` is not held by
Klipper, so there is no port contention and no reason for an outage. The
exclusive lock is still taken, so it cannot run beside a build or a flash.

Refusals, all synchronous and before a job exists:

| Check | Error code |
| --- | --- |
| capability gate | `flashing_disabled` |
| type exists | `unknown_type` |
| chipset uses DFU at all | `unsupported_chipset` (RP2040 needs BOOTSEL + `.uf2`) |
| Katapult has been built for the type | `no_artifact` |
| something is in DFU | `dfu_none` / `dfu_permission_denied` / `dfu_no_tool` |
| exactly one, or one named | `dfu_ambiguous` |
| the named serial is present | `device_not_found` |

**Several boards in DFU is a choice, not a dead end.** dfu-util takes `-S`, `-p`
and `-n`, so passing `dfu_serial` targets one exactly. It is still refused by
default: a USB serial like `3941335F3434` says nothing about which board on the
bench it is, so choosing on the user's behalf risks writing a bootloader to the
wrong one.

The write is pinned to the chosen device even when only one is attached — between
the scan and the command, a second board can be jumpered and plugged in.

Finding no new board **warns rather than failing the job**: the write may have
succeeded and the board simply be slow or on a marginal port, so the log says to
check `/dev/serial/by-id` and adopt directly.

#### A board that turns up later is still adopted

The wait is 15 seconds, which a board on a chain of hubs can miss - and one
unplugged after flashing and brought back tomorrow will certainly miss. So the
DFU serial is paired with the chosen type in `.dfu-pairings.json`, written
**after the write and before the wait**, since the wait timing out is exactly the
case it covers.

The agent's bus poll then adopts such a board when it appears. That is the
completion of an operation already asked for - the type was chosen and the button
pressed - rather than a new decision. Five conditions keep it from ever being a
surprise:

- only **untracked Katapult** devices; anything already in the registry is left alone
- only an **unambiguous** DFU-serial match, since the derivation sums two id words
- only within the **TTL** (24h), so a board found in a drawer next month is the stranger it has become
- only if the **type still exists**
- the pairing is **consumed**, so it cannot re-add a board you deliberately untracked

Each adoption is logged and emits a `state` event, because a registry edit nobody
can see happening is the thing to avoid.

Only STM32 is supported. RP2040 needs BOOTSEL mass storage and a `.uf2`, which is
a different mechanism; `unsupported_chipset` says so rather than failing later
with something about dfu-util.

## ESP32 displays

Knomis and anything else built by PlatformIO. Different enough from an MCU to be
separate: no Kconfig, no Katapult, no chipset to reason about — **a PlatformIO
env already names the board, its partitions and its build flags, so the env is
the type.**

```ini
# mcu-updater.cfg
[updater]
display_source: ~/knomi_serial     # one repo, shared by every env

[display knomi_toolchanger]
# env: knomi_toolchanger           defaults to the section name
# source: ~/knomi_serial           defaults to display_source
# klipper_section: knomi_serial    which [<prefix> X] sections are this type's
```

Adding the second screen is one more section.

**The device list is Klipper's, not ours.** `[knomi_serial T0_knomi]` already
names the port it uses, so a second copy here would only be something to disagree
with. It arrives in the same `configfile.settings` payload `fw.status` already
fetches for the MCU version join.

### `fw.display.list`

```json
{"displays": [{"name": "t0_knomi", "section": "knomi_serial t0_knomi",
               "configured_path": "/dev/knomi_t0",
               "resolved_path": "/dev/ttyUSB0", "present": true}],
 "reachable": true}
```

`present` is the field this exists for. The klippy module catches a failed open
and runs in no-op mode — deliberately, so one dead screen cannot take Klipper
down — which means a missing symlink produces **no error anywhere**. Klipper
starts happily with a blank display. Nothing else in the system notices.

`reachable` is distinct from an empty list: "no displays configured" and "we
could not ask Klipper" must not look the same.

### `fw.display.flash`

Two properties carry the risk, and both are enforced rather than documented.

**A port is never inferred.** `pio run -t upload` auto-detects one when told
nothing — and was observed on this printer picking between two indistinguishable
CH340s, with no way for the user to know which it took. The upload refuses an
empty port and always passes `--upload-port`.

**The screen list is read before Klipper stops.** It comes from
`configfile.settings`, which only a *running* Klipper can answer, so reading it
after the stop would find nothing and flash nothing. Every other flow in this API
can query mid-job; this one cannot.

Klipper is stopped once for the batch, because the klippy module holds the port
open and esptool cannot have it while it does. The idle gate applies — a display
is not special enough to interrupt a QGL for.

**Verification is free.** esptool's ROM handshake refuses to write to anything
that is not an ESP32, so the target check is inherent rather than a step that
could be skipped.

```json
{"env": "knomi_toolchanger",
 "flashed": [{"name": "t0_knomi", "port": "/dev/knomi_t0",
              "mac": "cc:ba:97:19:aa:38", "chip": "ESP32-S3 (QFN56) (revision v0.2)"}],
 "failures": [],
 "moved": [{"name": "t0_knomi", "port": "/dev/knomi_t0",
            "was": "aa:bb:...", "now": "cc:ba:..."}]}
```

`moved` is the swap signal. A display's MAC is in efuse, so it survives
reflashing — and the CH340 in front of it has no serial of its own, making this
the only durable identity a screen has. Every upload records `MAC → port`, so a
different display answering on a known port is reported. Two tophat boards
plugged into each other's sockets moves every screen on them at once, and nothing
else would say so.

It is a **warning, not a failure**: the write succeeded either way, and all
displays of a type run the same image, so a swap is a config problem rather than
a firmware one.

### The log, and its sequence numbers

Every log line gets a monotonic index, starting at 0. A `log` event carries the
index of its **first** line:

```json
{"job_id": "job-7", "seq": 120,
 "lines": [{"i": 120, "s": "stdout", "t": "  Compiling out/src/stepper.o"},
           {"i": 121, "s": "stdout", "t": "  Compiling out/src/buffer.o"}]}
```

Keys are short (`i`/`s`/`t`) because a build emits hundreds of these to every
connected client. `s` is one of `stdout`, `info`, `warn`, `error`, `cmd`.

**Client contract.** Track the next index you expect, starting at 0. On a `log`
event:

- `seq == expected` → append, and set `expected = seq + lines.length`.
- anything else → **a gap.** Call `fw.job.get {job_id, log_from: expected}` and
  replace from there.

Without this a streaming log silently lies after a dropped frame, a page reload,
or a Moonraker restart mid-build.

`fw.job.get` returns `log_from` — the first index it could *actually* serve. That
may be higher than you asked for, because the log is a ring buffer (default 2000
lines, `log_ring_size`) and a very long build evicts its own beginning. When
`log_from > log_from_you_requested`, or `log_dropped > 0`, tell the user lines
were omitted rather than renumbering silently.

Batching is not optional: flushes happen at 250 ms, 40 lines, or 32 KiB,
whichever comes first. One event per line would be 400–800 broadcast frames per
build to every connected client.

## Events

The agent pushes with `connection.send_event`; clients receive
`notify_agent_event` whose params are a **list** with one object:

```json
{"jsonrpc": "2.0", "method": "notify_agent_event",
 "params": [{"agent": "klipper_updater", "event": "state", "data": {...}}]}
```

| Event | `data` | When |
| --- | --- | --- |
| `state` | the full `fw.status` payload | on connect, when Klipper's service state changes, and after any job finishes |
| `bus` | `{devices: [BusDevice]}` | when the set of attached devices changes |
| `job` | `{job: Job}` | on every state transition and progress step |
| `log` | `{job_id, seq, lines}` | batched: 250 ms / 40 lines / 32 KiB |

Poll interval for `bus` is 15s idle, dropping to 2s while a job runs (a board
disappears and reappears within seconds during a flash).

Pending log lines are always flushed *before* the `job` event that follows them,
so the UI never shows "finished" above the final few lines of output.

A job outlives the connection deliberately. If Moonraker restarts mid-build the
build keeps going, and on reconnect the agent re-emits `state` plus a `job` event
for anything still running — so a client that joined late isn't left thinking the
printer is idle.

`connected` and `disconnected` are **reserved** — Moonraker emits those itself,
carrying the agent's identify payload, and rejects any attempt by an agent to
send them. That is deliberately what the panel uses for availability detection.

## Availability detection

No polling needed:

1. On store init, `server.extensions.list` → is `klipper_updater` present?
2. If so, `fw.ping` (version gate), then `fw.status`.
3. Live updates come from Moonraker's own `connected` / `disconnected` agent
   events.

## Later phases

Nothing is reserved any more — `fw.add_mcu.start` shipped, and it was the last
one. See "Setting up a brand-new board" above.

**`fw.add_mcu.confirm` was never implemented and never will be**, for the same
reason as `fw.flash_type`: adopting the board is `fw.serial.add`, which already
does it with the validation.

**`fw.flash_type` was never implemented and never will be.** It is
`fw.flash_all {name}`: the same selection and the same loop with a filter, rather
than a second implementation to keep in step with the first.

`fw.build` refuses a type with no saved `.config`, returning `no_saved_config`.
`make menuconfig` is an ncurses UI and cannot run inside the agent, so the
`.config` has to come from either that command over SSH or the `fw.kconfig.*`
methods, which write the same file.
