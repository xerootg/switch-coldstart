"""Put sys-botbase on the SD card over FTP (sys-ftpd); start it via twili as a fallback.

Installing a third-party sysmodule is the owner's decision: ``install``
prints its plan and does nothing unless ``--yes`` is given. ``boot2.flag``
(start at every boot) is written only with ``--boot``; the normal path is
``install --yes --boot`` followed by a reboot by the owner. ``start-sysmodule``
(twili's ``launch``) only helps while twili still runs on the console.
"""
from __future__ import annotations

import ftplib
import hashlib
import io
import time
from dataclasses import dataclass
from pathlib import Path

PROGRAM_ID = "430000000000000B"
CONTENTS_DIR = f"/atmosphere/contents/{PROGRAM_ID}"
REPO = Path(__file__).resolve().parent.parent
LOCAL_BUILD = REPO / "sysmodule" / "out" / PROGRAM_ID / "exefs.nsp"
RELEASE = REPO / "reference" / "sys-botbase-2.5" / "exefs.nsp"


class InstallError(Exception):
    pass


@dataclass
class Upload:
    remote: str
    data: bytes
    origin: str

    def describe(self) -> str:
        digest = hashlib.sha256(self.data).hexdigest()[:16]
        return f"upload {self.origin} -> {self.remote} ({len(self.data)} bytes, sha256 {digest}...)"


def choose_source(release: bool, local: Path = LOCAL_BUILD, rel: Path = RELEASE) -> tuple[Path, str]:
    if release:
        if not rel.is_file():
            raise InstallError(f"release artifact missing: {rel}")
        return rel, "the sys-botbase v2.5 release artifact (third-party binary from reference/)"
    if local.is_file():
        return local, "the local build (make sysmodule)"
    raise InstallError(
        f"no local build at {local}: run `make sysmodule`, or pass --release to install the "
        f"v2.5 release artifact shipped in reference/sys-botbase-2.5/")


def plan(release: bool, boot: bool, local: Path = LOCAL_BUILD, rel: Path = RELEASE):
    src, what = choose_source(release, local, rel)
    ups = [Upload(f"{CONTENTS_DIR}/exefs.nsp", src.read_bytes(), str(src))]
    if boot:
        ups.append(Upload(f"{CONTENTS_DIR}/flags/boot2.flag", b"", "(empty file)"))
    dirs = ["/atmosphere", "/atmosphere/contents", CONTENTS_DIR]
    if boot:
        dirs.append(f"{CONTENTS_DIR}/flags")
    return what, dirs, ups


def _mkdirs(ftp, dirs) -> list[str]:
    made = []
    for d in dirs:
        try:
            ftp.cwd(d)
        except ftplib.error_perm:
            ftp.mkd(d)
            made.append(d)
    ftp.cwd("/")
    return made


def install(cfg, log, yes: bool, boot: bool, release: bool, ftp_factory=None) -> int:
    host, port = cfg.console.host, cfg.console.ftp_port
    try:
        what, dirs, ups = plan(release, boot)
    except InstallError as e:
        log(f"install: {e}")
        return 1
    log(f"install plan (sys-ftpd at {host}:{port}, anonymous), source: {what}")
    for d in dirs:
        log(f"  ensure directory {d}")
    for u in ups:
        log(f"  {u.describe()}")
    if not boot:
        log("  no boot2.flag: the sysmodule will NOT start at boot (the normal install is "
            "--boot followed by a reboot; `coldstart start-sysmodule` is a twili-only fallback)")
    if not yes:
        log("dry run: nothing was sent. Re-run with --yes to do exactly the above.")
        return 0
    factory = ftp_factory or (lambda: ftplib.FTP())
    try:
        ftp = factory()
        ftp.connect(host, port, timeout=15)
        ftp.login()
        made = _mkdirs(ftp, dirs)
        for d in made:
            log(f"created {d}")
        for u in ups:
            ftp.storbinary(f"STOR {u.remote}", io.BytesIO(u.data))
            try:
                size = ftp.size(u.remote)
            except ftplib.error_perm:  # SIZE not supported: skip the check
                size = None
            if size is not None and size != len(u.data):
                raise InstallError(f"{u.remote}: {size} bytes on the card, expected {len(u.data)}")
            log(f"uploaded {u.remote} ({len(u.data)} bytes)")
        ftp.quit()
    except (OSError, ftplib.Error, EOFError, InstallError) as e:
        log(f"install FAILED: {e}")
        return 1
    if boot:
        log("installed with boot2.flag. Next: reboot the console (sys-botbase listens ~20 s after "
            "boot), then `coldstart status`.")
    else:
        log("installed without boot2.flag. Next: re-run with --boot and reboot, or, if twili still "
            "runs, `coldstart start-sysmodule` as a one-off fallback.")
    return 0


def start_sysmodule(cfg, log, twili, wait_s: float = 90.0, poll_s: float = 2.0) -> int:
    from .botbase import Botbase, BotbaseError, port_answers  # noqa: PLC0415

    host, port = cfg.console.host, cfg.botbase.port
    if port_answers(host, port, cfg.console.probe_timeout_s):
        log(f"port {port} already answers; not launching a second instance")
    else:
        log(f"twili: launch {PROGRAM_ID} none")
        try:
            out = twili.launch_program(PROGRAM_ID)
        except Exception as e:  # noqa: BLE001 - TwiliError/TwiliUnavailable, reported verbatim
            log(f"start-sysmodule FAILED: {e}")
            return 1
        if out:
            log(f"twili: {out}")
        log(f"waiting up to {wait_s:g} s for port {port} (sys-botbase sleeps ~20 s before listening)")
        deadline = time.monotonic() + wait_s
        while not port_answers(host, port, cfg.console.probe_timeout_s):
            if time.monotonic() >= deadline:
                log(f"start-sysmodule FAILED: port {port} did not answer within {wait_s:g} s "
                    "(not on the card? incompatible with this firmware? check `twib ps`)")
                return 1
            time.sleep(poll_s)
    try:
        with Botbase(host, port, cfg.botbase.timeout_s) as bb:
            v = bb.get_version()
    except BotbaseError as e:
        log(f"start-sysmodule FAILED: port {port} accepts but getVersion failed: {e}")
        return 1
    log(f"sys-botbase {v} answers on {host}:{port}")
    return 0
