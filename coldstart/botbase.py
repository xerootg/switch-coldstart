"""sys-botbase protocol client (TCP 6000, text commands ending in CR LF).

Only the subset this tool needs. Facts from the sys-botbase source
(main.c/util.c at the pinned revision) that shape this client:

- Commands are split on spaces/CR/LF; a line ends at LF.
- ``click``/``press``/``release``/``configure`` print nothing; ``getVersion``
  and ``pixelPeek`` print exactly one line.
- An unknown button name is silently treated as **A**. Button names are
  therefore validated here before anything is sent.
- ``pixelPeek`` prints the JPEG as upper-case hex on one line, or an empty
  line when the capture fails (Atmosphere's ``enable_am_debug_mode`` off).
"""
from __future__ import annotations

import socket

BUTTONS = frozenset(
    "A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE DUP DDOWN DLEFT DRIGHT LSTICK RSTICK".split()
)
# Holding R on an application tile launches the real application instead of
# hbl (override_any_app_key=!R). This tool never holds R.
NEVER_HOLD = frozenset({"R"})


class BotbaseError(Exception):
    """Protocol-level problem (bad reply, bad argument)."""


class BotbaseUnavailable(BotbaseError):
    """Port 6000 does not answer (refused, timed out, connection dropped)."""


class CaptureUnavailable(BotbaseError):
    """pixelPeek returned no image."""


class Botbase:
    def __init__(self, host: str, port: int = 6000, timeout: float = 5.0,
                 shot_timeout: float = 15.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.shot_timeout = shot_timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise BotbaseUnavailable(f"{self.host}:{self.port}: {e.strerror or e}") from None
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send(self, text: str) -> None:
        if self.sock is None:
            raise BotbaseError("not connected")
        if "\r" in text or "\n" in text:
            raise BotbaseError(f"command contains a line break: {text!r}")
        try:
            self.sock.sendall((text + "\r\n").encode("ascii"))
        except OSError as e:
            raise BotbaseUnavailable(f"send {text.split()[0]!r}: {e}") from None

    def _readline(self, timeout: float) -> str:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(1 << 20)
            except socket.timeout:
                raise BotbaseUnavailable(f"no reply within {timeout:g} s") from None
            except OSError as e:
                raise BotbaseUnavailable(f"recv: {e}") from None
            if not chunk:
                raise BotbaseUnavailable("connection closed by the console")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("ascii", errors="replace").strip()

    def ask(self, text: str, timeout: float | None = None) -> str:
        self.send(text)
        return self._readline(self.timeout if timeout is None else timeout)

    # --- commands -------------------------------------------------------

    def get_version(self) -> str:
        v = self.ask("getVersion")
        if not v:
            raise BotbaseError("getVersion returned an empty line")
        return v

    @staticmethod
    def check_button(btn: str) -> str:
        if btn not in BUTTONS:
            raise BotbaseError(f"unknown button {btn!r} (sys-botbase would press A instead)")
        return btn

    def click(self, btn: str) -> None:
        self.send("click " + self.check_button(btn))

    def press(self, btn: str) -> None:
        if btn in NEVER_HOLD:
            raise BotbaseError(f"refusing to hold {btn}: it launches the real application instead of hbl")
        self.send("press " + self.check_button(btn))

    def release(self, btn: str) -> None:
        self.send("release " + self.check_button(btn))

    def configure(self, key: str, value: int) -> None:
        if not key.isalnum():
            raise BotbaseError(f"bad configure key {key!r}")
        self.send(f"configure {key} {int(value)}")

    def pixel_peek(self) -> bytes:
        hexdata = self.ask("pixelPeek", timeout=self.shot_timeout)
        if not hexdata:
            raise CaptureUnavailable(
                "pixelPeek returned no image (is enable_am_debug_mode=1 set in Atmosphere?)")
        try:
            data = bytes.fromhex(hexdata)
        except ValueError:
            raise BotbaseError(f"pixelPeek reply is not hex ({len(hexdata)} chars)") from None
        if not data.startswith(b"\xff\xd8"):
            raise BotbaseError("pixelPeek reply is not a JPEG")
        return data


def port_answers(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
