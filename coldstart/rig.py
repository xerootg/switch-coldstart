"""The rig's UDP control port: one ASCII datagram out, one back.

    ping   -> pong role=game up=<s> beat=<n> exiting=<0|1>
           -> pong role=waiter up=<s> last=<code>:<reason> hold=<0|1> auto=<0|1>
                   countdown=<n> crashes=<n>
    launch -> ok launching <path>
    auto   -> ok auto on

Replies are parsed loosely: the first word, and ``key=value`` tokens.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field


@dataclass
class Reply:
    raw: str
    word: str
    fields: dict = field(default_factory=dict)

    @property
    def role(self) -> str | None:
        return self.fields.get("role")

    @property
    def ok(self) -> bool:
        return self.word in ("ok", "pong")


def parse_reply(text: str) -> Reply:
    words = text.strip().split()
    fields = {}
    for w in words[1:]:
        k, sep, v = w.partition("=")
        if sep and k:
            fields[k] = v
    return Reply(raw=text.strip(), word=words[0].lower() if words else "", fields=fields)


def ask(host: str, port: int, command: str, timeout: float = 1.0) -> Reply | None:
    """Send one datagram and wait for one reply; None if nothing answers."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(command.encode("ascii"), (host, port))
        data, _ = s.recvfrom(4096)
    except OSError:  # timeout, ICMP port unreachable, no route
        return None
    finally:
        s.close()
    return parse_reply(data.decode("ascii", errors="replace"))


def ping(host: str, port: int, timeout: float = 1.0) -> Reply | None:
    r = ask(host, port, "ping", timeout)
    if r is None or r.word != "pong":
        return None
    return r


def describe(r: Reply) -> str:
    keys = ("up", "beat", "exiting", "last", "hold", "auto", "countdown", "crashes")
    extra = " ".join(f"{k}={r.fields[k]}" for k in keys if k in r.fields)
    return f"role={r.role or '?'}" + (f" {extra}" if extra else "")
