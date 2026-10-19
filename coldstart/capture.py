"""Screenshot sources.

``album`` (default): press CAPTURE through sys-botbase, then fetch the new
JPEG from the SD card's album over FTP (sys-ftpd) and delete it. This works
on consoles where ``pixelPeek`` does not: capsrv refuses the debug capture
(0xE0CE) unless the real set:sys DebugModeFlag is set, and Atmosphere's
``enable_am_debug_mode`` only fakes the flag for am.

``pixelpeek``: sys-botbase's ``pixelPeek`` (an empty reply = unsupported).

The console's clock cannot be trusted (album years 2025, 2026 and 2051 all
occur on the rig's console), so the album is never located by the host's
date: the year/month/day directories are walked and the file names, which
start with the console's own timestamp, are compared. The capture is the
file that was not there before CAPTURE was pressed (if several appeared, the
lexicographically newest); no new file means the capture was refused or
failed, never "re-read the previous image".
"""
from __future__ import annotations

import ftplib
import io
import time

from .botbase import Botbase, CaptureUnavailable


class CaptureRefused(Exception):
    """The source works, but this screen produced no image (some screens
    refuse captures). The screen counts as ``unknown``."""


def _join(parent: str, name: str) -> str:
    if name.startswith("/"):
        return name                     # sys-ftpd's NLST returns full paths
    return parent.rstrip("/") + "/" + name


def _base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


class FtpAlbum:
    """The album on the SD card through sys-ftpd (anonymous FTP)."""

    def __init__(self, host: str, port: int, root: str = "/Nintendo/Album", timeout: float = 10.0,
                 ftp_factory=ftplib.FTP):
        self.host, self.port, self.root, self.timeout = host, port, root, timeout
        self.ftp_factory = ftp_factory

    def _open(self):
        try:
            ftp = self.ftp_factory()
            ftp.connect(self.host, self.port, timeout=self.timeout)
            ftp.login()
        except (OSError, EOFError, ftplib.Error) as e:
            raise CaptureUnavailable(f"FTP {self.host}:{self.port} (sys-ftpd): {e}") from None
        return ftp

    @staticmethod
    def _nlst(ftp, path: str) -> list[str]:
        try:
            names = ftp.nlst(path)
        except ftplib.error_perm:       # missing directory (or empty, on some servers)
            return []
        return [_join(path, n) for n in names if _base(n) not in (".", "..", "")]

    def _walk(self, ftp) -> set[str]:
        files = set()
        for y in self._nlst(ftp, self.root):
            if not (_base(y).isdigit() and len(_base(y)) == 4):
                continue
            for m in self._nlst(ftp, y):
                if not _base(m).isdigit():
                    continue
                for d in self._nlst(ftp, m):
                    if not _base(d).isdigit():
                        continue
                    files.update(f for f in self._nlst(ftp, d) if f.lower().endswith(".jpg"))
        return files

    def list(self) -> set[str]:
        ftp = self._open()
        try:
            return self._walk(ftp)
        except (OSError, EOFError, ftplib.Error) as e:
            raise CaptureUnavailable(f"FTP listing of {self.root} failed: {e}") from None
        finally:
            _quit(ftp)

    def fetch(self, path: str, delete: bool) -> bytes:
        ftp = self._open()
        try:
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write)
            if delete:
                ftp.delete(path)
            return buf.getvalue()
        except (OSError, EOFError, ftplib.Error) as e:
            raise CaptureUnavailable(f"FTP fetch of {path} failed: {e}") from None
        finally:
            _quit(ftp)


def _quit(ftp) -> None:
    try:
        ftp.quit()
    except (OSError, EOFError, ftplib.Error):
        try:
            ftp.close()
        except OSError:
            pass


def newest(names) -> str | None:
    return max(names, key=_base) if names else None


class AlbumSource:
    name = "album"

    def __init__(self, album: FtpAlbum, wait_s: float = 2.5, timeout_s: float = 8.0,
                 delete: bool = True, log=None, sleep=time.sleep):
        self.album, self.wait_s, self.timeout_s, self.delete = album, wait_s, timeout_s, delete
        self.log = log or (lambda m: None)
        self.sleep = sleep

    def capture(self, bb: Botbase) -> tuple[bytes, str]:
        before = self.album.list()
        bb.click("CAPTURE")
        self.sleep(self.wait_s)
        deadline = time.monotonic() + max(0.0, self.timeout_s - self.wait_s)
        while True:
            new = self.album.list() - before
            if new:
                break
            if time.monotonic() >= deadline:
                raise CaptureRefused(f"no new album image {self.timeout_s:g} s after CAPTURE "
                                     f"(newest still {_base(newest(before) or '-')}): capture refused "
                                     "on this screen, or the album is elsewhere")
            self.sleep(0.5)
        path = newest(new)
        if len(new) > 1:
            self.log(f"capture: {len(new)} new album files; taking the newest name {_base(path)}")
        overall = newest(before | new)
        if overall != path:
            self.log(f"capture: note {_base(overall)} sorts after the new image (console clock jumped)")
        return self.album.fetch(path, self.delete), path


class PixelPeekSource:
    name = "pixelpeek"

    def capture(self, bb: Botbase) -> tuple[bytes, str]:
        try:
            return bb.pixel_peek(), "pixelPeek"
        except CaptureUnavailable as e:
            raise CaptureUnavailable(f"pixelPeek unsupported on this console ({e})") from None


def from_config(cfg, log=None):
    s = cfg.screenshot
    if s.source == "pixelpeek":
        return PixelPeekSource()
    album = FtpAlbum(cfg.console.host, cfg.console.ftp_port, s.album_root, s.ftp_timeout_s)
    return AlbumSource(album, s.capture_wait_s, s.capture_timeout_s, s.delete_after, log=log)
