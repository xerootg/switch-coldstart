"""The supervisor: watch the rig, bring it back when it goes silent.

States (DESIGN.md has the diagram and the reasoning):

    WATCH      ping every poll_s; silent for silent_s -> RECOVER
    RECOVER    one attempt: twili if enabled (terminate a deaf hbl), then launch
               (the launch itself closes a suspended app via HOME, X, A)
               ok -> WATCH; hard failure -> HUMAN; soft failure -> BACKOFF,
               or HUMAN after `attempts` attempts
    BACKOFF    keep pinging every poll_s until the next attempt is due

While the rig is silent in WATCH, BACKOFF and HUMAN(launch_failed), a
harmless button (ZR) is clicked every keepawake_s so the HOME menu does not
auto-sleep; never while the rig answers.
    HUMAN      ping every human_poll_s; rig answers -> WATCH; sys-botbase
               answers again after a hard failure -> RECOVER; human_rearm_s
               after "failed N times" -> RECOVER

In every state a rig answer returns to WATCH.
"""
from __future__ import annotations

import time

from . import rig
from .botbase import Botbase, BotbaseError
from .launch import HARD, LAUNCH_FAILED, NEXT_STEP, Launcher, LaunchFailure
from .probe import notify
from .twili import TwiliError, TwiliUnavailable

WATCH, RECOVER, BACKOFF, HUMAN = "WATCH", "RECOVER", "BACKOFF", "HUMAN"
EXIT_OK, EXIT_HUMAN = 0, 3


class _Stop(Exception):
    pass


class Supervisor:
    def __init__(self, cfg, runlog, twili=None, launcher_factory=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.cfg = cfg
        self.S = cfg.supervise
        self.runlog = runlog
        self.log = runlog.log
        self.twili = twili
        self.launcher_factory = launcher_factory or (lambda: Launcher(cfg, runlog))
        self.clock = clock
        self._sleep = sleep
        self.state = WATCH
        self.attempt = 0
        self.human_kind: str | None = None
        self.human_since = 0.0
        self.next_attempt_at = 0.0
        self.last_answer: float | None = None
        self.last_role: str | None = None
        self.last_heartbeat = -1e18
        self.last_keepawake = -1e18
        self.silence_logged = False
        self.deadline: float | None = None
        self.started = clock()
        self.events: list[str] = []

    # --- helpers ---------------------------------------------------------

    def _set(self, state: str, why: str) -> None:
        if state != self.state:
            self.log(f"state {self.state} -> {state}: {why}")
            self.state = state

    def wait(self, seconds: float) -> None:
        if self.deadline is not None:
            left = self.deadline - self.clock()
            if left <= 0:
                raise _Stop()
            seconds = min(seconds, left)
        if seconds > 0:
            self._sleep(seconds)

    def ping(self) -> rig.Reply | None:
        return rig.ping(self.cfg.console.host, self.cfg.rig.port, self.cfg.rig.ping_timeout_s)

    def botbase_answers(self) -> bool:
        c = self.cfg
        try:
            with Botbase(c.console.host, c.botbase.port, c.botbase.timeout_s) as bb:
                bb.get_version()
            return True
        except BotbaseError:
            return False

    def _backoff(self, attempt: int) -> float:
        b = self.S.backoff_s
        return b[min(attempt - 1, len(b) - 1)]

    # --- events --------------------------------------------------------------

    def on_answer(self, reply: rig.Reply, now: float) -> None:
        role = reply.role or "?"
        if self.state != WATCH:
            who = "after a human stepped in" if self.state == HUMAN else "while waiting to retry"
            self.log(f"rig answers again as {role} {who} ({rig.describe(reply)})")
            self._set(WATCH, "rig answers")
            self.events.append("recovered")
            self.attempt = 0
            self.human_kind = None
        elif self.last_answer is None or self.silence_logged or role != self.last_role \
                or now - self.last_heartbeat >= self.S.heartbeat_s:
            self.log(f"heartbeat: rig answers ({rig.describe(reply)})")
            self.last_heartbeat = now
        self.last_answer = now
        self.last_role = role
        self.silence_logged = False

    def needs_human(self, kind: str, message: str) -> None:
        nxt = NEXT_STEP[kind]
        self.log(f"NEEDS A HUMAN ({kind}): {message}. Next: {nxt}")
        notify(self.cfg, self.log, "needs_human", message, nxt, kind=kind,
               run_dir=str(self.runlog.dir) if self.runlog.dir else "")
        self.human_kind = kind
        self.human_since = self.clock()
        self.events.append("human")
        self._set(HUMAN, kind)

    def recover(self) -> None:
        """One recovery attempt. Leaves the state at WATCH, BACKOFF or HUMAN."""
        self.attempt += 1
        n = self.S.attempts
        self.log(f"recovery attempt {self.attempt}/{n}")
        failure = self._check_slot()
        if failure is None:
            launcher = self.launcher_factory()
            try:
                result = launcher.run()
            except LaunchFailure as e:
                failure = e
            else:
                self.log(f"recovered: rig answers as {result.role} after {result.elapsed:.1f} s")
                self.events.append("recovered")
                self.last_answer = self.clock()
                self.last_role = result.role
                self.last_heartbeat = self.last_answer
                self.attempt = 0
                self._set(WATCH, "launched")
                return
        self.last_keepawake = self.clock()   # the launch pressed buttons
        self.log(f"attempt {self.attempt}/{n} failed ({failure.kind}): {failure.message}")
        if failure.hard:
            self.needs_human(failure.kind, failure.message)
        elif self.attempt >= n:
            self.needs_human(LAUNCH_FAILED,
                             f"the launch failed {self.attempt} times; last: {failure.message}")
        else:
            delay = self._backoff(self.attempt)
            self.next_attempt_at = self.clock() + delay
            self._set(BACKOFF, f"next attempt in {delay:g} s")

    def _check_slot(self) -> LaunchFailure | None:
        """Terminate a deaf hbl process. Returns a failure, or None to launch."""
        if self.twili is None:
            self.log("twili disabled: a deaf or suspended application is closed through the "
                     "HOME menu (HOME, X, A) during the launch")
            return None
        tid = int(self.cfg.twili.hbl_title_id, 16)
        try:
            procs = self.twili.find(tid)
        except TwiliUnavailable as e:
            self.log(f"twili unreachable ({e}); going on with the launch, which closes a "
                     "suspended application through the HOME menu")
            return None
        if not procs:
            self.log(f"twili: no process with title 0x{tid:016x}: the slot is dead")
            return None
        for p in procs:
            self.log(f"twili: {p} is alive but the rig does not answer: deaf slot, terminating")
            try:
                self.twili.terminate(p.pid, self.cfg.twili.terminate_wait_s)
            except (TwiliError, TwiliUnavailable) as e:
                return LaunchFailure(LAUNCH_FAILED, f"could not terminate the deaf process: {e}")
            self.log(f"twili: terminated pid 0x{p.pid:x}")
        return None

    def keepawake_click(self) -> None:
        c = self.cfg
        with Botbase(c.console.host, c.botbase.port, c.botbase.timeout_s) as bb:
            bb.get_version()
            bb.click(self.S.keepawake_button)

    def keepawake(self, now: float) -> None:
        """While the rig is silent (the console is at HOME, or about to be
        recovered), click a harmless button every keepawake_s so the HOME
        menu's idle timer never puts the console to sleep. Only called after
        a ping got no answer: never while the game (or waiter) answers."""
        if self.S.keepawake_s <= 0 or now - self.last_keepawake < self.S.keepawake_s:
            return
        self.last_keepawake = now
        btn = self.S.keepawake_button
        try:
            self.keepawake_click()
        except BotbaseError as e:
            self.log(f"keep-awake: could not click {btn} ({e})")
            return
        self.log(f"keep-awake: click {btn} (rig silent; keeps the HOME menu from auto-sleeping)")

    # --- loop ------------------------------------------------------------------

    def step(self) -> None:
        now = self.clock()
        reply = self.ping()
        if reply is not None:
            self.on_answer(reply, now)
            self.wait(self.S.poll_s)
            return
        if self.state in (WATCH, BACKOFF) or (self.state == HUMAN and self.human_kind == LAUNCH_FAILED):
            self.keepawake(now)
        if self.state == WATCH:
            since = self.last_answer if self.last_answer is not None else self.started
            silent = now - since
            if not self.silence_logged:
                what = "does not answer" if self.last_answer is None else "stopped answering"
                self.log(f"rig {what} on UDP {self.cfg.rig.port}; acting after {self.S.silent_s:g} s of silence")
                self.silence_logged = True
            if silent < self.S.silent_s:
                self.wait(min(self.S.poll_s, self.S.silent_s - silent))
                return
            self.log(f"rig silent for {silent:.0f} s")
            self._set(RECOVER, "silent too long")
        if self.state == BACKOFF:
            if now < self.next_attempt_at:
                self.wait(min(self.S.poll_s, self.next_attempt_at - now))
                return
            self._set(RECOVER, "backoff over")
        if self.state == RECOVER:
            self.recover()
            return
        if self.state == HUMAN:
            if self.human_kind in HARD and self.botbase_answers():
                self.log("sys-botbase answers again: retrying the launch")
                self.attempt = 0
                self._set(RECOVER, "sys-botbase back")
                return
            if (self.human_kind == LAUNCH_FAILED and self.S.human_rearm_s > 0
                    and now - self.human_since >= self.S.human_rearm_s):
                self.log(f"no human for {self.S.human_rearm_s:g} s: trying again")
                self.attempt = 0
                self._set(RECOVER, "rearm")
                return
            self.wait(self.S.human_poll_s)

    def run(self, max_runtime: float | None = None, exit_on: set[str] | None = None) -> int:
        exit_on = exit_on or set()
        self.started = self.clock()
        self.deadline = None if max_runtime is None else self.started + max_runtime
        c = self.cfg
        self.log(f"supervising {c.console.host}: rig UDP {c.rig.port}, poll {self.S.poll_s:g} s, "
                 f"act after {self.S.silent_s:g} s of silence, {self.S.attempts} attempts")
        try:
            while True:
                seen = len(self.events)
                self.step()
                for ev in self.events[seen:]:
                    if ev in exit_on:
                        self.log(f"exiting on '{ev}' as asked")
                        return EXIT_HUMAN if ev == "human" else EXIT_OK
        except _Stop:
            self.log(f"max runtime {max_runtime:g} s reached; stopping (state {self.state})")
            return EXIT_HUMAN if self.state == HUMAN else EXIT_OK
        except KeyboardInterrupt:
            self.log(f"interrupted; stopping (state {self.state})")
            return EXIT_OK
