"""End-to-end scenarios (SPEC section 6): the real CLI and supervisor against
botbase_sim, the fake twib/twibd on PATH and a local webhook receiver."""
from __future__ import annotations

import http.server
import json
import signal
import subprocess
import threading
import unittest

from harness import Sim, shared_refs, stop

SUPERVISE = ("supervise", "--exit-on", "recovered", "--exit-on", "human", "--max-runtime", "40")


class Webhook:
    def __init__(self):
        self.bodies = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(json.loads(self.rfile.read(n)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = shared_refs()

    def sim(self, *args, **kw):
        kw.setdefault("refs_dir", self.refs)
        s = Sim(*args, **kw)
        self.addCleanup(s.close)
        return s

    def assertIn_(self, needle, text):
        self.assertIn(needle, text, msg=f"\n--- output ---\n{text}")

    # --- SPEC 6 scenarios ----------------------------------------------------

    def test_dead_process_is_launched(self):
        sim = self.sim("--screen", "home")
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out)
        self.assertIn_("twili disabled", out)
        self.assertIn_("shot 01-screen-0.jpg: home", out)
        self.assertIn_("close-check.jpg: home", out)     # X with nothing suspended: harmless
        self.assertIn_("no suspended application", out)
        self.assertIn_("recovered: rig answers as game", out)
        st = sim.state()
        self.assertEqual(st["screen"], "game")
        self.assertNotIn("press R", st["events"])
        self.assertEqual(sim.twib_calls(), [])

    def test_dead_process_with_twili(self):
        sim = self.sim("--screen", "home", extra={"twili": {"enabled": True}})
        r = sim.run(*SUPERVISE)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("no process with title 0x01007ef00011e000: the slot is dead", r.stdout)
        self.assertTrue(any(c.endswith(" ps") for c in sim.twib_calls()))

    def test_deaf_slot_without_twili_is_closed_from_home(self):
        sim = self.sim("--screen", "game", "--deaf")
        old_pid = sim.state()["app"]["pid"]
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out)
        self.assertIn_("screen-0.jpg: game", out)
        self.assertIn_("in the foreground although the rig is silent: deaf slot", out)
        self.assertIn_("close-check.jpg: close-prompt", out)
        self.assertIn_("after-close.jpg: home", out)
        self.assertIn_("recovered: rig answers as game", out)
        st = sim.state()
        ev = [e for e in st["events"] if not e.startswith("set ")]
        self.assertEqual(ev[0], "HOME")
        self.assertIn(f"closed 0x{old_pid:x}", ev)
        i = ev.index("X")
        self.assertEqual(ev[i + 1], "A")
        self.assertNotEqual(st["app"]["pid"], old_pid)
        self.assertEqual(sim.twib_calls(), [])

    def test_deaf_app_suspended_at_home_is_closed(self):
        sim = self.sim("--screen", "waiter", "--land", "waiter", "--deaf")
        old_pid = sim.state()["app"]["pid"]
        sim.set(screen="home")           # suspended behind HOME, deaf, rig silent
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("screen-0.jpg: home", r.stdout)
        self.assertIn_("an application is suspended while the rig is silent", r.stdout)
        self.assertIn_("ok: rig answers as waiter", r.stdout)
        self.assertIn(f"closed 0x{old_pid:x}", sim.state()["events"])

    def test_dialog_is_dismissed_then_launched(self):
        sim = self.sim("--screen", "dialog")
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out)
        self.assertIn_("shot 01-screen-0.jpg: dialog", out)
        self.assertIn_("shot 02-screen-1.jpg: home", out)
        self.assertIn_("recovered: rig answers as game", out)
        ev = [e for e in sim.state()["events"] if not e.startswith("set ")]
        self.assertEqual(ev[0], "A", ev)          # the dialog's A comes first
        self.assertNotIn("HOME", ev)              # nothing else was needed

    def test_deaf_slot_is_terminated_then_launched(self):
        sim = self.sim("--screen", "game", "--deaf", extra={"twili": {"enabled": True}})
        old_pid = sim.state()["app"]["pid"]
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out)
        self.assertIn_("deaf slot, terminating", out)
        self.assertIn_(f"terminated pid 0x{old_pid:x}", out)
        self.assertIn_("recovered: rig answers as game", out)
        st = sim.state()
        self.assertIn(f"terminate 0x{old_pid:x}", st["events"])
        self.assertNotEqual(st["app"]["pid"], old_pid)
        self.assertTrue(any(f"terminate 0x{old_pid:x}" in c for c in sim.twib_calls()))

    def test_asleep_gives_up_with_the_right_message(self):
        hook = Webhook()
        self.addCleanup(hook.close)
        sim = self.sim("--screen", "asleep", extra={"notify": {"webhook": hook.url}})
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 3, out)
        self.assertIn_("NEEDS A HUMAN (console_down): the console 127.0.0.1 does not answer at all", out)
        self.assertIn_("asleep, off, or on a fatal error screen", out)
        self.assertIn_("Next: wake or power on the console", out)
        self.assertEqual(out.count("recovery attempt"), 1, "a hard failure is not retried")
        self.assertEqual(len(hook.bodies), 1)
        self.assertEqual(hook.bodies[0]["kind"], "console_down")
        self.assertEqual(hook.bodies[0]["event"], "needs_human")

    def test_wrong_tile_gives_bounded_retries(self):
        sim = self.sim("--screen", "home", "--tiles", "other,app")
        r = sim.run(*SUPERVISE)
        out = r.stdout
        self.assertEqual(r.returncode, 3, out)
        self.assertEqual(out.count("recovery attempt"), 3, out)
        self.assertIn_("attempt 3/3 failed (launch_failed): no rig answer within 2 s", out)
        self.assertIn_("NEEDS A HUMAN (launch_failed): the launch failed 3 times", out)
        self.assertIn_("next attempt in 0.3 s", out)

    def test_sysmodule_missing_is_a_hard_failure(self):
        sim = self.sim("--screen", "home", "--no-botbase")
        r = sim.run("launch")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn_("launch FAILED (botbase_down): sys-botbase does not answer on 127.0.0.1:", r.stdout)
        self.assertIn_("the sysmodule is not installed or not started", r.stdout)
        self.assertIn_("coldstart start-sysmodule", r.stdout)
        r = sim.run(*SUPERVISE)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn_("NEEDS A HUMAN (botbase_down)", r.stdout)
        self.assertEqual(r.stdout.count("recovery attempt"), 1)

    # --- further behaviour ----------------------------------------------------

    def test_user_select_gets_a_second_a(self):
        sim = self.sim("--screen", "home", "--user-select")
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("after-a.jpg: user-select", r.stdout)
        self.assertIn_("ok: rig answers as game", r.stdout)

    def test_blind_never_presses_x(self):
        sim = self.sim("--screen", "home", refs_dir=None)
        self.assertEqual(sim.state()["screen"], "home")   # blind here: no X at all
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("blind: not pressing X", r.stdout)
        self.assertNotIn("X", sim.state()["events"])

    def test_blind_launch_without_references(self):
        sim = self.sim("--screen", "home", "--user-select", refs_dir=None)
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("every screen is unknown, launching blind", r.stdout)
        self.assertIn_("blind: HOME twice", r.stdout)
        self.assertIn_("ok: rig answers as game", r.stdout)

    def test_no_capture_falls_back_to_blind(self):
        sim = self.sim("--screen", "home", "--no-capture")
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("enable_am_debug_mode", r.stdout)
        self.assertIn_("continuing blind", r.stdout)

    def test_dry_run_touches_nothing(self):
        sim = self.sim("--screen", "home")
        r = sim.run("launch", "--dry-run", "--tile", "2")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("click DRIGHT x2", r.stdout)
        self.assertIn_("never: press/hold R", r.stdout)
        self.assertEqual([e for e in sim.state()["events"] if not e.startswith("set ")], [])

    def test_already_answering_rig_is_left_alone(self):
        sim = self.sim("--screen", "game")
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("rig already answers", r.stdout)
        self.assertEqual([e for e in sim.state()["events"] if not e.startswith("set ")], [])

    def test_twili_unreachable_goes_on_with_launch(self):
        sim = self.sim("--screen", "home", extra={"twili": {"enabled": True, "twib": "/nonexistent/twib"}})
        r = sim.run(*SUPERVISE)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("twili unreachable", r.stdout)
        self.assertIn_("recovered: rig answers as game", r.stdout)

    def test_terminate_failure_is_retried_then_needs_human(self):
        sim = self.sim("--screen", "game", "--deaf", env={"FAKE_TWIB_TERMINATE": "fail"}, extra={"twili": {"enabled": True}})
        r = sim.run(*SUPERVISE)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn_("could not terminate the deaf process", r.stdout)
        self.assertEqual(r.stdout.count("recovery attempt"), 3)

    def test_supervisor_resumes_when_console_wakes(self):
        sim = self.sim("--screen", "asleep")
        p = sim.spawn("supervise", "--exit-on", "recovered", "--max-runtime", "40")
        self.addCleanup(stop, p)
        sim.wait_for_output("NEEDS A HUMAN (console_down)")
        sim.set(screen="home")                     # a human pressed POWER
        rc = p.wait(timeout=40)
        out = sim.spawn_output()
        self.assertEqual(rc, 0, out)
        self.assertIn_("sys-botbase answers again: retrying the launch", out)
        self.assertIn_("recovered: rig answers as game", out)

    def test_supervisor_notices_a_human_launch(self):
        sim = self.sim("--screen", "home", "--tiles", "other,app")
        p = sim.spawn("supervise", "--exit-on", "recovered", "--max-runtime", "40")
        self.addCleanup(stop, p)
        sim.wait_for_output("NEEDS A HUMAN (launch_failed)")
        sim.set(screen="game")                     # the human launched it
        rc = p.wait(timeout=40)
        out = sim.spawn_output()
        self.assertEqual(rc, 0, out)
        self.assertIn_("rig answers again as game after a human stepped in", out)

    def test_heartbeat_while_rig_answers_and_twibd_reaped(self):
        sim = self.sim("--screen", "game")
        r = sim.run("supervise", "--max-runtime", "1.5")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.count("heartbeat: rig answers (role=game"), 1, r.stdout)
        self.assertIn_("max runtime 1.5 s reached", r.stdout)
        self.assertEqual(list(sim.tmp.glob("runs/*/twibd.sock")), [])

    def test_sigterm_stops_cleanly_and_reaps_twibd(self):
        sim = self.sim("--screen", "asleep", extra={"twili": {"enabled": True}})
        p = sim.spawn("supervise", "--max-runtime", "40")
        self.addCleanup(stop, p)
        sim.wait_for_output("NEEDS A HUMAN (console_down)")
        socks = list(sim.tmp.glob("runs/*/twibd.sock"))
        self.assertEqual(len(socks), 1)              # our own twibd is running
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=15), 0)
        self.assertIn_("interrupted; stopping (state HUMAN)", sim.spawn_output())
        self.assertFalse(socks[0].exists())
        left = subprocess.run(["pgrep", "-f", str(socks[0])], capture_output=True, text=True)
        self.assertEqual(left.stdout, "")

    def test_start_sysmodule_through_twili(self):
        sim = self.sim("--screen", "home", "--no-botbase")
        r = sim.run("start-sysmodule", "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("twili: launch 430000000000000B none", r.stdout)
        self.assertIn_("sys-botbase 2.5 answers", r.stdout)
        self.assertTrue(any("launch 430000000000000B none" in c for c in sim.twib_calls()))

    def test_status_reports_what_answers(self):
        r = self.sim("--screen", "game").run("status")
        self.assertIn_("twili: disabled (twili.enabled = false)", r.stdout)
        sim = self.sim("--screen", "game", extra={"twili": {"enabled": True}})
        r = sim.run("status")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn_("[launch]", r.stdout)
        self.assertIn_("rig (UDP", r.stdout)
        self.assertIn_("role=game", r.stdout)
        self.assertIn_("version 2.5", r.stdout)
        self.assertIn_("twili: hbl process pid=0x97", r.stdout)
        self.assertIn_("references in", r.stdout)

    def test_screenshots_are_saved_per_step(self):
        sim = self.sim("--screen", "dialog")
        r = sim.run("launch")
        self.assertEqual(r.returncode, 0, r.stdout)
        runs = list(sim.tmp.glob("runs/*-launch"))
        self.assertEqual(len(runs), 1)
        names = sorted(p.name for p in runs[0].glob("*.jpg"))
        self.assertEqual(names[:4], ["01-screen-0.jpg", "02-screen-1.jpg", "03-close-check.jpg",
                                     "04-tile-0.jpg"])
        self.assertTrue((runs[0] / "log.txt").read_text().count("\n") > 5)


if __name__ == "__main__":
    unittest.main()
