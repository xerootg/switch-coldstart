"""The supervisor's state machine on a fake clock (no sockets, no sleeping)."""
import io
import unittest

from coldstart import config, rig
from coldstart.botbase import BotbaseError
from coldstart.launch import ASLEEP, BOTBASE_DOWN, LAUNCH_FAILED, LaunchFailure, LaunchResult
from coldstart.runlog import NullLog
from coldstart.supervisor import BACKOFF, HUMAN, WATCH, Supervisor
from coldstart.twili import Process, TwiliUnavailable

HBL = 0x01007EF00011E000


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeLauncher:
    def __init__(self, outcomes, rig_state):
        self.outcomes = outcomes
        self.rig_state = rig_state

    def run(self):
        o = self.outcomes.pop(0)
        if isinstance(o, LaunchFailure):
            raise o
        self.rig_state["up"] = True
        return LaunchResult(o, 1.0, rig.parse_reply(f"pong role={o}"))


class FakeTwili:
    def __init__(self, procs=(), unavailable=False):
        self.procs = list(procs)
        self.unavailable = unavailable
        self.terminated = []

    def find(self, tid):
        if self.unavailable:
            raise TwiliUnavailable("no twibd")
        return [p for p in self.procs if p.title_id == tid]

    def terminate(self, pid, wait_s):
        self.terminated.append(pid)
        self.procs = [p for p in self.procs if p.pid != pid]


class Harness(Supervisor):
    def __init__(self, outcomes, twili=None, botbase_back=False, **sup):
        cfg = config.load(None, env={"COLDSTART_HOST": "192.0.2.1"})  # TEST-NET, never the console
        cfg.supervise._values.update({"poll_s": 5.0, "silent_s": 60.0, "attempts": 3,
                                      "backoff_s": [60.0, 120.0], "human_poll_s": 30.0,
                                      "human_rearm_s": 1800.0, **sup})
        cfg.notify._values["webhook"] = ""
        self.out = io.StringIO()
        self.clock_ = Clock()
        self.rig_state = {"up": False}
        self.botbase_back = botbase_back
        self.keepawake_clicks = []
        self.keepawake_error = None
        super().__init__(cfg, NullLog(self.out), twili=twili,
                         launcher_factory=lambda: FakeLauncher(outcomes, self.rig_state),
                         clock=self.clock_, sleep=self.clock_.sleep)

    def ping(self):
        return rig.parse_reply("pong role=game up=1") if self.rig_state["up"] else None

    def botbase_answers(self):
        return self.botbase_back

    def keepawake_click(self):          # never a real socket from unit tests
        self.keepawake_clicks.append(self.clock_.t)
        if self.keepawake_error:
            raise BotbaseError(self.keepawake_error)

    @property
    def text(self):
        return self.out.getvalue()


def soft(msg="no rig answer"):
    return LaunchFailure(LAUNCH_FAILED, msg)


class SupervisorTests(unittest.TestCase):
    def test_waits_silent_s_before_acting(self):
        h = Harness(["game"])
        rc = h.run(max_runtime=59, exit_on={"recovered"})
        self.assertEqual(rc, 0)
        self.assertNotIn("recovery attempt", h.text)
        h2 = Harness(["game"])
        self.assertEqual(h2.run(max_runtime=200, exit_on={"recovered"}), 0)
        self.assertIn("recovery attempt 1/3", h2.text)
        self.assertGreaterEqual(h2.clock_.t - 1000.0, 60.0)

    def test_backoff_schedule_then_human(self):
        h = Harness([soft(), soft(), soft()])
        rc = h.run(max_runtime=10_000, exit_on={"human"})
        self.assertEqual(rc, 3)
        self.assertIn("next attempt in 60 s", h.text)
        self.assertIn("next attempt in 120 s", h.text)
        self.assertIn("NEEDS A HUMAN (launch_failed): the launch failed 3 times", h.text)
        self.assertEqual(h.state, HUMAN)
        # 60 s silence + 60 + 120 s backoff
        self.assertAlmostEqual(h.clock_.t - 1000.0, 240.0, delta=1)

    def test_hard_failure_is_not_retried_and_botbase_return_rearms(self):
        h = Harness([LaunchFailure(BOTBASE_DOWN, "port 6000 refused"), "waiter"])
        self.assertEqual(h.run(max_runtime=200, exit_on={"human"}), 3)
        self.assertEqual(h.text.count("recovery attempt"), 1)
        h.botbase_back = True
        self.assertEqual(h.run(max_runtime=200, exit_on={"recovered"}), 0)
        self.assertIn("sys-botbase answers again", h.text)
        self.assertIn("recovered: rig answers as waiter", h.text)
        self.assertEqual(h.state, WATCH)

    def test_rearm_after_human_rearm_s(self):
        h = Harness([soft(), soft(), soft(), "game"], backoff_s=[1.0])
        self.assertEqual(h.run(max_runtime=500, exit_on={"human"}), 3)
        t_human = h.clock_.t
        self.assertEqual(h.run(max_runtime=5000, exit_on={"recovered"}), 0)
        self.assertIn("no human for 1800 s: trying again", h.text)
        self.assertGreaterEqual(h.clock_.t - t_human, 1800)

    def test_rig_answer_during_backoff_returns_to_watch(self):
        h = Harness([soft()])
        h.run(max_runtime=70, exit_on={"recovered"})
        self.assertEqual(h.state, BACKOFF)
        h.rig_state["up"] = True
        self.assertEqual(h.run(max_runtime=10, exit_on={"recovered"}), 0)
        self.assertIn("rig answers again as game while waiting to retry", h.text)

    def test_deaf_slot_terminated_before_launch(self):
        tw = FakeTwili([Process(0x97, 0, HBL, "hbloader", "0x77"), Process(0x83, 0, 1, "ftpd", "")])
        h = Harness(["game"], twili=tw)
        self.assertEqual(h.run(max_runtime=200, exit_on={"recovered"}), 0)
        self.assertEqual(tw.terminated, [0x97])
        self.assertIn("deaf slot, terminating", h.text)

    def test_twili_unavailable_is_noted(self):
        h = Harness(["game"], twili=FakeTwili(unavailable=True))
        self.assertEqual(h.run(max_runtime=200, exit_on={"recovered"}), 0)
        self.assertIn("twili unreachable (no twibd); going on with the launch", h.text)

    def test_heartbeat_is_rate_limited(self):
        h = Harness([], heartbeat_s=600.0)
        h.rig_state["up"] = True
        h.run(max_runtime=1500)
        self.assertEqual(h.text.count("heartbeat"), 3)   # t=0, 600, 1200


    def test_keepawake_while_silent_only(self):
        h = Harness([], silent_s=10_000.0, keepawake_s=120.0)
        h.run(max_runtime=500)
        self.assertEqual([t - 1000.0 for t in h.keepawake_clicks], [0.0, 120.0, 240.0, 360.0, 480.0])
        self.assertIn("keep-awake: click ZR", h.text)
        h2 = Harness([], keepawake_s=120.0)
        h2.rig_state["up"] = True                      # the game answers: never
        h2.run(max_runtime=500)
        self.assertEqual(h2.keepawake_clicks, [])

    def test_keepawake_disabled_and_errors(self):
        h = Harness([], silent_s=10_000.0, keepawake_s=0.0)
        h.run(max_runtime=500)
        self.assertEqual(h.keepawake_clicks, [])
        h = Harness([], silent_s=10_000.0)
        h.keepawake_error = "no reply within 5 s"
        h.run(max_runtime=130)
        self.assertEqual(len(h.keepawake_clicks), 2)
        self.assertIn("keep-awake: could not click ZR (no reply within 5 s)", h.text)

    def test_no_keepawake_while_asleep_and_resume_on_wake(self):
        h = Harness([LaunchFailure(ASLEEP, "getVersion times out"), "game"])
        self.assertEqual(h.run(max_runtime=200, exit_on={"human"}), 3)
        self.assertIn("NEEDS A HUMAN (asleep)", h.text)
        self.assertIn("press HOME or POWER", h.text)
        n = len(h.keepawake_clicks)
        h.run(max_runtime=1000)                         # still asleep: no input at all
        self.assertEqual(len(h.keepawake_clicks), n)
        h.botbase_back = True                           # a human pressed HOME
        self.assertEqual(h.run(max_runtime=200, exit_on={"recovered"}), 0)

    def test_keepawake_button_validation(self):
        for bad in ("R", "A", "HOME", "CAPTURE", "NOPE"):
            d = config.load(None, env={})
            d.data["supervise"]["keepawake_button"] = bad
            with self.assertRaisesRegex(config.ConfigError, "keepawake_button"):
                config._validate(d.data)


if __name__ == "__main__":
    unittest.main()
