"""Unit tests for the sys-botbase client and the rig's UDP protocol, against
small in-process servers (not the simulator)."""
import socket
import threading
import unittest

from coldstart import rig
from coldstart.botbase import (Botbase, BotbaseError, BotbaseUnavailable, CaptureUnavailable,
                               port_answers)


class FakeBotbase:
    """Records every received line; replies from a dict of command -> line."""

    def __init__(self, replies=None, close_after=None):
        self.replies = replies or {}
        self.lines = []
        self.close_after = close_after
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.raw = b""
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                self.raw += data
                buf += data
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    cmd = line.decode().strip()
                    self.lines.append(cmd)
                    if self.close_after and cmd == self.close_after:
                        return
                    if cmd in self.replies:
                        conn.sendall(self.replies[cmd].encode() + b"\n")

    def close(self):
        self.srv.close()


class BotbaseTests(unittest.TestCase):
    def fake(self, **kw):
        f = FakeBotbase(**kw)
        self.addCleanup(f.close)
        return f

    def test_commands_are_crlf_terminated(self):
        f = self.fake(replies={"getVersion": "2.5"})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            bb.click("HOME")
            bb.click("DLEFT")
            bb.release("R")
            bb.configure("buttonClickSleepTime", 80)
            self.assertEqual(bb.get_version(), "2.5")
        self.assertEqual(f.raw, b"click HOME\r\nclick DLEFT\r\nrelease R\r\n"
                                b"configure buttonClickSleepTime 80\r\ngetVersion\r\n")

    def test_unknown_button_is_never_sent(self):
        # sys-botbase presses A for any name it does not know
        f = self.fake(replies={"getVersion": "2.5"})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            with self.assertRaisesRegex(BotbaseError, "would press A"):
                bb.click("DPAD_LEFT")
            with self.assertRaises(BotbaseError):
                bb.click("a")
            bb.get_version()
        self.assertEqual(f.lines, ["getVersion"])

    def test_r_is_never_held(self):
        f = self.fake(replies={"getVersion": "2.5"})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            with self.assertRaisesRegex(BotbaseError, "real application"):
                bb.press("R")
            bb.press("ZR")
            bb.get_version()
        self.assertEqual(f.lines, ["press ZR", "getVersion"])

    def test_line_breaks_in_commands_rejected(self):
        f = self.fake()
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            with self.assertRaises(BotbaseError):
                bb.send("click A\r\nclick B")

    def test_pixel_peek_decodes_hex_jpeg(self):
        jpeg = b"\xff\xd8\xff\xe0" + bytes(range(50)) + b"\xff\xd9"
        f = self.fake(replies={"pixelPeek": jpeg.hex().upper()})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            self.assertEqual(bb.pixel_peek(), jpeg)

    def test_pixel_peek_empty_line_means_no_debug_mode(self):
        f = self.fake(replies={"pixelPeek": ""})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            with self.assertRaisesRegex(CaptureUnavailable, "enable_am_debug_mode"):
                bb.pixel_peek()

    def test_pixel_peek_garbage(self):
        f = self.fake(replies={"pixelPeek": "zz", "getVersion": "2.5"})
        with Botbase("127.0.0.1", f.port, timeout=2) as bb:
            with self.assertRaisesRegex(BotbaseError, "not hex"):
                bb.pixel_peek()

    def test_refused_and_silent_and_closed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(BotbaseUnavailable):
            Botbase("127.0.0.1", port, timeout=1).connect()
        self.assertFalse(port_answers("127.0.0.1", port, 0.5))
        f = self.fake()  # never replies
        with Botbase("127.0.0.1", f.port, timeout=0.3) as bb:
            with self.assertRaisesRegex(BotbaseUnavailable, "no reply"):
                bb.get_version()
        f2 = self.fake(close_after="getVersion")
        with Botbase("127.0.0.1", f2.port, timeout=2) as bb:
            with self.assertRaisesRegex(BotbaseUnavailable, "closed"):
                bb.get_version()


class RigTests(unittest.TestCase):
    def responder(self, reply):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        got = []

        def serve():
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                return
            got.append(data)
            if reply is not None:
                s.sendto(reply.encode(), addr)
        threading.Thread(target=serve, daemon=True).start()
        return s.getsockname()[1], got

    def test_parse_game_and_waiter(self):
        r = rig.parse_reply("pong role=game up=12 beat=700 exiting=0")
        self.assertEqual((r.word, r.role, r.fields["beat"]), ("pong", "game", "700"))
        r = rig.parse_reply("pong role=waiter up=3 last=0x2a:crash hold=1 auto=0 countdown=5 crashes=2\n")
        self.assertEqual(r.role, "waiter")
        self.assertEqual(r.fields["last"], "0x2a:crash")
        self.assertIn("crashes=2", rig.describe(r))
        self.assertTrue(rig.parse_reply("ok launching sdmc:/x.nro").ok)
        self.assertFalse(rig.parse_reply("err no").ok)
        self.assertEqual(rig.parse_reply("").word, "")

    def test_ping(self):
        port, got = self.responder("pong role=game up=1 beat=60 exiting=0")
        r = rig.ping("127.0.0.1", port, 1.0)
        self.assertEqual(r.role, "game")
        self.assertEqual(got, [b"ping"])

    def test_ping_silence_and_non_pong(self):
        port, _ = self.responder(None)
        self.assertIsNone(rig.ping("127.0.0.1", port, 0.2))
        port, _ = self.responder("ok auto on")
        self.assertIsNone(rig.ping("127.0.0.1", port, 0.5))


if __name__ == "__main__":
    unittest.main()
