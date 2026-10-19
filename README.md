# switch-coldstart

Host-side tooling that brings the rig's homebrew loop on the Switch back to
life when nothing of it is running: after the homebrew process died, after a
kernel fatal someone cleared with POWER, or after a reboot. The console then
sits at the HOME menu; `coldstart` presses the buttons from the host through
the sys-botbase sysmodule, checks what it sees with screenshots, and hands
the console back to the rig as soon as the rig answers its UDP ping.

It relies on what the rig already set up on the card: hbl with
`override_any_app_key=!R` (A on any application tile boots hbl instead of the
application) and the rig's waiter as `sdmc:/hbmenu.nro` (hbl starts the
loop). So "press A on a tile" is a complete cold start. **The tool never
holds R** — holding R would launch the real application.

Everything the tool does is logged with timestamps to stdout and to
`runs/<timestamp>-<command>/log.txt`, with every screenshot saved next to it
as `NN-<step>.jpg`, so a failed night can be read in the morning.

## Requirements

- Python 3.12 or newer.
- Pillow (the only third-party dependency) for decoding and classifying
  screenshots. If `python3 -c 'import PIL'` fails:
  `python3 -m pip install --user pillow`. Without Pillow the tool still
  starts, `launch` and `supervise` run the "blind" sequence, and `shot` /
  `learn` say clearly what is missing.
- `twib` and `twibd` (twili's host tools) on `PATH`, or their paths in the
  config. The tool starts and stops its own `twibd` unless you point
  `twili.socket` at one you already run.
- For building the sysmodule: devkitPro with devkitA64 and libnx
  (`/opt/devkitpro` by default, or `$DEVKITPRO`).
- The console: Atmosphère with `enable_am_debug_mode=1` (needed for
  screenshots; this console has it), sys-ftpd on port 5000, twili.

Run the tool from the checkout as `bin/coldstart <command>` (or
`python3 -m coldstart` with the checkout on `PYTHONPATH`).

## Configuration

Copy `coldstart.example.toml` to `coldstart.toml` in the directory you run
from (or use `--config FILE` / `$COLDSTART_CONFIG`). Every key is optional;
the example lists all defaults with a comment each. Unknown keys are errors.
Environment overrides: `COLDSTART_HOST` (or `SWITCH_IP`),
`COLDSTART_BOTBASE_PORT`, `COLDSTART_RIG_PORT`, `COLDSTART_FTP_PORT`,
`COLDSTART_TWILI_PORT`.

The defaults match this rig: console `192.168.128.150`, sys-botbase 6000, rig
UDP 5600, hbl title `01007ef00011e000`, tile 0 (the leftmost tile is the most
recently played application), act after 60 s of silence, 3 attempts with
60 s / 120 s backoff.

`bin/coldstart status` prints the effective configuration and what answers
right now (rig, sys-botbase, the console's always-on ports, twili, recorded
references, Pillow). `status --no-probe` prints only the config.

## 1. Build the sysmodule

```
make sysmodule
```

clones https://github.com/olliz0r/sys-botbase into `sysmodule/src` at the
pinned commit `ae13548c587b75abf54f32032ad8d52f51eda05e` (one commit after
the v2.5 tag, still version "2.5", and the revision the sources in
`reference/sys-botbase-src/` came from) and builds
`sysmodule/out/430000000000000B/exefs.nsp`. `SYS_BOTBASE_REV=v2.5 make
sysmodule` builds the tag instead. If devkitPro or libnx is missing it says
so and stops.

## 2. Install it — your call

Installing a third-party sysmodule is the owner's decision, so `install`
only prints its plan unless you add `--yes`:

```
bin/coldstart install                 # dry run: prints exactly what it would upload
bin/coldstart install --yes           # upload exefs.nsp to /atmosphere/contents/430000000000000B/
bin/coldstart start-sysmodule         # start it now through twili (no reboot), check port 6000
bin/coldstart status                  # "sys-botbase (TCP 6000): version 2.5"
```

Without `--boot` nothing makes it start at boot: try it first with
`start-sysmodule` (it runs `twib launch 430000000000000B none`; sys-botbase
waits ~20 s before it listens, the command waits up to 90 s). Once it has
proven itself on this firmware, make it permanent:

```
bin/coldstart install --yes --boot    # also writes flags/boot2.flag
```

No local build? `--release` installs the v2.5 release artifact shipped in
`reference/sys-botbase-2.5/` (a third-party binary) instead.

To remove it: delete `/atmosphere/contents/430000000000000B/` from the card
(FTP) and reboot.

## 3. Record the reference screens

The launch sequence decides what to press by comparing screenshots with
reference images you record once. Bring the console to each screen by hand
and run `learn` with its name:

| name          | how to get there                                                    |
|---------------|---------------------------------------------------------------------|
| `home`        | the HOME menu, cursor on the tile row                               |
| `dialog`      | the "The software was closed because an error occurred" dialog (any dialog over HOME will do) |
| `user-select` | the profile picker ("Select a user") after A on a tile, if it appears on this console |
| `waiter`      | the rig's waiter running                                             |
| `game`        | the game running                                                     |

```
bin/coldstart learn home
bin/coldstart learn dialog
...
bin/coldstart shot                    # take a screenshot, classify it, print nearest + distance
bin/coldstart shot --file runs/.../03-tile-0.jpg   # re-classify a saved screenshot
```

References are stored in `refs/<name>.jpg` with `refs/index.json`. Several
references can stand for one screen: `home-2`, `home-dark`,
`user-select-2` all count as their prefix's label. Any other name classifies
as itself, which the supervisor treats like an unknown screen.

Tuning: every classification is logged as e.g.
`home (nearest home d=4.8; next dialog=109.0, game=117.4)`. The distance is
the mean RGB distance over a 16×9 grid (`classify.grid`) of the screen minus
its top 12 % (`classify.crop`, so the clock does not count). A screen is
`unknown` when its nearest reference is farther than `classify.threshold`
(40). If the HOME menu's tiles or theme change the distances, record a
second `home-2` rather than raising the threshold a lot.

Without any references the tool still works, "blind": HOME, HOME, tile, A, A
(the prototype's sequence) and only the rig's answer decides success.

## 4. Run the supervisor

In tmux:

```
bin/coldstart supervise
```

It pings the rig every 5 s and logs a heartbeat every 10 min while it
answers. After 60 s of silence it:

1. asks twili whether the hbl process still exists; if it does, the slot is
   "deaf" and it terminates it (twili unreachable: noted, goes on);
2. runs the launch sequence; on success it goes back to watching;
3. on failure retries twice more (60 s, then 120 s later), then logs
   `NEEDS A HUMAN ...` and keeps pinging every 30 s, so your own launch is
   noticed. After 30 min (`supervise.human_rearm_s`) it tries again.

If the console does not answer at all, or sys-botbase does not answer while
the console does, it does not retry: it logs one `NEEDS A HUMAN` line with
the next step and resumes by itself as soon as sys-botbase answers again
(for example after you pressed POWER and the console booted).

Notifications: set `notify.webhook = "https://..."` and every `NEEDS A
HUMAN` also POSTs a JSON body
(`{"event": "needs_human", "kind": ..., "message": ..., "next_step": ...,
"console": ..., "time": ..., "run_dir": ...}`).

Other commands:

```
bin/coldstart launch                  # one launch now (does nothing if the rig answers; --force)
bin/coldstart launch --dry-run        # print the sequence with all delays, touch nothing
bin/coldstart launch --tile 2
```

Exit status: 0 ok, 1 failed, 2 usage/config error, 3 needs a human.
Stop the supervisor with Ctrl-C or SIGTERM; it stops the twibd it started.

## What still needs a human

- **A kernel fatal, a powered-off or sleeping console.** Nothing on the host
  can press POWER. The supervisor says `NEEDS A HUMAN (console_down)`; after
  the console is on again (and sys-botbase started at boot, i.e. installed
  with `--boot`), it resumes by itself.
- **sys-botbase not running** (`botbase_down`): not installed, not started
  since the last reboot without `--boot`, or crashed. Run
  `bin/coldstart start-sysmodule`.
- **The launch failing repeatedly** (`launch_failed`): the screenshots in the
  run directory show where it got stuck (an unexpected dialog, a tile that is
  not the game, a theme change that needs new references).
- **A process twili cannot terminate.**
- Settings that keep the console reachable — HOME-menu auto-sleep set to
  Never, power, Wi-Fi — are outside this tool.

## Development

```
make check     # byte-compile, unit + scenario tests (~75 s), shellcheck if installed
```

Everything is tested against `sim/botbase_sim.py`, a fake console speaking
the sys-botbase subset with HOME / dialog / user-select / game / waiter /
asleep screens, a fake rig responder, and fake `twib`/`twibd` in `sim/bin`.
To try the tool by hand without a console:

```
python3 sim/botbase_sim.py --botbase-port 16000 --rig-port 15600 --ftp-port 15000 \
    --ctl-port 16099 --screen dialog --user-select &
PATH=$PWD/sim/bin:$PATH COLDSTART_SIM_CTL=127.0.0.1:16099 COLDSTART_HOST=127.0.0.1 \
    COLDSTART_BOTBASE_PORT=16000 COLDSTART_RIG_PORT=15600 COLDSTART_FTP_PORT=15000 \
    COLDSTART_TWILI_PORT=15000 bin/coldstart launch
```

DESIGN.md describes the state machine, the failure handling and the
decisions behind them.
