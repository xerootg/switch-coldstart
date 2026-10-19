import ftplib
import io
import tempfile
import unittest
from pathlib import Path

from coldstart import config, install

REPO = Path(__file__).resolve().parent.parent


class FakeFTP:
    def __init__(self, existing=("/", "/atmosphere")):
        self.dirs = set(existing)
        self.files = {}
        self.ops = []

    def connect(self, host, port, timeout=None):
        self.ops.append(("connect", host, port))

    def login(self):
        self.ops.append(("login",))

    def cwd(self, d):
        if d not in self.dirs:
            raise ftplib.error_perm("550 no such directory")

    def mkd(self, d):
        self.ops.append(("mkd", d))
        self.dirs.add(d)

    def storbinary(self, cmd, fh):
        path = cmd.split(" ", 1)[1]
        self.files[path] = fh.read()
        self.ops.append(("stor", path))

    def size(self, path):
        return len(self.files[path])

    def quit(self):
        self.ops.append(("quit",))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(None, env={"COLDSTART_HOST": "192.0.2.9"})
        self.lines = []

    def test_plan_without_boot_flag(self):
        what, dirs, ups = install.plan(release=True, boot=False)
        self.assertIn("release", what)
        self.assertEqual([u.remote for u in ups],
                         ["/atmosphere/contents/430000000000000B/exefs.nsp"])
        self.assertEqual(ups[0].data, (REPO / "reference/sys-botbase-2.5/exefs.nsp").read_bytes())
        self.assertNotIn("/atmosphere/contents/430000000000000B/flags", dirs)

    def test_plan_with_boot_flag(self):
        _, dirs, ups = install.plan(release=True, boot=True)
        self.assertEqual(ups[1].remote, "/atmosphere/contents/430000000000000B/flags/boot2.flag")
        self.assertEqual(ups[1].data, b"")
        self.assertIn("/atmosphere/contents/430000000000000B/flags", dirs)

    def test_missing_local_build_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(install.InstallError, "make sysmodule.*--release"):
                install.choose_source(False, local=Path(d) / "exefs.nsp")
            nsp = Path(d) / "exefs.nsp"
            nsp.write_bytes(b"NSP0")
            self.assertEqual(install.choose_source(False, local=nsp)[0], nsp)

    def test_dry_run_by_default_sends_nothing(self):
        def factory():
            raise AssertionError("FTP must not be touched without --yes")
        rc = install.install(self.cfg, self.lines.append, yes=False, boot=True, release=True,
                             ftp_factory=factory)
        self.assertEqual(rc, 0)
        text = "\n".join(self.lines)
        self.assertIn("192.0.2.9:5000", text)
        self.assertIn("boot2.flag", text)
        self.assertIn("nothing was sent", text)

    def test_yes_uploads_exactly_the_plan(self):
        ftp = FakeFTP()
        rc = install.install(self.cfg, self.lines.append, yes=True, boot=False, release=True,
                             ftp_factory=lambda: ftp)
        self.assertEqual(rc, 0, self.lines)
        self.assertEqual(ftp.ops[0], ("connect", "192.0.2.9", 5000))
        self.assertIn(("mkd", "/atmosphere/contents"), ftp.ops)
        self.assertEqual(list(ftp.files), ["/atmosphere/contents/430000000000000B/exefs.nsp"])
        self.assertIn("NOT start at boot", "\n".join(self.lines))
        self.assertIn("re-run with --boot and reboot", self.lines[-1])

    def test_yes_with_boot_says_reboot(self):
        ftp = FakeFTP()
        rc = install.install(self.cfg, self.lines.append, yes=True, boot=True, release=True,
                             ftp_factory=lambda: ftp)
        self.assertEqual(rc, 0, self.lines)
        self.assertIn("/atmosphere/contents/430000000000000B/flags/boot2.flag", ftp.files)
        self.assertIn("reboot the console", self.lines[-1])

    def test_ftp_failure_is_reported(self):
        class Broken(FakeFTP):
            def connect(self, *a, **k):
                raise ConnectionRefusedError(111, "Connection refused")
        rc = install.install(self.cfg, self.lines.append, yes=True, boot=False, release=True,
                             ftp_factory=Broken)
        self.assertEqual(rc, 1)
        self.assertIn("install FAILED", self.lines[-1])


if __name__ == "__main__":
    unittest.main()
