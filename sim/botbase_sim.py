#!/usr/bin/env python3
"""botbase_sim.py -- a fake console for testing switch-coldstart on the host.

Serves, on 127.0.0.1 by default:

- the sys-botbase protocol subset (TCP, default 6000): click/press/release
  <BTN>, getVersion, pixelPeek, configure <key> <value>; unknown buttons are
  pressed as A, exactly like sys-botbase;
- a fake rig control port (UDP, default 5600) answering ``ping`` while the
  game or waiter is in the foreground (and not "deaf");
- a stand-in for sys-ftpd (TCP, default 5000; a greeting only) so "the console
  answers TCP" can be told apart from "sys-botbase answers";
- a control port (TCP, JSON lines) used by the tests and by the fake ``twib``
  in sim/bin to read and change the state.

The fake console has screens home / dialog / user-select / game / waiter /
other / asleep, a cursor on a row of tiles ("app" or "other"), and an optional
hbl process. Rules: A on an app tile -> user-select (optional) -> game (or
waiter); A on an other tile -> other; A on the dialog -> home; HOME from the
game/waiter/other/user-select -> home (the app stays, suspended); DLEFT/DRIGHT
move the cursor within bounds; asleep closes every TCP port and silences the
rig. pixelPeek returns a synthetic JPEG with a distinct colour layout per
screen and a clock in the top bar that changes every second.

    botbase_sim.py [--screen home] [--tiles app,app] [--user-select] [--deaf]
                   [--land game|waiter] [--no-botbase] [--no-capture]
                   [--boot-delay S] [--ports-file F] [--<name>-port N]

Port 0 picks a free port; --ports-file receives the chosen ports as JSON.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import socket
import sys
import threading
import time

from PIL import Image, ImageDraw

BUTTONS = set("A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE DUP DDOWN DLEFT DRIGHT "
              "LSTICK RSTICK DL DU DR DD PALMA UNUSED".split())
ALIASES = {"DL": "DLEFT", "DU": "DUP", "DR": "DRIGHT", "DD": "DDOWN"}
SCREENS = ("home", "dialog", "user-select", "game", "waiter", "other", "asleep")
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
                if s in ("game", "waiter", "other", "user-select"):
                    self.screen = "home"
                    self.pending_launch = False
            elif btn == "A":
                if s == "dialog":
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
                if s == "user-select":
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
        if screen in ("home", "dialog"):
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


class Sim:
    def __init__(self, args):
        self.args = args
        self.console = Console(args)
        self.console.on_change = self.reconcile
        self.botbase_l = TcpListener("botbase", args.host, args.botbase_port, self.serve_botbase)
        self.ftp_l = TcpListener("ftp", args.host, args.ftp_port, self.serve_ftp)
        self.ctl_l = TcpListener("ctl", args.host, args.ctl_port, self.serve_ctl)
        self.rig = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rig.bind((args.host, args.rig_port))
        threading.Thread(target=self.serve_rig, daemon=True).start()
        self.reconcile()

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
        conn.sendall(b"220 botbase_sim ftp stand-in\r\n")
        conn.settimeout(2.0)
        try:
            conn.recv(1024)
        except OSError:
            pass

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
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
