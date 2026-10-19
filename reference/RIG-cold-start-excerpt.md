## Cold start without a button

The loop restarts itself as long as the application slot lives. When it
does not (the process died, or the console rebooted), somebody has to launch
the hbl title again. Two pieces are installed on the card to shrink that to
"press A on a tile", and a third is proposed:

- **hbl boots into the waiter.** `sdmc:/hbmenu.nro` is the rig waiter; the
  real menu is `sdmc:/hbmenu.real.nro` (the waiter's `+` button and the
  game's `hbmenu` exit chain to it). Starting hbl therefore starts the loop.
- **No R to hold.** `override_config.ini` has `override_any_app_key=!R`:
  launching any application WITHOUT R gives hbl, holding R gives the real
  game. (Backup of the original config: `override_config.ini.orig` in the
  session scratch; the only line that changed is that key.)
- **twili** (`twib -P <sock> connect-tcp <ip>`, after `twibd --unix -P
  <sock>`) can `ps` and `terminate` the dead process from the host.
  `twib launch <application id> <storage>` cannot start an application
  (pgl, not ns): every storage id fails, and `host` took the console down.
- **Proposed: sys-botbase** (sysmodule `430000000000000B`, TCP 6000) to
  press the buttons from the host: `pad.py home-launch` goes HOME, selects
  the leftmost tile and presses A; `pad.py shot` returns a screenshot
  (Atmosphere `enable_am_debug_mode=1` is set). It can be started through
  twili (`twib launch 430000000000000B none`) without a reboot, so its
  compatibility with this firmware is proven before any `boot2.flag` makes
  it permanent. Installing a third-party sysmodule with a boot flag is the
  owner's call; nothing of it is on the card yet.

A kernel fatal still ends everything, twili included: POWER is human.

## What still needs a human
