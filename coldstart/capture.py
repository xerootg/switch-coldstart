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

The album's location: with emuMMC the console's Nintendo folder is
redirected (e.g. /emuMMC/RAW1/Nintendo/Album). ``album_root = "auto"`` (the
default) reads /emummc/emummc.ini over the same FTP on first use: emuMMC
enabled (``enabled`` nonzero) with a ``nintendo_path`` -> that path's Album,
otherwise /Nintendo/Album. The result is cached and logged once per process.
"""
from __future__ import annotations

import configparser
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


def _parent(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[0] or "/"


def _base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


AUTO = "auto"
DEFAULT_ROOT = "/Nintendo/Album"
EMUMMC_INI = "/emummc/emummc.ini"

# (host, port) -> (root, how): the auto-resolved album root, once per process
_resolved: dict[tuple[str, int], tuple[str, str]] = {}
# far-future album names already reported as sorting after a new capture
_clock_noted: set[str] = set()


def album_root_from_ini(text: str | None) -> tuple[str, str]:
    """(album root, explanation) from the contents of emummc.ini (None: no file)."""
    if text is None:
        return DEFAULT_ROOT, f"no {EMUMMC_INI}: sysMMC"
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error as e:
        return DEFAULT_ROOT, f"{EMUMMC_INI} unreadable ({str(e).splitlines()[0]}): assuming sysMMC"
    if not cp.has_section("emummc"):
        return DEFAULT_ROOT, f"{EMUMMC_INI} has no [emummc] section: sysMMC"
    sec = cp["emummc"]
    raw = sec.get("enabled", "0").strip().strip('"')
    try:
        enabled = int(raw, 0) != 0
    except ValueError:
        enabled = False
    if not enabled:
        return DEFAULT_ROOT, f"emuMMC disabled in {EMUMMC_INI} (enabled={raw}): sysMMC"
    npath = sec.get("nintendo_path", "").strip().strip('"').strip("/")
    if not npath:
        return DEFAULT_ROOT, f"emuMMC enabled but {EMUMMC_INI} has no nintendo_path"
    return "/" + npath + "/Album", f"emuMMC enabled, nintendo_path={npath} in {EMUMMC_INI}"


class FtpAlbum:
    """The album on the SD card through sys-ftpd (anonymous FTP)."""

    def __init__(self, host: str, port: int, root: str = AUTO, timeout: float = 10.0,
                 ftp_factory=ftplib.FTP, log=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.configured = root
        self.ftp_factory = ftp_factory
        self.log = log or (lambda m: None)
        self.how = "configured" if root != AUTO else ""

    @property
    def root(self) -> str | None:
        """The album root, or None while "auto" is not resolved yet."""
        if self.configured != AUTO:
            return self.configured
        hit = _resolved.get((self.host, self.port))
        if hit is not None:
            self.how = hit[1]
        return hit[0] if hit else None

    def resolve(self, ftp=None) -> str:
        """Resolve "auto" (reading emummc.ini on ``ftp`` or a new connection)."""
        root = self.root
        if root is not None:
            return root
        own = ftp is None
        if own:
            ftp = self._open()
        try:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {EMUMMC_INI}", buf.write)
                text = buf.getvalue().decode("utf-8", "replace")
            except (ftplib.error_perm, ftplib.error_temp) as e:
                # 550 (or sys-ftpd's 450) for a missing file. Only conclude
                # "no emuMMC" if the directory listing agrees; otherwise do
                # not cache a guess for the whole process.
                listed = {_base(n) for n in self._nlst(ftp, _parent(EMUMMC_INI))}
                if _base(EMUMMC_INI) in listed:
                    raise CaptureUnavailable(f"FTP read of {EMUMMC_INI} failed: {e}") from None
                text = None
            root, how = album_root_from_ini(text)
        except (OSError, EOFError, ftplib.Error) as e:
            raise CaptureUnavailable(f"FTP read of {EMUMMC_INI} failed: {e}") from None
        finally:
            if own:
                _quit(ftp)
        _resolved[(self.host, self.port)] = (root, how)
        self.how = how
        self.log(f"album root {root} ({how})")
        return root

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
        for y in self._nlst(ftp, self.resolve(ftp)):
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
            raise CaptureUnavailable(f"FTP listing of {self.root or 'the album'} failed: {e}") from None
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
        if overall != path and overall not in _clock_noted:
            _clock_noted.add(overall)   # once per process: it stays there until deleted
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
    album = FtpAlbum(cfg.console.host, cfg.console.ftp_port, s.album_root, s.ftp_timeout_s, log=log)
    return AlbumSource(album, s.capture_wait_s, s.capture_timeout_s, s.delete_after, log=log)
