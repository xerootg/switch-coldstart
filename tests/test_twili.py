import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from coldstart.twili import Twili, TwiliError, TwiliUnavailable, parse_ps

SAMPLE = """\
Process ID | Result | Title ID           | Process Name | MMU Flags
0x83       | 0x0    | 0x420000000000000f | sys-ftpd     | 0x137
0x97       | 0x0    | 0x1007ef00011e000  | hbloader     | 0x77
"""
HBL = 0x01007EF00011E000


class ParsePs(unittest.TestCase):
    def test_sample(self):
        procs = parse_ps(SAMPLE)
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0].pid, 0x83)
        self.assertEqual(procs[0].name, "sys-ftpd")
        self.assertEqual(procs[1].title_id, HBL)   # leading zero dropped by twib
        self.assertEqual(procs[1].mmu_flags, "0x77")
        self.assertIn("pid=0x97", str(procs[1]))

    def test_decimal_ids_noise_and_blank(self):
        text = "garbage line\n\n151 | 0 | 0x1007ef00011e000 | hbloader\n--- | --- | --- | ---\n"
        procs = parse_ps(text)
        self.assertEqual([(p.pid, p.title_id) for p in procs], [(151, HBL)])
        self.assertEqual(parse_ps(""), [])

    def test_no_devices(self):
        with self.assertRaises(TwiliUnavailable):
            parse_ps("No devices were detected.\n")


FAKE_TWIB = """\
#!{python}
import os, sys
state = {state!r}
args = sys.argv[1:]
assert args[0] == "-P"
args = args[2:]
open(state + ".calls", "a").write(" ".join(args) + "\\n")
connected = os.path.exists(state + ".connected")
if args[0] == "connect-tcp":
    open(state + ".connected", "w").close(); sys.exit(0)
if args[0] == "ps":
    if not connected:
        print("No devices were detected."); sys.exit(1)
    gone = os.path.exists(state + ".gone")
    print({table!r} if not gone else {table!r}.splitlines()[0] + "\\n" + {table!r}.splitlines()[1])
    sys.exit(0)
if args[0] == "terminate":
    if os.environ.get("FAIL"):
        print("[ERROR] nope"); sys.exit(1)
    if not os.environ.get("STUCK"):
        open(state + ".gone", "w").close()
    sys.exit(0)
sys.exit(2)
"""


class TwiliWrapper(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="coldstart-twili-"))
        self.state = str(self.d / "state")
        self.twib = self.d / "twib"
        self.twib.write_text(FAKE_TWIB.format(python=sys.executable, state=self.state, table=SAMPLE))
        self.twib.chmod(self.twib.stat().st_mode | stat.S_IXUSR)
        self.sock = self.d / "twibd.sock"
        self.sock.touch()     # a "running" shared twibd

    def tearDown(self):
        for p in self.d.iterdir():
            p.unlink()
        self.d.rmdir()
        os.environ.pop("FAIL", None)
        os.environ.pop("STUCK", None)

    def tw(self, **kw):
        return Twili(str(self.twib), "twibd-unused", "192.0.2.1", socket_path=str(self.sock), **kw)

    def calls(self):
        return Path(self.state + ".calls").read_text().split("\n")[:-1]

    def test_connects_once_when_no_devices(self):
        tw = self.tw()
        self.assertEqual([p.pid for p in tw.find(HBL)], [0x97])
        tw.ps()
        self.assertEqual(self.calls(), ["ps", "connect-tcp 192.0.2.1", "ps", "ps"])

    def test_terminate_confirms_with_ps(self):
        tw = self.tw()
        tw.terminate(0x97, wait_s=1)
        self.assertIn("terminate 0x97", self.calls())
        self.assertEqual(tw.find(HBL), [])

    def test_terminate_failure_and_stuck_process(self):
        tw = self.tw()
        os.environ["FAIL"] = "1"
        with self.assertRaisesRegex(TwiliError, "failed"):
            tw.terminate(0x97, wait_s=0.5)
        del os.environ["FAIL"]
        os.environ["STUCK"] = "1"
        with self.assertRaisesRegex(TwiliError, "still listed"):
            tw.terminate(0x97, wait_s=0.5)

    def test_unavailable(self):
        with self.assertRaisesRegex(TwiliUnavailable, "not found"):
            Twili("/nonexistent/twib", "x", "h", socket_path=str(self.sock)).ps()
        with self.assertRaisesRegex(TwiliUnavailable, "does not exist"):
            Twili(str(self.twib), "x", "h", socket_path=str(self.d / "nope.sock")).ps()
        own = Twili(str(self.twib), "/nonexistent/twibd", "h", run_dir=self.d)
        with self.assertRaisesRegex(TwiliUnavailable, "cannot start"):
            own.ps()
        own.close()

    def test_owned_twibd_started_and_stopped(self):
        twibd = self.d / "twibd"
        twibd.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import signal, socket, sys, os
            p = sys.argv[sys.argv.index("-P") + 1]
            s = socket.socket(socket.AF_UNIX); s.bind(p); s.listen(1)
            signal.signal(signal.SIGTERM, lambda *a: (os.unlink(p), sys.exit(0)))
            signal.pause()
            """))
        twibd.chmod(twibd.stat().st_mode | stat.S_IXUSR)
        run = self.d / "run"
        run.mkdir()
        tw = Twili(str(self.twib), str(twibd), "192.0.2.1", run_dir=run)
        try:
            self.assertEqual(len(tw.find(HBL)), 1)
            self.assertTrue((run / "twibd.sock").exists())
            proc = tw._daemon
        finally:
            tw.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse((run / "twibd.sock").exists())
        run.rmdir()


if __name__ == "__main__":
    unittest.main()
