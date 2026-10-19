#!/usr/bin/env python3
"""pad.py -- drive the console's HOME menu from the host through sys-botbase.

sys-botbase (sdmc:/atmosphere/contents/430000000000000B) is a sysmodule that
takes one TCP connection on port 6000 and turns text commands into a virtual
controller, a touch screen and a screenshot. It is the one lever that still
works when nothing of ours is running on the console: a process that died,
hbmenu, the HOME menu after a reboot. With hbl's override key inverted
(override_any_app_key=!R) and the waiter installed as sdmc:/hbmenu.nro, "press
A on the game tile" is a complete cold start of the loop.

    pad.py click A|B|X|Y|L|R|ZL|ZR|PLUS|MINUS|HOME|CAPTURE|DUP|DDOWN|DLEFT|DRIGHT|LSTICK|RSTICK
    pad.py press R / release R             hold / let go
    pad.py shot [file.jpg]                 screenshot (needs Atmosphere debugmode, default on)
    pad.py raw <text>                      any sys-botbase command, print the reply
    pad.py home-launch [--tile N]          HOME, select tile N (0 = leftmost) and press A,
                                           then wait for the rig to answer
    pad.py wait-port [seconds]             block until port 6000 answers (sysmodule up)

SWITCH_IP selects the console (default 192.168.128.150).
"""
import os
import socket
import sys
import time

IP = os.environ.get("SWITCH_IP", "192.168.128.150")
PORT = int(os.environ.get("BOTBASE_PORT", "6000"))
HERE = os.path.dirname(os.path.abspath(__file__))


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Pad:
    def __init__(self, timeout=5.0):
        self.s = socket.create_connection((IP, PORT), timeout=timeout)
        self.s.settimeout(timeout)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def send(self, text):
        self.s.sendall((text + "\r\n").encode())

    def ask(self, text, timeout=5.0):
        """Send a command and read one line of reply (commands that reply)."""
        self.s.settimeout(timeout)
        self.send(text)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace").strip()

    def click(self, btn, settle=0.35):
        self.send(f"click {btn}")
        time.sleep(settle)

    def press(self, btn):
        self.send(f"press {btn}")

    def release(self, btn):
        self.send(f"release {btn}")

    def screenshot(self, path):
        hexdata = self.ask("pixelPeek", timeout=15.0)
        if not hexdata or len(hexdata) < 100:
            raise SystemExit(f"pixelPeek returned {len(hexdata)} chars: Atmosphere debugmode off?")
        with open(path, "wb") as fh:
            fh.write(bytes.fromhex(hexdata))
        return path


def port_open(timeout=1.5):
    try:
        with socket.create_connection((IP, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if port_open():
            return True
        time.sleep(2.0)
    return False


def rig_role():
    """Ask the rig's control port who is there, without importing rig.py."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.sendto(b"ping", (IP, int(os.environ.get("RIG_CTL_PORT", "5600"))))
        data, _ = s.recvfrom(4096)
        for tok in data.decode(errors="replace").split():
            if tok.startswith("role="):
                return tok[5:]
    except OSError:
        return None
    finally:
        s.close()
    return None


def home_launch(tile=0, shots_dir=None):
    """HOME menu -> select tile -> A. Takes screenshots before and after so a
    human (or Claude) can see what happened when it did not work."""
    p = Pad()
    try:
        say("version: " + p.ask("getVersion"))
        p.click("HOME", settle=1.5)
        p.click("HOME", settle=1.5)          # second HOME from a game returns to the menu
        if shots_dir:
            p.screenshot(os.path.join(shots_dir, "home-before.jpg"))
        # Tiles row: park the cursor at the leftmost tile, then move right.
        for _ in range(12):
            p.click("DLEFT", settle=0.15)
        for _ in range(tile):
            p.click("DRIGHT", settle=0.2)
        p.click("A", settle=2.5)
        # A user picker, if any: the first profile is highlighted; A again.
        if shots_dir:
            p.screenshot(os.path.join(shots_dir, "after-a.jpg"))
        p.click("A", settle=1.0)
    finally:
        p.close()
    say("launched; waiting for the rig")
    t0 = time.time()
    while time.time() - t0 < 120:
        r = rig_role()
        if r:
            say(f"rig answers as {r}")
            return True
        time.sleep(2.0)
    say("no rig answer within 120 s")
    return False


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "wait-port":
        secs = float(argv[2]) if len(argv) > 2 else 300.0
        ok = wait_port(secs)
        say("port 6000 " + ("up" if ok else "never answered"))
        return 0 if ok else 1
    if cmd == "home-launch":
        tile = 0
        if "--tile" in argv:
            tile = int(argv[argv.index("--tile") + 1])
        shots = os.path.join(HERE, "logs")
        os.makedirs(shots, exist_ok=True)
        return 0 if home_launch(tile, shots) else 1
    p = Pad()
    try:
        if cmd == "click":
            p.click(argv[2])
        elif cmd == "press":
            p.press(argv[2])
        elif cmd == "release":
            p.release(argv[2])
        elif cmd == "shot":
            path = argv[2] if len(argv) > 2 else os.path.join(HERE, "logs", "shot.jpg")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            say("saved " + p.screenshot(path))
        elif cmd == "raw":
            print(p.ask(" ".join(argv[2:])))
        else:
            print(__doc__)
            return 2
    finally:
        p.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
