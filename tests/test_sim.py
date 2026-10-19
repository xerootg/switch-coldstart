"""The simulator itself: its rules are what the scenario tests rely on."""
import subprocess
import time
import unittest

from harness import SIM_BIN, Sim

from coldstart import rig
from coldstart.botbase import Botbase, BotbaseTimeout, port_answers


class SimRules(unittest.TestCase):
    def sim(self, *args):
        s = Sim(*args)
        self.addCleanup(s.close)
        return s

    def bb(self, sim):
        b = Botbase("127.0.0.1", sim.ports["botbase"], timeout=3)
        b.connect()
        self.addCleanup(b.close)
        return b

    def test_tile_row_user_select_and_game(self):
        sim = self.sim("--tiles", "app,other,app", "--user-select")
        b = self.bb(sim)
        for _ in range(5):
            b.click("DRIGHT")
        b.get_version()                       # replies are ordered after the clicks
        self.assertEqual(sim.state()["cursor"], 2)
        for _ in range(12):
            b.click("DLEFT")
        b.click("A")
        b.get_version()
        self.assertEqual(sim.state()["screen"], "user-select")
        self.assertIsNone(rig.ping("127.0.0.1", sim.ports["rig"], 0.2))
        b.click("A")
        b.get_version()
        st = sim.state()
        self.assertEqual(st["screen"], "game")
        time.sleep(0.3)                        # boot delay
        self.assertEqual(rig.ping("127.0.0.1", sim.ports["rig"], 0.5).role, "game")
        b.click("HOME")
        b.get_version()
        self.assertEqual(sim.state()["screen"], "home")
        self.assertIsNotNone(sim.state()["app"])   # suspended, not gone

    def test_close_prompt(self):
        sim = self.sim("--screen", "game")
        pid = sim.state()["app"]["pid"]
        b = self.bb(sim)
        b.click("X")                              # X in the game: nothing
        b.click("HOME")
        b.click("X")
        b.get_version()
        self.assertEqual(sim.state()["screen"], "close-prompt")
        b.click("B")                              # back out: still suspended
        b.get_version()
        st = sim.state()
        self.assertEqual((st["screen"], st["app"]["pid"]), ("home", pid))
        b.click("X")
        b.click("A")                              # close it
        b.get_version()
        st = sim.state()
        self.assertEqual((st["screen"], st["app"]), ("home", None))
        b.click("X")                              # nothing suspended: X does nothing
        b.get_version()
        self.assertEqual(sim.state()["screen"], "home")

    def test_dialog_a_and_unknown_button_is_a(self):
        sim = self.sim("--screen", "dialog")
        b = self.bb(sim)
        b.send("click NOSUCH")                    # sys-botbase presses A for unknown names
        b.get_version()
        self.assertEqual(sim.state()["screen"], "home")

    def test_pixel_peek_differs_per_screen(self):
        sim = self.sim()
        b = self.bb(sim)
        home = b.pixel_peek()
        sim.set(screen="game")
        game = b.pixel_peek()
        self.assertTrue(home.startswith(b"\xff\xd8") and game.startswith(b"\xff\xd8"))
        self.assertNotEqual(home, game)

    def test_off_closes_ports_asleep_accepts_but_ignores(self):
        sim = self.sim("--screen", "off")
        self.assertFalse(port_answers("127.0.0.1", sim.ports["botbase"], 0.5))
        self.assertFalse(port_answers("127.0.0.1", sim.ports["ftp"], 0.5))
        sim.set(screen="asleep")
        self.assertTrue(port_answers("127.0.0.1", sim.ports["botbase"], 0.5))
        self.assertFalse(port_answers("127.0.0.1", sim.ports["ftp"], 0.5))
        b = Botbase("127.0.0.1", sim.ports["botbase"], timeout=0.5)
        b.connect()
        self.addCleanup(b.close)
        with self.assertRaises(BotbaseTimeout):
            b.get_version()
        sim.set(screen="home")
        self.assertTrue(port_answers("127.0.0.1", sim.ports["botbase"], 0.5))
        self.assertTrue(port_answers("127.0.0.1", sim.ports["ftp"], 0.5))

    def test_no_botbase_keeps_ftp(self):
        sim = self.sim("--no-botbase")
        self.assertFalse(port_answers("127.0.0.1", sim.ports["botbase"], 0.5))
        self.assertTrue(port_answers("127.0.0.1", sim.ports["ftp"], 0.5))

    def test_fake_twib(self):
        sim = self.sim("--screen", "game", "--deaf")
        sock = sim.tmp / "twibd.sock"
        twibd = subprocess.Popen([str(SIM_BIN / "twibd"), "--unix", "-P", str(sock)])
        self.addCleanup(lambda: (twibd.terminate(), twibd.wait(5)))

        def twib(*a, **env):
            e = dict(sim.env, **env)
            return subprocess.run([str(SIM_BIN / "twib"), "-P", str(sock), *a], env=e,
                                  capture_output=True, text=True, timeout=10)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.02)
        self.assertIn("No devices were detected.", twib("ps").stdout)
        self.assertEqual(twib("connect-tcp", "127.0.0.1").returncode, 0)
        out = twib("ps").stdout
        self.assertIn("hbloader", out)
        self.assertNotIn("hbloader", twib("ps", FAKE_TWIB_PROCESS="absent").stdout)
        self.assertEqual(twib("terminate", "0x97", FAKE_TWIB_TERMINATE="fail").returncode, 1)
        self.assertEqual(twib("terminate", "0x97").returncode, 0)
        self.assertNotIn("hbloader", twib("ps").stdout)
        self.assertEqual(sim.state()["screen"], "home")
        twibd.terminate()
        twibd.wait(5)
        self.assertFalse(sock.exists())
        self.assertNotEqual(twib("ps").returncode, 0)


if __name__ == "__main__":
    unittest.main()
