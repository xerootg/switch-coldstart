# switch-coldstart — specification

Host-side tooling that brings a homebrew loop on an Atmosphère Switch back
to life without a human: after the homebrew process died, after a kernel
fatal that someone cleared with POWER, or after a reboot, the console sits
at the HOME menu, and this tool presses the buttons from the host, verifies
what it sees, and hands the console back to the normal iteration rig.

It is the last missing piece of an unattended rig described in
`reference/RIG-cold-start-excerpt.md`: everything from "the game is
running" onward already restarts itself; what does not is "nothing is
running". The tool is used by one developer on one console, but must be
written as a proper small project: tests, docs, a clean CLI.

## The console and what is already there

- Nintendo Switch, firmware 20.2.0, Atmosphère 1.11.2. LAN address
  192.168.128.150 (configurable). Services on it today:
  - sys-ftpd on TCP 5000 (anonymous FTP, the SD card as `/`).
  - twili's TCP bridge on 15152 — **being removed (spec change
    2026-10-19, see below); optional from now on.** The host tool `twib`
    talks to it through
    `twibd`: `twibd --unix -P <sock>`, `twib -P <sock> connect-tcp <ip>`,
    then `twib -P <sock> ps` (a table: `Process ID | Result | Title ID |
    Process Name | MMU Flags`, ids in hex), `twib -P <sock> terminate
    <pid>`, `twib -P <sock> reboot`. `twib launch` cannot start
    applications and storage `host` takes the console down: never use it.
  - Atmosphère's GDB stub on 22225 (not needed here).
  - The rig's control port, UDP 5600, answered by whichever rig NRO runs:
    `ping` → `pong role=game ...` or `pong role=waiter ... hold=<0|1>
    countdown=<n> crashes=<n>`; `launch` makes the waiter start the game;
    `auto` re-enables automatic relaunch. No answer within a second means
    nothing of the rig is running (or the slot is "deaf", see below).
- hbl is installed with `override_any_app_key=!R`: launching ANY
  application tile WITHOUT holding R boots the homebrew loader instead of
  the application, and `sdmc:/hbmenu.nro` is the rig's waiter, which starts
  the game by itself. So "press A on an application tile" is a complete
  cold start. Holding R launches the real application; the tool never
  holds R.
- sys-botbase (olliz0r/sys-botbase, v2.5, program id 430000000000000B) is
  the HID-injection sysmodule this tool drives. It is NOT yet installed on
  the console; installing it is the owner's decision and the owner's
  action. Its protocol is one TCP connection on port 6000, text commands
  terminated by `\r\n`; the relevant ones (see `reference/sys-botbase-src/`
  for the authoritative list and syntax):
  - `click <BTN>`, `press <BTN>`, `release <BTN>`; buttons A B X Y L R ZL
    ZR PLUS MINUS HOME CAPTURE DUP DDOWN DLEFT DRIGHT LSTICK RSTICK.
  - `pixelPeek` → the current screen as a JPEG, hex-encoded, one line
    (needs capsrv's debug capture; it does NOT work on this console, see
    the album spec change below).
  - `getVersion` → the version string.
  - `configure` subcommands for key sleep/press durations.
  The normal install is the files plus `boot2.flag` and a reboot by the
  owner. While twili still runs, the sysmodule can also be started without
  a reboot (`twib launch 430000000000000B none`); that is a fallback only.
  `reference/pad.py` is a working prototype client.

### Spec change 2026-10-19: twili leaves the console

The console's system memory pool has 5 MiB free; sys-botbase needs about
5 MiB and twili holds 14 MiB. The owner approved migrating to sys-botbase:
twili's boot flag is disabled and sys-botbase starts at boot. Consequences:

- twili is optional: `twili.enabled = false` by default. When enabled and
  reachable it may still be used for `ps`/`terminate`; when disabled or
  unreachable, the supervisor uses the HOME-menu path below.
- Without twili, "is a homebrew process alive?" is answered by the rig port
  (silent) plus the screenshot (an application or hbmenu screen versus
  HOME).
- A deaf slot (process alive, rig port silent) is closed through
  sys-botbase: HOME (the application is suspended and HOME shows), then X
  (the HOME menu's "Close software" prompt for the suspended application),
  then A to confirm, with a screenshot and classification after each step;
  then the normal launch sequence. The classifier gains the reference name
  `close-prompt`; the simulator gains that screen (HOME with a suspended
  application, X → close-prompt, A → HOME with nothing suspended, B → back).
- `coldstart start-sysmodule` (twili launch) is a fallback only.

### Spec change 2026-10-19 (field finding): screenshots come from the album

`pixelPeek` does not work on this console: capsrv refuses the debug capture
(result 0xE0CE, module 206 capsrv, description 112) because the real set:sys
DebugModeFlag is false; Atmosphère's `enable_am_debug_mode` only fakes the
flag for am. Flipping the persistent debug-mode flag is off the table. The
screenshot source is therefore pluggable (`screenshot.source`):

- `album` (default): click CAPTURE through sys-botbase, wait
  (`capture_wait_s`, default 2.5 s), find the new JPEG under
  `/Nintendo/Album/` over FTP (sys-ftpd, port 5000, anonymous), download it,
  and delete it from the card (`delete_after`, default on). The console's
  clock is wrong (years 2025/2026/2051 all occur), so the host's date is
  never used: the year/month/day directories are walked and file names
  (which start with the console's timestamp, e.g.
  `/Nintendo/Album/2026/03/02/2026030221455100-3C381EA7....jpg`; sys-ftpd's
  NLST returns full paths) are compared. The album is listed before CAPTURE
  and a file that was not there before is required afterwards, so a failed
  capture is detected instead of re-reading an old image. A refused capture
  makes the screen `unknown` and is logged.
- `pixelpeek`: kept for consoles where it works; an empty reply means
  "unsupported" (the launch then runs blind).
- The simulator serves a minimal FTP server (stdlib) over an SD-card
  directory, and CAPTURE there writes the current screen into the album.

### Spec change 2026-10-19 (field finding): auto-sleep and keep-awake

The console auto-sleeps at the HOME menu about five minutes after boot, and
sys-botbase cannot wake it: while asleep the console surfaces on the network
for a few seconds every minute or so, port 6000 even accepts a TCP
connection, but commands time out. Therefore:

- This state is classified explicitly as `asleep` (sys-botbase accepts the
  connection but `getVersion` times out; ICMP/TCP flapping) and treated as
  "needs a human: press HOME or POWER", with the notification, instead of
  retrying the launch sequence. The supervisor resumes by itself when
  sys-botbase answers again. A console that refuses everything is still
  "does not answer at all" (off, or a fatal screen).
- Keep-awake: while the console is awake and the rig is silent (at HOME, or
  about to be recovered), the supervisor clicks a harmless button every
  `supervise.keepawake_s` (default 120 s) so the HOME menu's idle timer never
  expires. The button is configurable (`supervise.keepawake_button`, default
  `ZR`, which does nothing on the HOME menu; R, A, HOME and CAPTURE are
  refused), and `keepawake_s = 0` disables it. No keep-awake input is ever
  sent while the rig answers (in particular as `game`, which disables
  auto-sleep itself and must not receive stray input), nor while the console
  is asleep or down.
- The owner also sets auto-sleep to Never; keep-awake is the safety net.
- The simulator distinguishes `asleep` (port 6000 accepts, nothing answers,
  FTP and rig silent) from `off` (every port refuses).

## What the tool must do

### 1. Supervise (`coldstart supervise`)

A long-running loop on the host:

1. Poll the rig (UDP ping) every few seconds. While it answers, do nothing
   but log a heartbeat (rate-limited).
2. When it has not answered for `silent_s` (default 60 s):
   - If twili is enabled: ask it whether a homebrew process (the hbl title,
     default program id 01007ef00011e000, configurable) exists. If it does,
     the slot is "deaf" (alive but not answering); terminate it with twili,
     then launch. If twili is disabled or unreachable, note it and go on
     with the launch, which closes a deaf/suspended application through the
     HOME menu (HOME, X, A; see the spec change above).
   - Launch (section 2). On success, back to polling. On failure, retry
     with backoff (configurable, default 3 attempts over ~5 minutes), then
     declare "needs a human" (section 4) and keep polling at a slow rate
     so a human's launch is noticed.
3. Everything the loop does is logged with timestamps to stdout and to a
   run directory (`runs/<timestamp>/`), including every screenshot taken,
   named by step, so a failed night can be read in the morning.

### 2. Launch (`coldstart launch`)

The button sequence, with a screenshot and a classification after every
step that changes the screen:

1. Connect to sys-botbase; `getVersion` as a liveness check. If port 6000
   does not answer, this is a hard failure: say exactly that (sysmodule
   not installed, not started, or console asleep/off).
2. Screenshot, classify (section 3):
   - error dialog ("The software was closed because an error occurred" or
     any dialog): click A, screenshot again.
   - in an application or hbmenu or anything that is not HOME: HOME,
     settle, screenshot; if still not HOME, HOME again (the first HOME
     from a running application only suspends it).
   - HOME: proceed.
3. On HOME: park the cursor on the leftmost tile (the suspended
   application, if any) and press X; if the "Close software" prompt
   (`close-prompt`) appears, press A to close it and verify HOME. Then move
   the cursor to the configured tile (default: the leftmost
   application tile, `tile = 0`): DLEFT repeated enough times to be sure
   (12), then DRIGHT `tile` times, with settle delays; screenshot; click A.
4. If a user-select screen appears (profile picker), click A once more.
5. Wait for the rig to answer a ping (default 120 s). Success means `pong`
   from the waiter or the game. Report which, and how long it took.

Every delay is configurable; defaults should be generous (the HOME menu
animates). A dry-run flag prints the sequence without touching anything.

### 3. Screen classification (`coldstart shot`, `coldstart learn`)

No OCR. Classify a screenshot by comparing it to reference images the
owner records once with `coldstart learn <name>` (which takes a screenshot
and stores it under `refs/<name>.jpg` with an index). The classifier
downsamples both to a small grid (e.g. 16×9 mean colours) and uses a
distance with a threshold; it must cope with the HOME menu's clock and
icons changing, so compare a configurable crop or weight the grid, and
report the best match plus its distance so thresholds can be tuned from
the logs. Names the supervisor relies on: `home`, `dialog`, `game`,
`waiter`, `user-select`, `close-prompt`; anything else is `unknown`. Without references the
tool still runs, treats every screen as unknown, and says so.

Pillow may be used for JPEG decoding and the synthetic images in the
simulator; it is the only third-party dependency allowed, and the tool
must degrade gracefully (clear error) if it is missing.

### 4. Needs a human

Some states cannot be fixed from the host and the tool must say so plainly
instead of retrying forever: the console does not answer ICMP/TCP at all
(asleep, off, or in a fatal screen); sys-botbase does not answer while the
console does (sysmodule not running); the launch sequence fails N times.
Each is one clear log line with the next step for the human, and an
optional webhook/notification (a URL from the config, POST with a JSON
body; off by default).

### 5. Install helpers (`coldstart install`, `coldstart start-sysmodule`)

- `make sysmodule` clones sys-botbase at a pinned tag/commit into
  `sysmodule/src` and builds it with devkitPro (the host has devkitA64 and
  libnx under /opt/devkitpro); output `sysmodule/out/430000000000000B/
  exefs.nsp`. If devkitPro is missing, say so and stop.
- `coldstart install` uploads the sysmodule's files to the card over FTP
  into `/atmosphere/contents/430000000000000B/` and, only with `--boot`,
  writes `flags/boot2.flag` (start at boot). It prints exactly what it will
  do and does nothing without `--yes`. `--dry-run` is the default.
- `coldstart start-sysmodule` runs the twili launch above so the owner can
  try the sysmodule without a reboot, then checks port 6000. Fallback only:
  it needs twili, which is leaving the console; the normal path is
  `install --yes --boot` and a reboot by the owner.

### 6. Simulator and tests (`make check`)

A simulator so everything above is tested on the host with no console:

- `sim/botbase_sim.py`: a TCP server on a chosen port speaking the
  sys-botbase protocol subset above, driving a fake console with screens
  HOME / dialog / user-select / game / waiter / close-prompt / asleep, a
  cursor on the tile row, and rules (plus X on HOME with a suspended app →
  close-prompt, A there → HOME with nothing suspended, B → back): A on a tile → user-select (optional) → the "game"
  (which then starts a fake rig responder); A on the dialog → HOME; HOME
  from the game → HOME; DLEFT/DRIGHT move the cursor within bounds.
  `pixelPeek` returns a synthetic JPEG of the current screen (distinct
  colours/blocks per screen so the classifier has something to work with).
- A fake rig responder (UDP) and a fake `twib` (a script on PATH) with
  configurable behaviour (process present or not, terminate succeeds or
  fails).
- Scenario tests that run the real supervisor against the simulator
  (also: asleep → "press HOME or POWER", off → console down, keep-awake
  while silent and never while the game answers):
  dead process → launched; dialog → dismissed → launched; deaf slot →
  terminated → launched (with twili) and deaf slot → closed from HOME →
  launched (twili disabled); asleep → gives up with the right message; wrong
  tile → bounded retries; sysmodule missing → hard failure message.
- Unit tests for the protocol client, the twib output parser, the
  classifier and the config.

`make check` runs byte-compilation, the unit and scenario tests, and
`shellcheck` on any shell script if shellcheck is installed. It must pass
before every commit.

## Non-goals

No OCR, no machine learning, no GUI, no daemonisation beyond "run it in
tmux", no support for consoles without sys-botbase, no reimplementation of
the rig (it is reached only through its UDP port).

## Rules for the builder

- Work on `main`, commit directly, small focused commits, each one leaving
  the tree in a state where `make check` passes. Never commit code that
  does not work.
- Python 3.12+, standard library plus Pillow only. Shell scripts POSIX-ish
  bash, shellcheck-clean.
- Never touch the real console from this project's development: no FTP
  writes, no twili commands, no connections to port 6000. The simulator is
  the only target until the owner installs the sysmodule and runs it.
  Reading `reference/` is fine; it is all local.
- Commit messages explain the why; they end with the two attribution lines
  the builder was given.
- Write the README for the owner: what this is, how to build the
  sysmodule, how to install it (and that it is their call), how to record
  references, how to run the supervisor, what still needs a human.
