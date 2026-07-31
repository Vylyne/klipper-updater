# Agent API

The contract between `klipper-updater` (Python) and the Mainsail panel
(TypeScript). Both sides are hand-written, so **this file is the single source of
truth** — and `tests/test_agent_methods.py` is what stops them drifting.

- Agent name: `klipper_updater`
- `api_version`: **1**
- Phase: **2** (can build; still cannot flash or mutate the registry)

**Gate controls on `capabilities` from `fw.ping`, not on `phase`.** An agent
without a job runner does not register or advertise the job methods at all, so a
newer panel talking to an older agent must hide its build buttons rather than
offer something that returns `-32601`.

## Transport

The agent connects to `~/printer_data/comms/moonraker.sock` and identifies itself:

```json
{"jsonrpc": "2.0", "id": 1, "method": "server.connection.identify",
 "params": {"client_name": "klipper_updater", "version": "0.9.0",
            "type": "agent", "url": "https://github.com/Vylyne/klipper-updater"}}
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
`unsupported_chipset`, `busy`, `print_in_progress`, `cancelled`, `tty_required`.

JSON-RPC codes: `-32601` unknown method, `-32602` bad params, `-32000`
application error (see `data.code`), `-32603` internal.

## Methods

| Method | Arguments | Returns |
| --- | --- | --- |
| `fw.ping` | — | version/capability handshake |
| `fw.status` | — | everything the panel needs, in one call |
| `fw.type.list` | — | `{types: [TypeStatus]}` |
| `fw.bus.scan` | `only_untracked?`, `chipset?` | `{devices: [BusDevice]}` |
| `fw.artifacts` | `name` (required) | `{klipper: Artifact, katapult: Artifact}` |
| `fw.settings.get` | — | `{settings: Settings}` |
| `fw.build` | `name`, `fw`, `jobs?`, `clean?` | `{job_id, job}` — returns immediately |
| `fw.job.get` | `job_id?`, `log_from?` | `{job, log, log_from, log_next, log_dropped}` |
| `fw.job.cancel` | `job_id?` | `{cancelling, immediate}` |

### `fw.ping`

```json
{"api_version": 1, "version": "0.9.0", "dry_run": false, "enable_flashing": false,
 "phase": 1, "capabilities": ["fw.artifacts", "fw.bus.scan", "..."],
 "host": {"nproc": 4, "python": "3.9.2", "settings_dir": "/home/biqu/mcus"},
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
| `build` | **Immediate.** The whole `make` process group is killed. Always safe. |
| `flash`, `flash_type` | **Deferred** — honoured only *between* devices. Interrupting a `flashtool -f` write leaves a board with half an image. Show "cancelling after the current board finishes…". |
| `update_all` | Deferred between types and devices; the in-flight build is cancellable. |

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

Reserved names, not yet implemented — the agent returns `-32601` for these and
does not advertise them, which is why the panel gates on `capabilities`:

`fw.flash`, `fw.flash_type`, `fw.update_all`, `fw.type.add`, `fw.type.update`,
`fw.type.remove`, `fw.serial.add`, `fw.serial.remove`, `fw.settings.set`,
`fw.kconfig.*`, `fw.add_mcu.start`, `fw.add_mcu.confirm`.

`fw.build` deliberately refuses a type with no saved `.config`, returning
`no_saved_config` with the exact CLI command to run. `menuconfig` is an ncurses
UI and cannot run inside the agent — that stays an SSH operation until the
Kconfig web UI lands.
