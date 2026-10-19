# Design

How `coldstart` is put together, the supervisor's state machine, how each
failure is handled, and the decisions taken where SPEC.md left room.

## Modules

| module                  | job                                                                 |
|-------------------------|---------------------------------------------------------------------|
| `coldstart/config.py`   | TOML + env overrides, defaults for every key, strict validation     |
| `coldstart/runlog.py`   | `runs/<timestamp>-<cmd>/log.txt`, `NN-<step>.jpg` screenshot names  |
| `coldstart/botbase.py`  | sys-botbase client (TCP 6000)                                       |
| `coldstart/rig.py`      | rig control port client (UDP 5600), loose reply parsing             |
| `coldstart/twili.py`    | optional `twib`/`twibd` wrapper: ps, terminate, sysmodule launch; `ps` parser |
| `coldstart/classify.py` | reference store and nearest-reference classifier                    |
| `coldstart/probe.py`    | "does the console answer at all", webhook                           |
| `coldstart/launch.py`   | the launch sequence and its failure kinds                           |
| `coldstart/supervisor.py` | the state machine                                                 |
| `coldstart/install.py`  | FTP install plan/execution, `start-sysmodule`                       |
| `coldstart/cli.py`      | argparse front end, exit codes                                      |
| `sim/botbase_sim.py`    | fake console for the tests; `sim/bin/twib`, `sim/bin/twibd` fakes   |

## Supervisor state machine

```
                  rig answers (any state) ──────────────────────────────┐
                                                                         v
   start ──> WATCH ── silent >= silent_s ──> RECOVER ── launched ──> WATCH
               ^                               │  │
               │                               │  └─ hard failure ───────────┐
               │            soft failure,      │     (console_down,          │
               │            attempt < N        v      botbase_down)          v
               │                            BACKOFF                        HUMAN
               │                               │                             ^
               │      backoff_s[i] elapsed     │                             │
               │      (pinging every poll_s)   └──> RECOVER                  │
               │                                                             │
               │                    soft failure, attempt == N ──────────────┘
               │
   HUMAN ──> RECOVER   when sys-botbase answers again (after a hard failure)
   HUMAN ──> RECOVER   after human_rearm_s (after "failed N times"; 0 disables)
```

- **WATCH**: ping every `poll_s` (5 s). A heartbeat line is logged on the
  first answer, on a role change, after a silence, and otherwise every
  `heartbeat_s` (10 min). Silence is measured from the last answer, or from
  start-up if the rig never answered. After `silent_s` (60 s): RECOVER.
- **RECOVER** (one attempt):
  1. Only with `twili.enabled = true` (default false, since twili is leaving
     the console to free memory for sys-botbase): `twib ps`; if the hbl title (`twili.hbl_title_id`) is listed, the slot
     is *deaf*: `twib terminate <pid>`, then poll `ps` until it is gone
     (`terminate_wait_s`). If it cannot be terminated, the attempt fails
     (soft) without launching: A on the suspended tile would only resume the
     same deaf process. If twili is disabled or unreachable, that is logged
     and the launch goes ahead; it closes a deaf application itself through
     the HOME menu (launch steps 2-3).
  2. The launch sequence (below). Success → WATCH.
  3. Failure: hard → HUMAN at once; soft → BACKOFF, or HUMAN after
     `attempts` (3) attempts.
- **BACKOFF**: keeps pinging every `poll_s`, so a rig that comes back (a slow
  boot, a human) ends it; otherwise RECOVER when `backoff_s[i]` has elapsed
  (60 s, 120 s; the last value repeats for more attempts).
- **HUMAN**: logs one `NEEDS A HUMAN (<kind>): <what>. Next: <step>` line and
  POSTs the webhook if configured, then pings every `human_poll_s` (30 s).
  After a hard failure it also checks sys-botbase each time and retries as
  soon as it answers — this is the "console rebooted / human pressed POWER"
  path, which is exactly the case the tool exists for. After "launch failed
  N times" it retries after `human_rearm_s` (30 min) so a one-off glitch at
  night does not need a human in the morning.

`supervise --exit-on recovered|human` and `--max-runtime` exist for scripts
and the tests; normal use is to leave it running in tmux. SIGTERM/Ctrl-C
stop it cleanly and stop the twibd it started.

## Launch sequence

1. Connect to sys-botbase, `getVersion`. If that fails, probe the console's
   always-on TCP services (sys-ftpd 5000, twili 15152; ICMP optionally):
   none answer → `console_down`, one answers → `botbase_down`. Both are hard
   failures.
2. Up to `max_normalize_steps` (5) times: screenshot, classify. `home` →
   proceed; `dialog` → A; anything else (game, waiter, user-select,
   close-prompt, unknown) → HOME. A final screenshot must be `home`, else the
   attempt fails (soft). Without twili, "a homebrew process is alive" is
   exactly this: the rig is silent and the first screenshot shows an
   application (game/waiter/hbmenu); it is logged as a deaf slot, and HOME
   suspends it.
3. DLEFT × `dleft_presses` (12; DLEFT past the left end does nothing). The
   cursor is now on tile 0, the most recently played application, i.e. the
   suspended one if any. X, screenshot: `close-prompt` → A (settle
   `settle_close_s`), screenshot must be `home` (an error dialog in between
   is dismissed with A); `home` → nothing was suspended, X was a no-op;
   anything else → B and the attempt fails.
4. DRIGHT × `tile`, screenshot (a non-HOME here is logged as a warning), A.
5. Screenshot: `user-select` → A; `dialog` → attempt fails.
6. Ping the rig every `rig_poll_s` for up to `wait_rig_s` (120 s). Success
   reports the role (game or waiter) and the elapsed time. Timeout → a last
   screenshot for the log, attempt fails (soft).

**Blind mode.** Without references, without Pillow, or when `pixelPeek`
returns nothing (Atmosphère debug mode off), screens cannot be told apart,
and the prototype's sequence runs instead: HOME, HOME, DLEFT × 12, DRIGHT ×
tile, A, A. It is logged as blind. It never presses X: if nothing is
suspended, the following A would be a blind A on the HOME menu, so a
suspended deaf application cannot be closed blind. It cannot see an error dialog: the first
attempt's A then only dismisses the dialog, and the next attempt launches.
Screenshots are still taken and saved where possible.

Every click is followed by a configurable settle delay (defaults: HOME 2.5 s,
D-pad 0.4 s, A 3 s, dialog 2 s). Button names are validated before sending
because sys-botbase presses A for any name it does not know, and the client
refuses `press R` outright.

## Failure handling summary

| situation                                   | detected by                               | handling |
|---------------------------------------------|-------------------------------------------|----------|
| rig silent, nothing running                 | ping timeout, HOME on screen, X no-op     | launch |
| rig silent, application in the foreground   | ping timeout, game/waiter/unknown screen  | HOME, X, A (close), then launch |
| rig silent, application suspended behind HOME | ping timeout, X shows `close-prompt`    | A (close), then launch |
| X shows neither HOME nor the close prompt   | classification                            | B, soft failure |
| (twili enabled) hbl alive, rig silent       | `twib ps` lists hbl                       | terminate, then launch |
| (twili enabled) terminate fails             | twib error / still listed                 | soft failure (retry, then human) |
| twili disabled or unreachable               | config / twib errors                      | logged, launch (HOME-menu close path) |
| console asleep / off / fatal                | no TCP on 6000, 5000, 15152 (ICMP opt.)   | hard: `console_down`; resumes when sys-botbase answers |
| sys-botbase not running                     | 6000 refused, 5000 or 15152 accept        | hard: `botbase_down`; resumes when it answers |
| screenshots unavailable                     | `pixelPeek` empty line                    | blind for the rest of the attempt |
| HOME not reached                            | 5 normalize steps without `home`          | soft failure |
| wrong tile / tile does not start the rig    | no rig answer within `wait_rig_s`         | soft failure, bounded retries, then human |
| dialog after A on the tile                  | classification                            | soft failure |
| connection drops mid-sequence               | send/recv error                           | soft failure (next attempt re-probes) |
| launch failed N times                       | attempt counter                           | human; retry after `human_rearm_s` |
| human (or anything) fixes it                | rig answers                               | back to WATCH from any state |

## Classifier

Crop (`classify.crop`, default the screen minus the top 12 % where the clock
and battery are), downsample with box filtering to `classify.grid` (16×9)
mean colours, distance = mean Euclidean RGB distance per cell (0 to ~441).
The nearest reference wins if within `classify.threshold` (40), else
`unknown`. The nearest name and distance, and the next three, are logged for
every screenshot, so the threshold can be tuned from a night's log. A
reference's label is its name or, for `home-2` style names, the known label
it starts with. In the simulator, same-screen distances are ~0–5 and
different screens are >100 apart.

## Decisions where SPEC.md left room (or was departed from)

- **Pinned revision**: `ae13548` rather than the `v2.5` tag. The reference
  sources are byte-identical to `ae13548`; at `v2.5` `commands.c` differs
  (one getMetaData check). Both report version "2.5". `SYS_BOTBASE_REV=v2.5`
  selects the tag.
- **"Does not answer ICMP/TCP"** is judged by TCP to the console's always-on
  services (sys-ftpd and twili ports) rather than ICMP, because a sleeping
  Switch may keep Wi-Fi up and answer pings. ICMP via the system `ping` is
  available with `console.icmp = true`.
- **Hard failures are not retried** (the spec calls a dead port 6000 a hard
  failure), but the HUMAN state watches for sys-botbase to come back and
  resumes then. `human_rearm_s` (not in the spec) re-arms after "failed N
  times"; set it to 0 to wait for a human only.
- **twili is optional and off by default** (spec change 2026-10-19: it is
  leaving the console). The HOME-menu close (X, A) runs on every sighted
  launch, not only when a deaf slot was suspected: a suspended application
  on tile 0 would otherwise be resumed instead of relaunched, and X with
  nothing suspended is a no-op. X is pressed with the cursor on tile 0
  because the suspended application is the most recently played one.
- **`start-sysmodule` is a fallback**; the normal install is `--boot` and a
  reboot by the owner.
- **A failed terminate (twili enabled) counts as a failed attempt** and does not launch,
  because launching would resume the deaf process.
- **A dialog right after A on the tile** fails the attempt instead of being
  dismissed and retried within the same attempt; the next attempt starts by
  dismissing it.
- **`launch` does nothing if the rig already answers** (`--force` to launch
  anyway), so running it by hand cannot disturb a working loop.
- **`install` defaults to the local build**; the release artifact in
  `reference/` is used only with `--release`.
- **twibd** (only when twili is enabled): if `twili.socket` is empty the tool owns a twibd per command
  (socket in the run directory, or a short temp path when that would exceed
  the AF_UNIX limit), reaps it on exit, and runs `connect-tcp` whenever `ps`
  reports no device.
- **Simulator FTP** only greets (it stands in for "the console answers TCP");
  `install --yes` is tested against an in-process fake `ftplib` object.

## sys-botbase facts relied on (from the pinned source)

- One line per command, split on space/CR/LF (`args.c`); line ends at LF.
- `click`, `press`, `release`, `configure` print nothing; `getVersion`
  prints `2.5`; `pixelPeek` prints the JPEG as upper-case hex on one line,
  or an empty line if `capsscCaptureForDebug` fails.
- Unknown button names become A (`util.c: parseStringToButton`).
- `click` holds for `buttonClickSleepTime` (50 ms default; settable with
  `botbase.click_hold_ms`).
- The sysmodule sleeps 20 s in `__appInit` before listening.

## Not verified against real hardware

Nothing here has run against the console yet (by rule). In particular: the
exact HOME-menu timings, whether sys-botbase's HOME click is reliably
recognised by the HOME menu on firmware 20.2.0 (`click_hold_ms` is the knob
if not), that X on HOME opens "Close software" for the suspended application
with the cursor on its tile (and does nothing when none is suspended), the
real `twib` output and exit codes beyond the documented `ps`
table, the error dialog's response to A, and pixelPeek's size and latency.
