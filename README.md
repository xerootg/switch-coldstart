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
- Optional: `twib` and `twibd` (twili's host tools). twili is being removed
  from the console (it holds 14 MiB of the system pool sys-botbase needs),
  so the tool does not use it unless `twili.enabled = true`; then it starts
  and stops its own `twibd` unless you point `twili.socket` at a running one.
- For building the sysmodule: devkitPro with devkitA64 and libnx
  (`/opt/devkitpro` by default, or `$DEVKITPRO`).
- The console: sys-ftpd on port 5000 (anonymous). Screenshots are taken by
  pressing CAPTURE and fetching the new image from the album over FTP (then
  deleting it from the card). The album is found automatically
  (`screenshot.album_root = "auto"`): with emuMMC enabled in
  `/emummc/emummc.ini` it is `/<nintendo_path>/Album` (on the rig's console
  `/emuMMC/RAW1/Nintendo/Album`), otherwise `/Nintendo/Album`; `status`
  prints the resolved root, and an explicit path in the config wins. The
  album route exists because sys-botbase's `pixelPeek` is refused on this console (capsrv 0xE0CE: the real DebugModeFlag is off;
  `enable_am_debug_mode` only fakes it for am). `screenshot.source =
  "pixelpeek"` selects pixelPeek on consoles where it works.
- HOME-menu auto-sleep set to Never (System Settings → Sleep Mode).

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
bin/coldstart install --boot          # dry run: prints exactly what it would upload
bin/coldstart install --yes --boot    # upload exefs.nsp and flags/boot2.flag to
                                      #   /atmosphere/contents/430000000000000B/
```

Then reboot the console yourself (with twili's boot flag disabled, so the
system pool has room for sys-botbase). sys-botbase waits ~20 s after boot
before it listens; then:

```
bin/coldstart status                  # "sys-botbase (TCP 6000): version 2.5"
```

If it does not answer, the console probably could not start it (memory, or
incompatibility with this firmware): remove the folder below and reboot.

Fallback only, while twili still runs: `install --yes` without `--boot`
followed by `bin/coldstart start-sysmodule` starts it without a reboot
(`twib launch 430000000000000B none`, then waits up to 90 s for port 6000).

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
| `close-prompt`| HOME with the game suspended, then X: the "Close software" prompt (press B afterwards) |
| `update-prompt`| "A new update is available" after A on the tile, if the console offers one (press B afterwards; never A, the cursor is on Download) |

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

All HOME-menu dialogs share one layout, so record every dialog you meet
(`update-prompt`, and further `dialog-2`, `dialog-ok`...): a screen whose
nearest reference has another label within 2 × d + 3 of it
(`classify.ambiguity_factor`, `ambiguity_offset`) is `unknown`, "ambiguous
between X and Y", and the tool never presses A on it. That rule only
protects against dialogs that have a reference: without `update-prompt` the
update prompt was a confident `close-prompt` (5.2, next label 19.1).

Tuning: every classification is logged as e.g.
`home (nearest home d=4.8; next dialog=109.0, game=117.4)`. The distance is
the mean RGB distance over a 16×9 grid (`classify.grid`) of the screen minus
its top 12 % (`classify.crop`, so the clock does not count). A screen is
`unknown` when its nearest reference is farther than `classify.threshold`
(40). If the HOME menu's tiles or theme change the distances, record a
second `home-2` rather than raising the threshold a lot.

Without any references the tool still works, "blind": HOME, HOME, tile, A,
and only the rig's answer decides success. The prototype's second A (for a
profile picker) is off unless `launch.blind_second_a = true`: on the update
prompt the cursor starts on Download, so a blind second A downloads the
update. Blind
mode cannot close a suspended deaf application (it never presses X without
seeing the screen), so record the references, `close-prompt` included.

## 4. Run the supervisor

In tmux:

```
bin/coldstart supervise
```

It pings the rig every 5 s and logs a heartbeat every 10 min while it
answers. While the rig is silent it clicks ZR every 120 s (keep-awake:
`supervise.keepawake_s`, `keepawake_button`) so the HOME menu never
auto-sleeps — sys-botbase cannot wake a sleeping console. ZR does nothing on
HOME, and nothing is ever sent while the rig answers. After 60 s of silence
it:

1. runs the launch sequence. An application or hbmenu screen while the rig
   is silent means a deaf slot: HOME suspends it, and on HOME the tool parks
   the cursor on tile 0 (the suspended application), presses X and, if the
   "Close software" prompt appears, A to close it. With nothing suspended,
   X does nothing. Then it selects the tile and presses A; if the update
   prompt appears it presses DUP (to "Start Software") and A, and A again on
   the profile picker. (With
   `twili.enabled = true` it first asks twili for the hbl process and
   terminates a deaf one; twili unreachable is noted and ignored.) On
   success it goes back to watching;
2. on failure retries twice more (60 s, then 120 s later), then logs
   `NEEDS A HUMAN ...` and keeps pinging every 30 s, so your own launch is
   noticed. After 30 min (`supervise.human_rearm_s`) it tries again.

If the console is asleep (port 6000 accepts but `getVersion` times out), does
not answer at all, or sys-botbase does not answer while the console does, it
does not retry: it logs one `NEEDS A HUMAN` line with
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

- **A sleeping console** (`NEEDS A HUMAN (asleep)`): sys-botbase cannot wake
  it. Press HOME or POWER; the supervisor resumes by itself. Keep-awake and
  auto-sleep = Never should prevent this.
- **A kernel fatal or a powered-off console.** Nothing on the host can press
  POWER. The supervisor says `NEEDS A HUMAN (console_down)`; after the
  console is on again (and sys-botbase started at boot, i.e. installed with
  `--boot`), it resumes by itself.
- **sys-botbase not running** (`botbase_down`): not installed, installed
  without `--boot`, or crashed. Install with `--boot` and reboot (or, while
  twili still runs, `bin/coldstart start-sysmodule`).
- **The launch failing repeatedly** (`launch_failed`): the screenshots in the
  run directory show where it got stuck (an unexpected dialog, a tile that is
  not the game, a theme change that needs new references).
- **An application the HOME menu's close prompt does not close** (or, with
  twili enabled, a process twili cannot terminate).
- Settings that keep the console reachable — HOME-menu auto-sleep set to
  Never, power, Wi-Fi — are outside this tool.
- **Captures refused on some screens** count as `unknown`; if the album
  fills up or sys-ftpd is down, screenshots stop and launches run blind.

## Hardware notes (2026-10-19)

From the first run against the real console (firmware 20.2.0, Atmosphère
1.11.2, emuMMC):

- **X on HOME** with a suspended application opens "Close the software you
  are currently using?" with Cancel and Close, the cursor on Close: A closes
  it. Confirmed.
- **CAPTURE through sys-botbase** works on the HOME menu and on its dialogs,
  but is refused ("Unable to take a screenshot", no album file) while a
  homebrew application that has not presented a frame, or applet-mode
  homebrew, is in front. Those screens classify as `unknown` (HOME is then
  pressed).
- **Cold launch time:** the first cold launch took 73 s from the tile's A
  until the rig's waiter answered; `launch.wait_rig_s` (120 s) covers it.
- **Error 2123-0011** ("restart the console"): seen twice, both times when
  the tile was started right after closing an application that was hung
  (13:09 on 2026-10-19, 00:47 on 2026-10-20); the closed process was
  evidently still terminating. Nothing needs restarting: OK on the dialog and
  a second launch a minute later worked both times, which is what the
  supervisor's next attempt does. (This note first blamed the update
  prompt below; that was wrong.)
- **Update prompt:** A on the tile can show "A new update is available. It
  will be downloaded now." (Start Software above Cancel and Download, cursor
  on Download), and A there starts a download. The tool recognises
  `update-prompt` and goes DUP, A; blind mode no longer sends a second A.
  The prompt classified as `close-prompt` at 5.2 before it had its own
  reference, hence the ambiguity rule.
- **emuMMC album:** captures land in `/emuMMC/RAW1/Nintendo/Album`
  (`/emummc/emummc.ini`: `nintendo_path=emuMMC/RAW1/Nintendo`), not
  `/Nintendo/Album`; `album_root = "auto"` finds it.
- **Clicks:** sys-botbase's default 50 ms clicks register on the HOME menu
  and its dialogs; `botbase.click_hold_ms` can stay 0.
- The album holds a far-future (2051) file from an earlier clock jump; its
  "sorts after the new image" note is logged once per run.

## Development

```
make check     # byte-compile, unit + scenario tests (~3 min), shellcheck if installed
```

Everything is tested against `sim/botbase_sim.py`, a fake console speaking
the sys-botbase subset with HOME / dialog / user-select / game / waiter /
close-prompt / update-prompt / asleep / off screens, a small FTP server
over an SD-card directory whose album receives CAPTURE images (with a
skewable console clock; `--emummc` redirects it like the rig's console), a fake rig responder, and fake `twib`/`twibd` in `sim/bin`.
To try the tool by hand without a console:

```
python3 sim/botbase_sim.py --botbase-port 16000 --rig-port 15600 --ftp-port 15000 \
    --ctl-port 16099 --screen dialog &
PATH=$PWD/sim/bin:$PATH COLDSTART_SIM_CTL=127.0.0.1:16099 COLDSTART_HOST=127.0.0.1 \
    COLDSTART_BOTBASE_PORT=16000 COLDSTART_RIG_PORT=15600 COLDSTART_FTP_PORT=15000 \
    COLDSTART_TWILI_PORT=15000 bin/coldstart launch
```

DESIGN.md describes the state machine, the failure handling and the
decisions behind them.

## License

switch-coldstart is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See `LICENSE`.

`reference/sys-botbase-src/` and `reference/sys-botbase-2.5/` are copied from
[olliz0r/sys-botbase](https://github.com/olliz0r/sys-botbase) and remain under
that project's GPL-3.0 license. The binary in `reference/sys-botbase-2.5/` is
the unmodified v2.5 release; its complete source is the v2.5 tag of that
repository, and `make sysmodule` builds from the revision pinned in
`scripts/build-sysmodule.sh`.
