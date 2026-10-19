"""coldstart -- bring the rig's homebrew loop back from the HOME menu.

Commands:
  status            effective config, and what answers (rig, sys-botbase, console, twili)
  supervise         watch the rig; relaunch when it stays silent; say when a human is needed
  launch            one launch: HOME -> tile -> A -> wait for the rig (--dry-run prints it)
  shot              screenshot through sys-botbase and classify it (or classify --file)
  learn NAME        record a reference screen (home, dialog, game, waiter, user-select, close-prompt)
  install           put sys-botbase on the SD card over FTP (prints the plan; acts only with --yes)
  start-sysmodule   fallback: start sys-botbase through twili without a reboot, check port 6000

Exit status: 0 ok, 1 failed, 2 usage/config error, 3 needs a human.
"""
from __future__ import annotations

import argparse
import signal
import sys
import tempfile
from pathlib import Path

from . import __version__, capture, config, rig
from .capture import CaptureRefused
from .botbase import Botbase, BotbaseError
from .classify import LABELS, Classifier, PillowMissing, pillow_available
from .launch import Launcher, LaunchFailure
from .probe import console_answers
from .runlog import NullLog, RunLog
from .supervisor import EXIT_HUMAN, Supervisor
from .twili import TwiliError, TwiliUnavailable, from_config as twili_from_config

EXIT_FAIL, EXIT_USAGE = 1, 2


def _runlog(cfg, name: str) -> RunLog:
    return RunLog(cfg.path(cfg.paths.runs_dir), name)


def cmd_status(cfg, args) -> int:
    print(cfg.dump(), end="")
    if args.no_probe:
        return 0
    c = cfg.console
    print("# what answers")
    r = rig.ping(c.host, cfg.rig.port, cfg.rig.ping_timeout_s)
    print(f"rig (UDP {cfg.rig.port}): " + (rig.describe(r) if r else "no answer"))
    try:
        with Botbase(c.host, cfg.botbase.port, cfg.botbase.timeout_s) as bb:
            print(f"sys-botbase (TCP {cfg.botbase.port}): version {bb.get_version()}")
    except BotbaseError as e:
        print(f"sys-botbase (TCP {cfg.botbase.port}): no answer ({e})")
    ok, how = console_answers(cfg)
    print(f"console {c.host}: " + ("answers, " if ok else "silent, ") + how)
    if not cfg.twili.enabled:
        print("twili: disabled (twili.enabled = false)")
    else:
        _status_twili(cfg)
    _status_refs(cfg)
    return 0


def _status_twili(cfg) -> None:
    with tempfile.TemporaryDirectory(prefix="coldstart-status-") as tmp:
        tw = twili_from_config(cfg, Path(tmp))
        try:
            procs = tw.find(int(cfg.twili.hbl_title_id, 16))
            print("twili: hbl process " + (", ".join(map(str, procs)) if procs else "not running"))
        except (TwiliUnavailable, TwiliError) as e:
            print(f"twili: unreachable ({e})")
        finally:
            tw.close()


def _status_refs(cfg) -> None:
    clf = Classifier.from_config(cfg)
    names = clf.ref_names()
    missing = [lab for lab in LABELS if lab not in {n for n in names} and
               not any(n.startswith(lab + "-") for n in names)]
    print(f"references in {clf.refs_dir}: " + (", ".join(names) if names else "none (launch runs blind)"))
    if names and missing:
        print(f"references missing: {', '.join(missing)}")
    print("Pillow: " + ("available" if pillow_available() else
                        "MISSING (python3 -m pip install --user pillow)"))


def cmd_launch(cfg, args) -> int:
    if args.dry_run:
        Launcher(cfg, NullLog()).run(args.tile, dry_run=True)
        return 0
    log = _runlog(cfg, "launch")
    try:
        if not args.force:
            r = rig.ping(cfg.console.host, cfg.rig.port, cfg.rig.ping_timeout_s)
            if r is not None:
                log.log(f"rig already answers ({rig.describe(r)}); nothing to do (--force to launch anyway)")
                return 0
        try:
            res = Launcher(cfg, log).run(args.tile)
        except LaunchFailure as e:
            log.log(f"launch FAILED ({e.kind}): {e.message}. Next: {e.next_step}")
            return EXIT_HUMAN if e.hard else EXIT_FAIL
        log.log(f"ok: rig answers as {res.role} after {res.elapsed:.1f} s; run dir {log.dir}")
        return 0
    finally:
        log.close()


def cmd_supervise(cfg, args) -> int:
    log = _runlog(cfg, "supervise")
    use_twili = cfg.twili.enabled and not args.no_twili
    tw = twili_from_config(cfg, log.dir, log=log.log) if use_twili else None
    try:
        sup = Supervisor(cfg, log, twili=tw)
        return sup.run(max_runtime=args.max_runtime, exit_on=set(args.exit_on or []))
    finally:
        if tw is not None:
            tw.close()
        log.close()


def _take_shot(cfg, log) -> tuple[bytes, str]:
    with Botbase(cfg.console.host, cfg.botbase.port, cfg.botbase.timeout_s,
                 cfg.botbase.shot_timeout_s) as bb:
        bb.get_version()
        return capture.from_config(cfg, log=log).capture(bb)


def cmd_shot(cfg, args) -> int:
    clf = Classifier.from_config(cfg)
    if args.file:
        data = Path(args.file).read_bytes()
        try:
            if not pillow_available():
                raise PillowMissing()
            print(f"{args.file}: {clf.classify(data).summary()}")
        except PillowMissing as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_USAGE
        return 0
    log = _runlog(cfg, "shot")
    try:
        data, origin = _take_shot(cfg, log.log)
    except (BotbaseError, CaptureRefused) as e:
        log.log(f"shot FAILED: {e}")
        return EXIT_FAIL
    log.log(f"captured from {origin}")
    out = Path(args.out) if args.out else log.shot_path("shot")
    out.write_bytes(data)
    try:
        log.log(f"saved {out}: {clf.classify(data).summary()}")
    except PillowMissing as e:
        log.log(f"saved {out}; not classified: {e}")
    log.close()
    return 0


def cmd_learn(cfg, args) -> int:
    clf = Classifier.from_config(cfg)
    if not pillow_available():
        print(f"error: {PillowMissing()}", file=sys.stderr)
        return EXIT_USAGE
    if args.file:
        data = Path(args.file).read_bytes()
    else:
        try:
            data, _ = _take_shot(cfg, lambda m: None)
        except (BotbaseError, CaptureRefused) as e:
            print(f"learn FAILED: {e}", file=sys.stderr)
            return EXIT_FAIL
    try:
        path = clf.learn(args.name, data)
    except (ValueError, OSError) as e:
        print(f"learn FAILED: {e}", file=sys.stderr)
        return EXIT_USAGE
    from .classify import label_of  # noqa: PLC0415
    lab = label_of(args.name)
    note = "" if lab in LABELS else " (not a name the supervisor uses; it classifies as itself)"
    print(f"learned {args.name} -> {path} (label {lab}){note}")
    return 0


def cmd_install(cfg, args) -> int:
    from .install import install  # noqa: PLC0415
    log = NullLog()
    return install(cfg, log.log, yes=args.yes and not args.dry_run, boot=args.boot, release=args.release)


def cmd_start_sysmodule(cfg, args) -> int:
    from .install import start_sysmodule  # noqa: PLC0415
    log = _runlog(cfg, "start-sysmodule")
    tw = twili_from_config(cfg, log.dir, log=log.log)
    try:
        return start_sysmodule(cfg, log.log, tw, wait_s=args.wait)
    finally:
        tw.close()
        log.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coldstart", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="TOML config file (default: $COLDSTART_CONFIG or ./coldstart.toml)")
    p.add_argument("--version", action="version", version=f"coldstart {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("status", help="effective config and what answers")
    s.add_argument("--no-probe", action="store_true", help="print the config only")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("supervise", help="watch the rig and relaunch it")
    s.add_argument("--max-runtime", type=float, help="stop after this many seconds")
    s.add_argument("--exit-on", action="append", choices=("recovered", "human"),
                   help="exit when this happens (repeatable; for scripts and tests)")
    s.add_argument("--no-twili", action="store_true",
                   help="do not use twili even if twili.enabled (the HOME-menu close path is used)")
    s.set_defaults(func=cmd_supervise)

    s = sub.add_parser("launch", help="one launch from whatever is on screen")
    s.add_argument("--tile", type=int, help="tile index (default: launch.tile)")
    s.add_argument("--dry-run", action="store_true", help="print the sequence, touch nothing")
    s.add_argument("--force", action="store_true", help="launch even if the rig answers")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("shot", help="take and classify a screenshot")
    s.add_argument("--file", help="classify this JPEG instead of taking a screenshot")
    s.add_argument("--out", help="save the screenshot here (default: a new run directory)")
    s.set_defaults(func=cmd_shot)

    s = sub.add_parser("learn", help="record a reference screen")
    s.add_argument("name", help="home, dialog, game, waiter, user-select, close-prompt (or e.g. home-2)")
    s.add_argument("--file", help="use this JPEG instead of taking a screenshot")
    s.set_defaults(func=cmd_learn)

    s = sub.add_parser("install", help="upload sys-botbase to the SD card (needs --yes)")
    s.add_argument("--yes", action="store_true", help="really do it")
    s.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    s.add_argument("--boot", action="store_true", help="also write flags/boot2.flag (start at boot)")
    s.add_argument("--release", action="store_true",
                   help="install the v2.5 release artifact from reference/ instead of a local build")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("start-sysmodule", help="fallback: start sys-botbase through twili (if twili runs), check port 6000")
    s.add_argument("--wait", type=float, default=90.0, help="seconds to wait for port 6000")
    s.set_defaults(func=cmd_start_sysmodule)
    return p


def _sigterm(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # SIGTERM (tmux kill, systemd, timeout) stops cleanly: twibd is reaped.
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.cmd == "launch" and args.tile is not None and args.tile < 0:
        print("error: --tile must be >= 0", file=sys.stderr)
        return EXIT_USAGE
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
