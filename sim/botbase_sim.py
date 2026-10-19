#!/usr/bin/env python3
"""botbase_sim.py -- a fake console for testing switch-coldstart on the host.

Serves, on 127.0.0.1 by default:

- the sys-botbase protocol subset (TCP, default 6000): click/press/release
  <BTN>, getVersion, pixelPeek, configure <key> <value>; unknown buttons are
  pressed as A, exactly like sys-botbase;
- a fake rig control port (UDP, default 5600) answering ``ping`` while the
  game or waiter is in the foreground (and not "deaf");
- a minimal FTP server standing in for sys-ftpd (TCP, default 5000) over an
  SD-card directory: the album lives in Nintendo/Album/YYYY/MM/DD/ and CAPTURE
  saves the current screen there as YYYYMMDDHHMMSSff-<hex>.jpg (by the
  console's clock, which --clock-offset-s can skew); it also lets "the
  console answers TCP" be told apart from "sys-botbase answers";
- a control port (TCP, JSON lines) used by the tests and by the fake ``twib``
  in sim/bin to read and change the state.

The fake console has screens home / dialog / user-select / game / waiter /
other / close-prompt / asleep, a cursor on a row of tiles ("app" or "other"), and an optional
hbl process. Rules: A on an app tile -> user-select (optional) -> game (or
waiter); A on an other tile -> other; A on the dialog -> home; HOME from the
game/waiter/other/user-select -> home (the app stays, suspended); DLEFT/DRIGHT
move the cursor within bounds; X on HOME with a suspended app -> close-prompt,
whose A closes the app (-> home, nothing suspended) and whose B/HOME go back;
asleep closes every TCP port and silences the
rig. pixelPeek and CAPTURE produce a synthetic JPEG with a distinct colour
layout per screen and a clock in the top bar that changes every second.

    botbase_sim.py [--screen home] [--tiles app,app] [--user-select] [--deaf]
                   [--land game|waiter] [--no-botbase] [--no-capture]
                   [--boot-delay S] [--ports-file F] [--<name>-port N]
                   [--sd-dir D] [--clock-offset-s S] [--album-seed T ...]
                   [--capture-refuse screen,...] [--capture-delay S]

Port 0 picks a free port; --ports-file receives the chosen ports as JSON.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import socket
import shutil
import sys
import tempfile
import threading
import time

from PIL import Image, ImageDraw

BUTTONS = set("A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE DUP DDOWN DLEFT DRIGHT "
              "LSTICK RSTICK DL DU DR DD PALMA UNUSED".split())
ALIASES = {"DL": "DLEFT", "DU": "DUP", "DR": "DRIGHT", "DD": "DDOWN"}
SCREENS = ("home", "dialog", "user-select", "game", "waiter", "other", "close-prompt", "asleep")
HBL_TITLE = 0x01007EF00011E000
BOTBASE_TITLE = "430000000000000B"
W, H = 1280, 720


def log(msg):
    print(f"[sim {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class Console:
    def __init__(self, args):
        self.lock = threading.RLock()
        self.screen = args.screen
        self.tiles = args.tiles
        self.cursor = 0
        self.user_select = args.user_select
        self.land = args.land
        self.deaf = args.deaf
        self.botbase = not args.no_botbase
        self.capture = not args.no_capture
        self.boot_delay = args.boot_delay
        self.twili_connected = False
        self.next_pid = 0x97
        self.app = None            # {"pid": int, "started": float}
        self.pending_launch = False
        self.events = []           # every button and notable event, for the tests
        self.held = set()
        self.on_change = lambda: None
        self.on_capture = lambda: None
        if self.screen in ("game", "waiter"):
            self._start_app(started=time.monotonic() - self.boot_delay)

    # -- state ------------------------------------------------------------

    def snapshot(self):
        with self.lock:
            return {"screen": self.screen, "cursor": self.cursor, "tiles": self.tiles,
                    "app": dict(self.app) if self.app else None, "deaf": self.deaf,
                    "botbase": self.botbase, "capture": self.capture,
                    "twili_connected": self.twili_connected, "held": sorted(self.held),
                    "events": list(self.events), "rig_role": self.rig_role()}

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if k == "screen":
                    if v not in SCREENS:
                        raise ValueError(f"bad screen {v}")
                    if v == "asleep":
                        self.twili_connected = False
                    if v in ("game", "waiter") and self.app is None:
                        self._start_app(started=time.monotonic() - self.boot_delay)
                elif k not in ("cursor", "tiles", "user_select", "land", "deaf", "botbase",
                               "capture", "boot_delay"):
                    raise ValueError(f"cannot set {k}")
                setattr(self, k, v)
                self.events.append(f"set {k}={v}")
        self.on_change()

    def _start_app(self, started=None):
        self.app = {"pid": self.next_pid, "started": time.monotonic() if started is None else started}
        self.next_pid += 1
        self.deaf = False
        self.screen = self.land

    def rig_role(self):
        with self.lock:
            if self.screen not in ("game", "waiter") or self.app is None or self.deaf:
                return None
            if time.monotonic() - self.app["started"] < self.boot_delay:
                return None
            return self.screen

    # -- buttons ------------------------------------------------------------

    def button(self, btn):
        btn = ALIASES.get(btn, btn)
        if btn not in BUTTONS:
            btn = "A"   # sys-botbase: "return HidNpadButton_A; //I guess lol"
        with self.lock:
            self.events.append(btn)
            s = self.screen
            if btn == "HOME":
                if s in ("game", "waiter", "other", "user-select", "close-prompt"):
                    self.screen = "home"
                    self.pending_launch = False
            elif btn == "CAPTURE":
                self.on_capture()
            elif btn == "X":
                if s == "home" and self.app is not None:
                    self.screen = "close-prompt"
            elif btn == "A":
                if s == "dialog":
                    self.screen = "home"
                elif s == "close-prompt":
                    self.events.append(f"closed 0x{self.app['pid']:x}" if self.app else "closed none")
                    self.app = None
                    self.screen = "home"
                elif s == "home":
                    tile = self.tiles[self.cursor] if self.tiles else None
                    if tile == "app":
                        if self.app is not None and self.cursor == 0:
                            self.screen = self.land          # resume the suspended app
                        elif self.user_select:
                            self.screen = "user-select"
                            self.pending_launch = True
                        else:
                            self.app = None
                            self._start_app()
                    elif tile is not None:
                        self.screen = "other"
                elif s == "user-select" and self.pending_launch:
                    self.pending_launch = False
                    self.app = None
                    self._start_app()
            elif btn == "B":
                if s == "close-prompt":
                    self.screen = "home"
                elif s == "user-select":
                    self.screen = "home"
                    self.pending_launch = False
            elif btn in ("DLEFT", "DRIGHT") and s == "home":
                step = -1 if btn == "DLEFT" else 1
                self.cursor = max(0, min(len(self.tiles) - 1, self.cursor + step))

    def terminate(self, pid):
        with self.lock:
            if self.app is None or self.app["pid"] != pid:
                return False
            self.app = None
            self.events.append(f"terminate 0x{pid:x}")
            if self.screen in ("game", "waiter", "user-select"):
                self.screen = "home"
            return True

    # -- screenshot ---------------------------------------------------------

    def render(self) -> bytes:
        with self.lock:
            screen, cursor, tiles = self.screen, self.cursor, list(self.tiles)
        im = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(im)
        if screen in ("home", "dialog", "close-prompt"):
            d.rectangle((0, 0, W, H), fill=(45, 45, 60))
            # top bar: a "clock" whose colour changes every second
            sec = int(time.time())
            d.rectangle((0, 0, W, 70), fill=(60, 60, 80))
            d.rectangle((1100, 15, 1260, 55), fill=((sec * 53) % 256, (sec * 97) % 256, 200))
            for i, kind in enumerate(tiles[:6]):
                x = 60 + i * 200
                colour = (200, 60, 60) if kind == "app" else (60, 160, 220)
                d.rectangle((x, 180, x + 170, 470), fill=colour)
                if i == cursor:
                    d.rectangle((x - 8, 172, x + 178, 478), outline=(0, 230, 230), width=8)
            for i in range(6):
                d.ellipse((380 + i * 90, 560, 440 + i * 90, 620), fill=(230, 230, 230))
            if screen == "dialog":
                im = Image.blend(im, Image.new("RGB", (W, H), (0, 0, 0)), 0.6)
                d = ImageDraw.Draw(im)
                d.rectangle((290, 200, 990, 520), fill=(250, 250, 250))
                d.rectangle((290, 440, 990, 520), fill=(30, 140, 230))
            elif screen == "close-prompt":
                im = Image.blend(im, Image.new("RGB", (W, H), (0, 0, 0)), 0.6)
                d = ImageDraw.Draw(im)
                d.rectangle((240, 160, 1040, 560), fill=(70, 70, 75))
                d.rectangle((240, 470, 640, 560), fill=(220, 40, 40))
                d.rectangle((640, 470, 1040, 560), fill=(250, 250, 250))
        elif screen == "user-select":
            d.rectangle((0, 0, W, H), fill=(20, 90, 200))
            for i in range(3):
                d.ellipse((300 + i * 260, 260, 480 + i * 260, 440), fill=(250, 220, 120))
        elif screen == "game":
            d.rectangle((0, 0, W, H), fill=(30, 170, 60))
            d.rectangle((200, 150, 1080, 570), fill=(10, 60, 20))
        elif screen == "waiter":
            d.rectangle((0, 0, W, H), fill=(170, 40, 170))
            d.rectangle((100, 600, 1180, 680), fill=(250, 250, 250))
        elif screen == "other":
            d.rectangle((0, 0, W, H), fill=(240, 140, 0))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


class TcpListener:
    """A listening socket that can be closed and reopened on the same port, so
    "asleep" and "sysmodule not running" refuse connections like the real thing."""

    def __init__(self, name, host, port, handler):
        self.name, self.host, self.handler = name, host, handler
        self.sock = None
        self.lock = threading.Lock()
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        self.port = probe.getsockname()[1]
        self._adopt(probe)

    def _adopt(self, s):
        s.listen(8)
        self.sock = s
        threading.Thread(target=self._accept, args=(s,), daemon=True).start()

    def open(self):
        with self.lock:
            if self.sock is not None:
                return
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            self._adopt(s)

    def close(self):
        with self.lock:
            if self.sock is None:
                return
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _accept(self, s):
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            self.handler(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class FtpSession:
    """Just enough of an FTP server (passive mode) for ftplib against the SD
    card directory: USER PASS SYST TYPE PWD CWD PASV NLST LIST RETR STOR DELE
    MKD SIZE QUIT. NLST returns full paths, like sys-ftpd."""

    def __init__(self, conn, root, host):
        self.conn, self.root, self.host = conn, os.path.realpath(root), host
        self.cwd = "/"
        self.pasv = None

    def reply(self, text):
        self.conn.sendall((text + "\r\n").encode())

    def local(self, path):
        virt = os.path.normpath(os.path.join(self.cwd, path or "."))
        if not virt.startswith("/"):
            virt = "/" + virt
        real = os.path.realpath(self.root + virt)
        if real != self.root and not real.startswith(self.root + os.sep):
            return virt, None
        return virt, real

    def data(self):
        if self.pasv is None:
            self.reply("425 use PASV first")
            return None
        self.pasv.settimeout(5)
        try:
            d, _ = self.pasv.accept()
        finally:
            self.pasv.close()
            self.pasv = None
        return d

    def run(self):
        self.reply("220 botbase_sim sys-ftpd stand-in")
        f = self.conn.makefile("rb")
        for raw in f:
            cmd, _, arg = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            cmd = cmd.upper()
            if cmd == "QUIT":
                self.reply("221 bye")
                return
            handler = getattr(self, "do_" + cmd, None)
            if handler is None:
                self.reply(f"502 {cmd} not implemented")
            else:
                handler(arg)

    def do_USER(self, arg):
        self.reply("331 any password")

    def do_PASS(self, arg):
        self.reply("230 logged in")

    def do_SYST(self, arg):
        self.reply("215 UNIX Type: L8")

    def do_TYPE(self, arg):
        self.reply("200 type set")

    def do_PWD(self, arg):
        self.reply(f'257 "{self.cwd}"')

    def do_CWD(self, arg):
        virt, real = self.local(arg)
        if real and os.path.isdir(real):
            self.cwd = virt
            self.reply("250 ok")
        else:
            self.reply("550 no such directory")

    def do_PASV(self, arg):
        s = socket.socket()
        s.bind((self.host, 0))
        s.listen(1)
        self.pasv = s
        port = s.getsockname()[1]
        h = self.host.replace(".", ",")
        self.reply(f"227 Entering Passive Mode ({h},{port >> 8},{port & 255})")

    def _listing(self, arg, long):
        virt, real = self.local(arg)
        if not real or not os.path.isdir(real):
            self.reply("550 no such directory")
            return
        names = sorted(os.listdir(real))
        if long:
            lines = [("d" if os.path.isdir(os.path.join(real, n)) else "-")
                     + f"rw-r--r-- 1 0 0 {os.path.getsize(os.path.join(real, n))} Jan 1 00:00 {n}"
                     for n in names]
        else:
            lines = [virt.rstrip("/") + "/" + n for n in names]
        d = self.data()
        if d is None:
            return
        self.reply("150 listing")
        with d:
            d.sendall("".join(line + "\r\n" for line in lines).encode())
        self.reply("226 done")

    def do_NLST(self, arg):
        self._listing(arg, False)

    def do_LIST(self, arg):
        self._listing(arg, True)

    def do_RETR(self, arg):
        _, real = self.local(arg)
        if not real or not os.path.isfile(real):
            self.reply("550 no such file")
            return
        d = self.data()
        if d is None:
            return
        self.reply("150 sending")
        with d, open(real, "rb") as fh:
            d.sendall(fh.read())
        self.reply("226 done")

    def do_STOR(self, arg):
        _, real = self.local(arg)
        if not real or not os.path.isdir(os.path.dirname(real)):
            self.reply("553 bad path")
            return
        d = self.data()
        if d is None:
            return
        self.reply("150 receiving")
        with d, open(real, "wb") as fh:
            while True:
                chunk = d.recv(65536)
                if not chunk:
                    break
                fh.write(chunk)
        self.reply("226 stored")

    def do_DELE(self, arg):
        _, real = self.local(arg)
        if real and os.path.isfile(real):
            os.unlink(real)
            self.reply("250 deleted")
        else:
            self.reply("550 no such file")

    def do_MKD(self, arg):
        virt, real = self.local(arg)
        try:
            os.mkdir(real)
            self.reply(f'257 "{virt}" created')
        except (OSError, TypeError):
            self.reply("550 cannot create")

    def do_SIZE(self, arg):
        _, real = self.local(arg)
        if real and os.path.isfile(real):
            self.reply(f"213 {os.path.getsize(real)}")
        else:
            self.reply("550 no such file")


def album_name(t: float, seq: int) -> tuple[str, str]:
    """(relative directory, file name) the way the console names captures:
    Nintendo/Album/YYYY/MM/DD/YYYYMMDDHHMMSSff-<32 hex>.jpg"""
    tm = time.gmtime(t)
    stamp = time.strftime("%Y%m%d%H%M%S", tm) + f"{int(t * 100) % 100:02d}"
    d = time.strftime("Nintendo/Album/%Y/%m/%d", tm)
    return d, f"{stamp}-{seq:032X}.jpg"


class Sim:
    def __init__(self, args):
        self.args = args
        self.sd = args.sd_dir or tempfile.mkdtemp(prefix="botbase-sim-sd-")
        self.own_sd = args.sd_dir is None
        os.makedirs(os.path.join(self.sd, "Nintendo", "Album"), exist_ok=True)
        self.capture_seq = 0
        self.console = Console(args)
        self.console.on_capture = self.capture
        for t in args.album_seed:
            os.replace(*self._write_album(t, self.console.render()))
        self.console.on_change = self.reconcile
        self.botbase_l = TcpListener("botbase", args.host, args.botbase_port, self.serve_botbase)
        self.ftp_l = TcpListener("ftp", args.host, args.ftp_port, self.serve_ftp)
        self.ctl_l = TcpListener("ctl", args.host, args.ctl_port, self.serve_ctl)
        self.rig = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rig.bind((args.host, args.rig_port))
        threading.Thread(target=self.serve_rig, daemon=True).start()
        self.reconcile()

    def _write_album(self, t, jpeg):
        self.capture_seq += 1
        d, name = album_name(t, self.capture_seq)
        full = os.path.join(self.sd, d)
        os.makedirs(full, exist_ok=True)
        tmp = os.path.join(self.sd, f".{name}.part")
        with open(tmp, "wb") as fh:
            fh.write(jpeg)
        return tmp, os.path.join(full, name)

    def capture(self):
        """CAPTURE: the console saves a JPEG into the album a moment later
        (or not at all on screens that refuse captures)."""
        c = self.console
        if c.screen in self.args.capture_refuse:
            c.events.append("capture refused")
            return
        tmp, final = self._write_album(time.time() + self.args.clock_offset_s, c.render())
        threading.Timer(self.args.capture_delay, os.replace, (tmp, final)).start()

    def ports(self):
        return {"botbase": self.botbase_l.port, "ftp": self.ftp_l.port,
                "ctl": self.ctl_l.port, "rig": self.rig.getsockname()[1]}

    def reconcile(self):
        c = self.console
        awake = c.screen != "asleep"
        (self.ftp_l.open if awake else self.ftp_l.close)()
        (self.botbase_l.open if awake and c.botbase else self.botbase_l.close)()

    # -- servers ------------------------------------------------------------

    def serve_botbase(self, conn):
        c = self.console
        f = conn.makefile("rb")
        for raw in f:
            line = raw.decode("ascii", "replace")
            argv = line.replace("\r", " ").replace("\n", " ").split()
            if not argv:
                continue
            cmd = argv[0]
            out = None
            if cmd == "getVersion":
                out = "2.5"
            elif cmd == "click" and len(argv) == 2:
                c.button(argv[1])
                time.sleep(0.05)
            elif cmd == "press" and len(argv) == 2:
                with c.lock:
                    c.held.add(ALIASES.get(argv[1], argv[1]))
                    c.events.append("press " + argv[1])
            elif cmd == "release" and len(argv) == 2:
                with c.lock:
                    c.held.discard(ALIASES.get(argv[1], argv[1]))
                    c.events.append("release " + argv[1])
            elif cmd == "configure" and len(argv) == 3:
                with c.lock:
                    c.events.append(f"configure {argv[1]} {argv[2]}")
            elif cmd == "pixelPeek":
                out = c.render().hex().upper() if c.capture else ""
            if out is not None:
                conn.sendall((out + "\n").encode("ascii"))
            if c.screen == "asleep" or not c.botbase:
                return

    def serve_ftp(self, conn):
        FtpSession(conn, self.sd, self.args.host).run()

    def serve_rig(self):
        while True:
            try:
                data, addr = self.rig.recvfrom(4096)
            except OSError:
                return
            cmd = data.decode("ascii", "replace").strip()
            role = self.console.rig_role()
            if role is None:
                continue
            app = self.console.app or {"started": time.monotonic()}
            up = int(time.monotonic() - app["started"])
            if cmd == "ping":
                if role == "game":
                    reply = f"pong role=game up={up} beat={up * 60} exiting=0"
                else:
                    reply = f"pong role=waiter up={up} last=0:none hold=0 auto=1 countdown=3 crashes=0"
            elif cmd == "launch":
                reply = "ok launching sdmc:/switch/game.nro"
            elif cmd == "auto":
                reply = "ok auto on"
            else:
                reply = f"err unknown command {cmd}"
            self.rig.sendto(reply.encode("ascii"), addr)

    def serve_ctl(self, conn):
        f = conn.makefile("rb")
        for raw in f:
            try:
                req = json.loads(raw)
                resp = {"ok": True, **(self.control(req) or {})}
            except Exception as e:  # noqa: BLE001 - report anything to the caller
                resp = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(resp) + "\n").encode())

    def control(self, req):
        c = self.console
        cmd = req.get("cmd")
        if cmd == "state":
            return c.snapshot()
        if cmd == "set":
            c.set(**req.get("values", {}))
            return c.snapshot()
        if cmd == "connect":
            if c.screen == "asleep":
                raise RuntimeError("device does not answer")
            with c.lock:
                c.twili_connected = True
            return {}
        if cmd == "ps":
            with c.lock:
                if c.screen == "asleep" or not c.twili_connected:
                    return {"devices": 0, "procs": []}
                procs = [{"pid": 0x83, "tid": 0x420000000000000F, "name": "sys-ftpd"}]
                if c.botbase:
                    procs.append({"pid": 0x84, "tid": 0x430000000000000B, "name": "sys-botbase"})
                if c.app is not None:
                    procs.append({"pid": c.app["pid"], "tid": HBL_TITLE, "name": "hbloader"})
                return {"devices": 1, "procs": procs}
        if cmd == "terminate":
            if c.screen == "asleep":
                raise RuntimeError("device does not answer")
            if not c.terminate(int(req["pid"])):
                raise RuntimeError("no such process")
            return {}
        if cmd == "launch":
            if c.screen == "asleep":
                raise RuntimeError("device does not answer")
            if req.get("tid", "").upper().removeprefix("0X") != BOTBASE_TITLE:
                raise RuntimeError("the sim only launches sys-botbase")
            delay = float(req.get("delay", 0.3))

            def later():
                time.sleep(delay)
                c.set(botbase=True)
            threading.Thread(target=later, daemon=True).start()
            with c.lock:
                c.events.append("launch sys-botbase")
            return {}
        raise ValueError(f"unknown control command {cmd!r}")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--botbase-port", type=int, default=6000)
    p.add_argument("--rig-port", type=int, default=5600)
    p.add_argument("--ftp-port", type=int, default=5000)
    p.add_argument("--ctl-port", type=int, default=6099)
    p.add_argument("--ports-file")
    p.add_argument("--screen", default="home", choices=SCREENS)
    p.add_argument("--tiles", default="app,app,other", type=lambda s: s.split(","))
    p.add_argument("--user-select", action="store_true")
    p.add_argument("--land", default="game", choices=("game", "waiter"))
    p.add_argument("--deaf", action="store_true", help="the app runs but the rig does not answer")
    p.add_argument("--no-botbase", action="store_true", help="sys-botbase not running")
    p.add_argument("--no-capture", action="store_true", help="pixelPeek returns an empty line")
    p.add_argument("--boot-delay", type=float, default=0.5)
    p.add_argument("--sd-dir", help="SD card root served over FTP (default: a temporary directory)")
    p.add_argument("--clock-offset-s", type=float, default=0.0,
                   help="the console clock's offset from the host's (album file names)")
    p.add_argument("--album-seed", type=float, nargs="*", default=[],
                   help="epoch times of album images present at start")
    p.add_argument("--capture-refuse", default="", type=lambda s: [x for x in s.split(",") if x],
                   help="screens on which CAPTURE saves nothing")
    p.add_argument("--capture-delay", type=float, default=0.2,
                   help="seconds before a capture appears in the album")
    a = p.parse_args(argv)
    if any(t not in ("app", "other") for t in a.tiles):
        p.error("--tiles: comma-separated app/other")
    return a


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    deaf = args.deaf
    sim = Sim(args)
    if deaf:
        sim.console.set(deaf=True)
    ports = sim.ports()
    if args.ports_file:
        tmp = args.ports_file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(ports, fh)
        os.replace(tmp, args.ports_file)
    log(f"listening {ports} screen={sim.console.screen} tiles={args.tiles}")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    if sim.own_sd:
        shutil.rmtree(sim.sd, ignore_errors=True)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
