"""Is the console there at all? And the optional "needs a human" webhook."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request


def tcp_accepts(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def icmp_answers(host: str, timeout: float) -> bool:
    ping = shutil.which("ping")
    if ping is None:
        return False
    try:
        p = subprocess.run([ping, "-c", "1", "-W", str(max(1, int(timeout))), host],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout + 2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def console_answers(cfg) -> tuple[bool, str]:
    """True if any always-on service (sys-ftpd, twili) accepts TCP, or (if
    enabled) ICMP echo answers. Returns (answers, what answered / was tried)."""
    c = cfg.console
    for name, port in (("sys-ftpd", c.ftp_port), ("twili", c.twili_port)):
        if tcp_accepts(c.host, port, c.probe_timeout_s):
            return True, f"{name} on TCP {port}"
    if c.icmp and icmp_answers(c.host, c.probe_timeout_s):
        return True, "ICMP echo"
    tried = f"TCP {c.ftp_port}/{c.twili_port}" + (" and ICMP" if c.icmp else "")
    return False, f"no answer on {tried}"


def notify(cfg, log, event: str, message: str, next_step: str, **extra) -> None:
    url = cfg.notify.webhook
    if not url:
        return
    body = {"event": event, "message": message, "next_step": next_step,
            "console": cfg.console.host, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **extra}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.notify.timeout_s) as r:
            log(f"notify: POST {url} -> {r.status}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"notify: POST {url} failed: {e}")
