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
from coldstart.capture import AlbumSource, CaptureRefused, FtpAlbum, PixelPeekSource, newest  # noqa: E402

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
        self.sd = Path(tempfile.mkdtemp(prefix="coldstart-sd-"))
        self.addCleanup(shutil.rmtree, self.sd)
        self.album_dir = self.sd / "Nintendo" / "Album"
        self.album_dir.mkdir(parents=True)
        self.srv = FtpServer(str(self.sd))
        self.addCleanup(self.srv.close)
        self.album = FtpAlbum("127.0.0.1", self.srv.port, timeout=5)

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
