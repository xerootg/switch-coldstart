"""The launch sequence: whatever is on screen -> HOME -> tile -> A -> rig answers.

    1. connect to sys-botbase, getVersion (hard failure if port 6000 is dead)
    2. screenshot + classify until HOME: dialog -> A, anything else -> HOME
    3. DLEFT x dleft_presses, DRIGHT x tile, screenshot, A
    4. user-select -> A
    5. wait for the rig to answer a ping

Without references (or without Pillow, or when pixelPeek returns nothing) the
screens cannot be told apart, so the sequence runs "blind": HOME, HOME, tile,
A, A -- the prototype's sequence -- and only the rig's answer decides.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import rig
from .botbase import Botbase, BotbaseError, BotbaseUnavailable, CaptureUnavailable
from .classify import Classifier, PillowMissing, Result, UNKNOWN, pillow_available
from .probe import console_answers

CONSOLE_DOWN = "console_down"
BOTBASE_DOWN = "botbase_down"
LAUNCH_FAILED = "launch_failed"

NEXT_STEP = {
    CONSOLE_DOWN: ("wake or power on the console (press POWER; hold it ~12 s if a fatal error "
                   "screen is up); the supervisor resumes by itself once sys-botbase answers"),
    BOTBASE_DOWN: ("start sys-botbase: `coldstart start-sysmodule` (through twili, no reboot), or "
                   "install it first with `coldstart install --yes [--boot]`; right after a boot "
                   "it needs ~20 s before it listens"),
    LAUNCH_FAILED: ("look at the screenshots in the run directory and launch the tile by hand; "
                    "the supervisor notices the rig answering and resumes"),
}


class LaunchFailure(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def hard(self) -> bool:
        """Hard failures are not retried: the host cannot fix them."""
        return self.kind in (CONSOLE_DOWN, BOTBASE_DOWN)

    @property
    def next_step(self) -> str:
        return NEXT_STEP[self.kind]


@dataclass
class LaunchResult:
    role: str
    elapsed: float
    reply: rig.Reply


class Launcher:
    def __init__(self, cfg, log, classifier: Classifier | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.L = cfg.launch
        self.runlog = log
        self.log = log.log
        self.classifier = classifier if classifier is not None else Classifier.from_config(cfg)
        self.sleep = sleep
        self.bb: Botbase | None = None
        self.blind = False

    # --- helpers ---------------------------------------------------------

    def _click(self, btn: str, settle: float, quiet: bool = False) -> None:
        assert self.bb is not None
        try:
            self.bb.click(btn)
        except BotbaseUnavailable as e:
            raise LaunchFailure(LAUNCH_FAILED, f"sys-botbase connection lost during click {btn}: {e}") from None
        if not quiet:
            self.log(f"click {btn}" + (f" (settle {settle:g} s)" if settle else ""))
        if settle:
            self.sleep(settle)

    def shot(self, step: str) -> Result:
        """Screenshot, save, classify, log one line. Never raises for capture
        problems: they make the screen ``unknown`` (and the launch blind)."""
        assert self.bb is not None
        try:
            jpeg = self.bb.pixel_peek()
        except CaptureUnavailable as e:
            self.log(f"shot {step}: {e}")
            if not self.blind:
                self.log("screenshots unavailable: continuing blind")
                self.blind = True
            return Result(UNKNOWN, None, None, note="no screenshot")
        except BotbaseUnavailable as e:
            raise LaunchFailure(LAUNCH_FAILED, f"sys-botbase connection lost during pixelPeek: {e}") from None
        except BotbaseError as e:
            self.log(f"shot {step}: {e}")
            return Result(UNKNOWN, None, None, note="bad screenshot")
        path = self.runlog.shot_path(step)
        if path is not None:
            path.write_bytes(jpeg)
        name = path.name if path is not None else step
        if self.blind:
            res = Result(UNKNOWN, None, None, note="blind")
        else:
            try:
                res = self.classifier.classify(jpeg)
            except PillowMissing:
                res = Result(UNKNOWN, None, None, note="Pillow missing")
            except OSError as e:  # undecodable JPEG
                res = Result(UNKNOWN, None, None, note=f"cannot decode: {e}")
        self.log(f"shot {name}: {res.summary()}")
        return res

    def plan(self, tile: int) -> list[str]:
        L = self.L
        return [
            f"connect to sys-botbase {self.cfg.console.host}:{self.cfg.botbase.port}, getVersion",
            *([f"configure buttonClickSleepTime {self.cfg.botbase.click_hold_ms}"]
              if self.cfg.botbase.click_hold_ms > 0 else []),
            "screenshot + classify; until HOME (at most "
            f"{L.max_normalize_steps} steps): dialog -> click A (settle {L.settle_dialog_s:g} s), "
            f"anything else -> click HOME (settle {L.settle_home_s:g} s)",
            "  (blind, without references: click HOME, click HOME)",
            f"click DLEFT x{L.dleft_presses} (settle {L.settle_dpad_s:g} s each)",
            f"click DRIGHT x{tile} (settle {L.settle_dpad_s:g} s each)",
            "screenshot",
            f"click A (settle {L.settle_a_s:g} s), screenshot",
            f"if user-select (blind: always): click A (settle {L.settle_a_s:g} s)",
            f"ping the rig on UDP {self.cfg.rig.port} every {L.rig_poll_s:g} s for up to {L.wait_rig_s:g} s",
            "never: press/hold R",
        ]

    # --- the sequence ------------------------------------------------------

    def connect(self) -> str:
        c = self.cfg
        self.bb = Botbase(c.console.host, c.botbase.port, c.botbase.timeout_s, c.botbase.shot_timeout_s)
        try:
            self.bb.connect()
            version = self.bb.get_version()
        except BotbaseUnavailable as e:
            self.close()
            answers, how = console_answers(c)
            if answers:
                raise LaunchFailure(
                    BOTBASE_DOWN,
                    f"sys-botbase does not answer on {c.console.host}:{c.botbase.port} ({e}) although "
                    f"the console does ({how}): the sysmodule is not installed or not started") from None
            raise LaunchFailure(
                CONSOLE_DOWN,
                f"the console {c.console.host} does not answer at all (sys-botbase {c.botbase.port}: {e}; "
                f"{how}): it is asleep, off, or on a fatal error screen") from None
        except BotbaseError as e:
            self.close()
            raise LaunchFailure(BOTBASE_DOWN, f"sys-botbase answered strangely: {e}") from None
        if c.botbase.click_hold_ms > 0:
            self.bb.configure("buttonClickSleepTime", c.botbase.click_hold_ms)
        return version

    def close(self) -> None:
        if self.bb is not None:
            self.bb.close()
            self.bb = None

    def run(self, tile: int | None = None, dry_run: bool = False) -> LaunchResult:
        tile = self.L.tile if tile is None else tile
        if dry_run:
            self.log(f"dry run: launch sequence for tile {tile} (nothing is sent)")
            for i, step in enumerate(self.plan(tile), 1):
                self.log(f"  {i:2d}. {step}")
            return LaunchResult("dry-run", 0.0, rig.parse_reply("dry-run"))
        if not pillow_available():
            self.log(str(PillowMissing()) + " -- launching blind")
            self.blind = True
        elif not self.classifier.has_refs():
            self.log(f"no reference screens in {self.classifier.refs_dir}: every screen is unknown, "
                     "launching blind (record them with `coldstart learn <name>`)")
            self.blind = True
        t0 = time.monotonic()
        try:
            version = self.connect()
            self.log(f"sys-botbase {version} answers on {self.cfg.console.host}:{self.cfg.botbase.port}")
            self._to_home()
            self._select_and_start(tile)
        finally:
            self.close()
        return self._wait_rig(t0)

    def _to_home(self) -> None:
        L = self.L
        if not self.blind:
            for step in range(L.max_normalize_steps):
                r = self.shot(f"screen-{step}")
                if self.blind:
                    break
                if r.label == "home":
                    return
                if r.label == "dialog":
                    self._click("A", L.settle_dialog_s)
                else:
                    self._click("HOME", L.settle_home_s)
            if not self.blind:
                r = self.shot("screen-final")
                if r.label == "home":
                    return
                if not self.blind:
                    raise LaunchFailure(
                        LAUNCH_FAILED,
                        f"could not reach the HOME menu in {L.max_normalize_steps} steps "
                        f"(last screen: {r.summary()})")
        self.log("blind: HOME twice (the first HOME from an application only suspends it)")
        self._click("HOME", L.settle_home_s)
        self._click("HOME", L.settle_home_s)

    def _select_and_start(self, tile: int) -> None:
        L = self.L
        self.log(f"select tile {tile}: DLEFT x{L.dleft_presses}, DRIGHT x{tile}")
        for _ in range(L.dleft_presses):
            self._click("DLEFT", L.settle_dpad_s, quiet=True)
        for _ in range(tile):
            self._click("DRIGHT", L.settle_dpad_s, quiet=True)
        r = self.shot(f"tile-{tile}")
        if not self.blind and r.label != "home":
            self.log(f"warning: expected HOME with the cursor on tile {tile}, saw {r.label}")
        self._click("A", L.settle_a_s)
        r = self.shot("after-a")
        if self.blind:
            self._click("A", L.settle_a_s)    # profile picker, if any
        elif r.label == "user-select":
            self._click("A", L.settle_a_s)
            self.shot("after-user-select")
        elif r.label == "dialog":
            raise LaunchFailure(LAUNCH_FAILED, f"a dialog appeared after A on tile {tile}")

    def _wait_rig(self, t0: float) -> LaunchResult:
        c = self.cfg
        self.log(f"waiting up to {self.L.wait_rig_s:g} s for the rig on UDP {c.rig.port}")
        deadline = time.monotonic() + self.L.wait_rig_s
        while True:
            reply = rig.ping(c.console.host, c.rig.port, c.rig.ping_timeout_s)
            if reply is not None:
                elapsed = time.monotonic() - t0
                self.log(f"launched: rig answers as {reply.role or '?'} after {elapsed:.1f} s "
                         f"({rig.describe(reply)})")
                return LaunchResult(reply.role or "?", elapsed, reply)
            if time.monotonic() >= deadline:
                break
            self.sleep(self.L.rig_poll_s)
        last = ""
        try:
            self.connect()
            last = f" (screen now: {self.shot('no-rig').summary()})"
        except LaunchFailure as e:
            last = f" ({e.message})"
        finally:
            self.close()
        raise LaunchFailure(LAUNCH_FAILED, f"no rig answer within {self.L.wait_rig_s:g} s{last}")
