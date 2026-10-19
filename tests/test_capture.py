"""The album screenshot source: the real ftplib client code against the
simulator's FTP server running in-process, plus edge cases on fakes."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness import REPO

sys.path.insert(0, str(REPO / "sim"))
import botbase_sim  # noqa: E402

from coldstart.botbase import CaptureUnavailable  # noqa: E402
import ftplib  # noqa: E402

from coldstart import capture  # noqa: E402
from coldstart.capture import (AlbumSource, CaptureRefused, FtpAlbum, PixelPeekSource,  # noqa: E402
                               album_root_from_ini, newest)

EMUMMC_INI = """[emummc]
enabled=1
sector=0x344d0000
path=emuMMC/RAW1
id=0x31574152
nintendo_path=emuMMC/RAW1/Nintendo
"""


def reset_capture_state():
    capture._resolved.clear()
    capture._clock_noted.clear()

JPEG = b"\xff\xd8\xff\xe0fake\xff\xd9"


class FtpServer:
    def __init__(self, root):
        self.listener = botbase_sim.TcpListener(
            "ftp", "127.0.0.1", 0, lambda conn: botbase_sim.FtpSession(conn, root, "127.0.0.1").run())
        self.port = self.listener.port

    def close(self):
        self.listener.close()


class FakeBot:
    """click("CAPTURE") drops a file into the album, like the console."""

    def __init__(self, album_root, names):
        self.root, self.names, self.clicks = Path(album_root), list(names), []

    def click(self, btn):
        self.clicks.append(btn)
        if self.names:
            name = self.names.pop(0)
            d = self.root / name[0:4] / name[4:6] / name[6:8]
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(JPEG + name.encode())


class AlbumOverFtp(unittest.TestCase):
    def setUp(self):
        reset_capture_state()
        self.sd = Path(tempfile.mkdtemp(prefix="coldstart-sd-"))
        self.addCleanup(shutil.rmtree, self.sd)
        self.album_dir = self.sd / "Nintendo" / "Album"
        self.album_dir.mkdir(parents=True)
        self.srv = FtpServer(str(self.sd))
        self.addCleanup(self.srv.close)
        self.album = FtpAlbum("127.0.0.1", self.srv.port, timeout=5)   # album_root "auto"

    def put(self, name):
        d = self.album_dir / name[0:4] / name[4:6] / name[6:8]
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(JPEG)

    def test_walk_lists_full_paths_and_ignores_other_entries(self):
        self.put("2025010112000000-AA.jpg")
        self.put("2051123123595999-BB.jpg")
        (self.album_dir / "Extra").mkdir()
        (self.album_dir / "2026" / "03" / "02").mkdir(parents=True)
        (self.album_dir / "2026" / "03" / "02" / "2026030221455100-CC.mp4").write_bytes(b"v")
        names = self.album.list()
        self.assertEqual(names, {"/Nintendo/Album/2025/01/01/2025010112000000-AA.jpg",
                                 "/Nintendo/Album/2051/12/31/2051123123595999-BB.jpg"})
        self.assertTrue(newest(names).endswith("BB.jpg"))

    def test_capture_takes_the_new_file_and_deletes_it(self):
        self.put("2051123123595999-OLD.jpg")              # wrong-clock leftovers
        bot = FakeBot(self.album_dir, ["2026030221455100-3C381EA7.jpg"])
        logs = []
        src = AlbumSource(self.album, wait_s=0, timeout_s=1, delete=True, log=logs.append,
                          sleep=lambda s: None)
        data, path = src.capture(bot)
        self.assertEqual(bot.clicks, ["CAPTURE"])
        self.assertEqual(path, "/Nintendo/Album/2026/03/02/2026030221455100-3C381EA7.jpg")
        self.assertTrue(data.startswith(JPEG))
        self.assertFalse((self.album_dir / "2026/03/02/2026030221455100-3C381EA7.jpg").exists())
        self.assertTrue(any("console clock jumped" in m for m in logs))
        self.assertEqual(len(self.album.list()), 1)

    def test_clock_note_is_logged_once_per_process(self):
        self.put("2051123123595999-OLD.jpg")
        bot = FakeBot(self.album_dir, ["2026030221455100-A1.jpg", "2026030221455200-A2.jpg",
                                       "2026030221455300-A3.jpg"])
        logs = []
        for _ in range(2):    # a new source per launch attempt, as the supervisor does
            src = AlbumSource(FtpAlbum("127.0.0.1", self.srv.port, timeout=5), wait_s=0, timeout_s=1,
                              log=logs.append, sleep=lambda s: None)
            src.capture(bot)
        src.capture(bot)
        self.assertEqual(sum("console clock jumped" in m for m in logs), 1, logs)

    def test_auto_root_without_emummc_is_nintendo_album(self):
        logs = []
        a = FtpAlbum("127.0.0.1", self.srv.port, timeout=5, log=logs.append)
        self.put("2026030221455100-AA.jpg")
        self.assertEqual(len(a.list()), 1)
        self.assertEqual(a.root, "/Nintendo/Album")
        self.assertEqual(sum("album root /Nintendo/Album" in m for m in logs), 1, logs)

    def test_auto_root_follows_emummc_ini(self):
        (self.sd / "emummc").mkdir()
        (self.sd / "emummc" / "emummc.ini").write_text(EMUMMC_INI)
        emu = self.sd / "emuMMC" / "RAW1" / "Nintendo" / "Album" / "2026" / "03" / "02"
        emu.mkdir(parents=True)
        (emu / "2026030221455100-EE.jpg").write_bytes(JPEG)
        self.put("2026030221455100-SYS.jpg")            # the sysMMC album is not used
        logs = []
        a = FtpAlbum("127.0.0.1", self.srv.port, timeout=5, log=logs.append)
        self.assertEqual(a.list(), {"/emuMMC/RAW1/Nintendo/Album/2026/03/02/2026030221455100-EE.jpg"})
        self.assertIn("emuMMC enabled", a.how)
        # resolved once per process: a second album object neither re-reads nor re-logs
        b = FtpAlbum("127.0.0.1", self.srv.port, timeout=5, log=logs.append)
        self.assertEqual(b.root, "/emuMMC/RAW1/Nintendo/Album")
        b.list()
        self.assertEqual(sum(m.startswith("album root ") for m in logs), 1, logs)

    def test_explicit_root_wins_over_emummc(self):
        (self.sd / "emummc").mkdir()
        (self.sd / "emummc" / "emummc.ini").write_text(EMUMMC_INI)
        self.put("2026030221455100-SYS.jpg")
        a = FtpAlbum("127.0.0.1", self.srv.port, root="/Nintendo/Album", timeout=5)
        self.assertEqual(len(a.list()), 1)
        self.assertEqual(a.how, "configured")

    def test_capture_keeps_file_when_delete_off(self):
        bot = FakeBot(self.album_dir, ["2026030221455100-AB.jpg"])
        src = AlbumSource(self.album, wait_s=0, timeout_s=1, delete=False, sleep=lambda s: None)
        src.capture(bot)
        self.assertEqual(len(self.album.list()), 1)

    def test_no_new_file_is_refused_not_an_old_image(self):
        self.put("2026030221455100-OLD.jpg")
        bot = FakeBot(self.album_dir, [])
        src = AlbumSource(self.album, wait_s=0, timeout_s=0.3, sleep=lambda s: None)
        with self.assertRaisesRegex(CaptureRefused, "no new album image"):
            src.capture(bot)

    def test_ftp_down_is_unavailable(self):
        self.srv.close()
        with self.assertRaisesRegex(CaptureUnavailable, "sys-ftpd"):
            self.album.list()

    def test_missing_album_is_empty(self):
        shutil.rmtree(self.album_dir)
        self.assertEqual(self.album.list(), set())

    def test_sim_ftp_also_serves_install(self):
        from coldstart import config, install
        cfg = config.load(None, env={"COLDSTART_HOST": "127.0.0.1",
                                     "COLDSTART_FTP_PORT": str(self.srv.port)})
        lines = []
        self.assertEqual(install.install(cfg, lines.append, yes=True, boot=True, release=True), 0,
                         lines)
        base = self.sd / "atmosphere/contents/430000000000000B"
        self.assertEqual((base / "exefs.nsp").read_bytes(),
                         (REPO / "reference/sys-botbase-2.5/exefs.nsp").read_bytes())
        self.assertEqual((base / "flags/boot2.flag").read_bytes(), b"")


class BareNamesFTP:
    """A server whose NLST returns bare names instead of full paths."""
    tree = {"/A": ["2026"], "/A/2026": ["03"], "/A/2026/03": ["02"],
            "/A/2026/03/02": ["2026030200000000-X.jpg", "."]}

    def nlst(self, path):
        return list(self.tree.get(path, []))


class FakeFTP:
    """ftplib.FTP stand-in: a dict of files, optional failure codes."""

    def __init__(self, files=None, retr_error=None, nlst_error=None):
        self.files = dict(files or {})
        self.retr_error, self.nlst_error = retr_error, nlst_error
        self.retrs = 0

    def connect(self, host, port, timeout=None):
        pass

    def login(self):
        pass

    def quit(self):
        pass

    def retrbinary(self, cmd, cb):
        self.retrs += 1
        path = cmd.split(" ", 1)[1]
        if self.retr_error:
            raise self.retr_error
        if path not in self.files:
            raise ftplib.error_perm("550 no such file")
        cb(self.files[path].encode())

    def nlst(self, path):
        if self.nlst_error:
            raise self.nlst_error
        pre = path.rstrip("/") + "/"
        return sorted({pre + p[len(pre):].split("/")[0] for p in self.files if p.startswith(pre)})


class AutoRoot(unittest.TestCase):
    def setUp(self):
        reset_capture_state()

    def album(self, ftp, **kw):
        logs = []
        return FtpAlbum("h", 1, ftp_factory=lambda: ftp, log=logs.append, **kw), logs

    def test_ini_parsing(self):
        self.assertEqual(album_root_from_ini(EMUMMC_INI)[0], "/emuMMC/RAW1/Nintendo/Album")
        self.assertEqual(album_root_from_ini(None)[0], "/Nintendo/Album")
        self.assertEqual(album_root_from_ini(EMUMMC_INI.replace("enabled=1", "enabled=0"))[0],
                         "/Nintendo/Album")
        self.assertEqual(album_root_from_ini(EMUMMC_INI.replace("enabled=1", "enabled=0x2"))[0],
                         "/emuMMC/RAW1/Nintendo/Album")       # any nonzero
        self.assertEqual(album_root_from_ini(EMUMMC_INI.replace("enabled=1", "enabled=yes"))[0],
                         "/Nintendo/Album")
        self.assertEqual(album_root_from_ini(
            EMUMMC_INI.replace("nintendo_path=emuMMC/RAW1/Nintendo", "nintendo_path=/emu/N/"))[0],
            "/emu/N/Album")
        no_path = album_root_from_ini(EMUMMC_INI.replace("nintendo_path=emuMMC/RAW1/Nintendo\n", ""))
        self.assertEqual(no_path[0], "/Nintendo/Album")
        self.assertIn("no nintendo_path", no_path[1])
        self.assertEqual(album_root_from_ini("garbage without a section")[0], "/Nintendo/Album")
        self.assertEqual(album_root_from_ini("[other]\nx=1\n")[0], "/Nintendo/Album")

    def test_resolves_once_and_logs_once(self):
        ftp = FakeFTP({"/emummc/emummc.ini": EMUMMC_INI})
        a, logs = self.album(ftp)
        self.assertIsNone(a.root)
        self.assertEqual(a.resolve(), "/emuMMC/RAW1/Nintendo/Album")
        self.assertEqual(a.resolve(), "/emuMMC/RAW1/Nintendo/Album")
        b, logs_b = self.album(ftp)
        self.assertEqual(b.resolve(), "/emuMMC/RAW1/Nintendo/Album")
        self.assertEqual(ftp.retrs, 1)
        self.assertEqual(logs, ["album root /emuMMC/RAW1/Nintendo/Album "
                                "(emuMMC enabled, nintendo_path=emuMMC/RAW1/Nintendo in /emummc/emummc.ini)"])
        self.assertEqual(logs_b, [])

    def test_missing_ini_is_sysmmc(self):
        a, logs = self.album(FakeFTP({}))
        self.assertEqual(a.resolve(), "/Nintendo/Album")
        self.assertIn("no /emummc/emummc.ini", logs[0])

    def test_temporary_error_on_a_listed_ini_is_not_cached(self):
        ftp = FakeFTP({"/emummc/emummc.ini": EMUMMC_INI}, retr_error=ftplib.error_temp("450 busy"))
        a, logs = self.album(ftp)
        with self.assertRaisesRegex(CaptureUnavailable, "emummc.ini"):
            a.resolve()
        self.assertIsNone(a.root)
        ftp.retr_error = None
        self.assertEqual(a.resolve(), "/emuMMC/RAW1/Nintendo/Album")

    def test_450_for_a_missing_ini_is_sysmmc(self):
        a, _ = self.album(FakeFTP({}, retr_error=ftplib.error_temp("450 cannot open")))
        self.assertEqual(a.resolve(), "/Nintendo/Album")

    def test_connection_failure_is_unavailable_not_cached(self):
        a, _ = self.album(FakeFTP({}, retr_error=OSError("reset")))
        with self.assertRaisesRegex(CaptureUnavailable, "emummc.ini"):
            a.resolve()
        self.assertEqual(capture._resolved, {})

    def test_explicit_root_never_reads_the_ini(self):
        ftp = FakeFTP({"/emummc/emummc.ini": EMUMMC_INI})
        a, logs = self.album(ftp, root="/Custom/Album")
        self.assertEqual(a.resolve(), "/Custom/Album")
        self.assertEqual(ftp.retrs, 0)
        self.assertEqual(logs, [])


class Misc(unittest.TestCase):
    def test_bare_nlst_names_are_joined(self):
        a = FtpAlbum("h", 1, root="/A")
        self.assertEqual(a._walk(BareNamesFTP()), {"/A/2026/03/02/2026030200000000-X.jpg"})

    def test_pixelpeek_empty_is_unsupported(self):
        class B:
            def pixel_peek(self):
                raise CaptureUnavailable("pixelPeek returned no image")
        with self.assertRaisesRegex(CaptureUnavailable, "unsupported"):
            PixelPeekSource().capture(B())


if __name__ == "__main__":
    unittest.main()
