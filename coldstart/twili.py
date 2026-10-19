"""twili through the host tools ``twibd`` and ``twib``.

Only ``ps``, ``terminate`` and (for ``start-sysmodule``) ``launch <program id>
none`` are used. ``twib launch`` of an *application* and the ``host``
storage are never used: the first cannot work, the second takes the console
down.

The wrapper either uses a twibd already listening on ``twili.socket`` or
starts its own (``twibd --unix -P <run dir>/twibd.sock``) and stops it on
close. twibd must be told about the device once per lifetime
(``twib -P <sock> connect-tcp <ip>``); ``twib ps`` prints
``No devices were detected.`` until then.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

NO_DEVICES = "No devices were detected"


class TwiliUnavailable(Exception):
    """twib/twibd missing, twibd not answering, or the console not attached."""


class TwiliError(Exception):
    """twili answered but the operation failed."""


@dataclass
class Process:
    pid: int
    result: int
    title_id: int
    name: str
    mmu_flags: str

    def __str__(self):
        return f"pid=0x{self.pid:x} title=0x{self.title_id:016x} name={self.name}"


def _int(text: str) -> int:
    return int(text.strip(), 0)


def parse_ps(text: str) -> list[Process]:
    """Parse the ``twib ps`` table. Raises TwiliUnavailable when twib says no
    device is attached; ignores the header and any line it cannot parse."""
    if NO_DEVICES in text:
        raise TwiliUnavailable("twibd reports no device (connect-tcp not done or console gone)")
    procs = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 4:
            continue
        try:
            pid = _int(cols[0])
            result = _int(cols[1])
            tid = _int(cols[2])
        except ValueError:
            continue  # header or noise
        procs.append(Process(pid, result, tid, cols[3], cols[4] if len(cols) > 4 else ""))
    return procs


class Twili:
    def __init__(self, twib: str, twibd: str, host: str, socket_path: str = "",
                 run_dir: Path | None = None, timeout: float = 15.0, log=None):
        self.twib = twib
        self.twibd = twibd
        self.host = host
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        self._own = not socket_path
        if socket_path:
            self.socket = Path(socket_path)
        else:
            base = run_dir if run_dir is not None else Path.cwd()
            self.socket = base / "twibd.sock"
            if len(str(self.socket)) > 100:  # AF_UNIX path limit is 108 bytes
                self.socket = Path(tempfile.mkdtemp(prefix="coldstart-twibd-")) / "twibd.sock"
        self._daemon: subprocess.Popen | None = None
        self._connected = False

    # --- daemon ----------------------------------------------------------

    def _ensure_daemon(self) -> None:
        if not self._own:
            if not self.socket.exists():
                raise TwiliUnavailable(f"twibd socket {self.socket} does not exist")
            return
        if self._daemon is not None and self._daemon.poll() is None:
            return
        try:
            if self.socket.exists():
                self.socket.unlink()
            self._daemon = subprocess.Popen(
                [self.twibd, "--unix", "-P", str(self.socket)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise TwiliUnavailable(f"cannot start {self.twibd}: {e.strerror or e}") from None
        self.log(f"twili: started {self.twibd} (pid {self._daemon.pid}) on {self.socket}")
        self._connected = False
        deadline = time.monotonic() + 5.0
        while not self.socket.exists():
            if self._daemon.poll() is not None:
                raise TwiliUnavailable(f"twibd exited with status {self._daemon.returncode}")
            if time.monotonic() > deadline:
                raise TwiliUnavailable("twibd did not create its socket within 5 s")
            time.sleep(0.05)

    def close(self) -> None:
        if self._daemon is not None:
            if self._daemon.poll() is None:
                self._daemon.terminate()
                try:
                    self._daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._daemon.kill()
                    self._daemon.wait(timeout=3)
            self._daemon = None
            try:
                self.socket.unlink()
            except OSError:
                pass

    # --- twib ------------------------------------------------------------

    def _run(self, *args: str) -> tuple[int, str]:
        cmd = [self.twib, "-P", str(self.socket), *args]
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=self.timeout)
        except FileNotFoundError:
            raise TwiliUnavailable(f"{self.twib} not found") from None
        except subprocess.TimeoutExpired:
            raise TwiliUnavailable(f"twib {args[0]} timed out after {self.timeout:g} s") from None
        except OSError as e:
            raise TwiliUnavailable(f"twib {args[0]}: {e}") from None
        return p.returncode, (p.stdout + p.stderr)

    def connect(self) -> None:
        self._ensure_daemon()
        rc, out = self._run("connect-tcp", self.host)
        if rc != 0:
            raise TwiliUnavailable(f"twib connect-tcp {self.host} failed: {out.strip() or rc}")
        self._connected = True

    def ps(self) -> list[Process]:
        self._ensure_daemon()
        rc, out = self._run("ps")
        if NO_DEVICES in out:
            # once per twibd lifetime, or after the console came back
            self.connect()
            rc, out = self._run("ps")
        if rc != 0 and NO_DEVICES not in out:
            raise TwiliUnavailable(f"twib ps failed: {out.strip() or rc}")
        procs = parse_ps(out)
        self._connected = True
        return procs

    def _ensure_connected(self) -> None:
        if not self._connected or self._daemon is None and self._own:
            self.ps()

    def find(self, title_id: int) -> list[Process]:
        return [p for p in self.ps() if p.title_id == title_id]

    def terminate(self, pid: int, wait_s: float = 5.0) -> None:
        """Terminate and confirm with ps that the process is gone."""
        self._ensure_connected()
        rc, out = self._run("terminate", f"0x{pid:x}")
        if rc != 0:
            raise TwiliError(f"twib terminate 0x{pid:x} failed: {out.strip() or rc}")
        deadline = time.monotonic() + wait_s
        while True:
            if all(p.pid != pid for p in self.ps()):
                return
            if time.monotonic() > deadline:
                raise TwiliError(f"process 0x{pid:x} still listed {wait_s:g} s after terminate")
            time.sleep(0.25)

    def launch_program(self, program_id: str) -> str:
        """``twib launch <program id> none``: start a sysmodule (never an app)."""
        self._ensure_connected()
        rc, out = self._run("launch", program_id, "none")
        if rc != 0:
            raise TwiliError(f"twib launch {program_id} none failed: {out.strip() or rc}")
        return out.strip()


def from_config(cfg, run_dir: Path | None, log=None) -> Twili:
    return Twili(cfg.twili.twib, cfg.twili.twibd, cfg.console.host,
                 socket_path=os.path.expanduser(cfg.twili.socket) if cfg.twili.socket else "",
                 run_dir=run_dir, timeout=cfg.twili.timeout_s, log=log)
