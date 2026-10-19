"""Test harness: run botbase_sim as a subprocess and the coldstart CLI against it."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SIM = REPO / "sim" / "botbase_sim.py"
SIM_BIN = REPO / "sim" / "bin"

FAST = {
    "launch": {"settle_home_s": 0.1, "settle_dpad_s": 0.0, "settle_a_s": 0.3,
               "settle_dialog_s": 0.1, "wait_rig_s": 2.0, "rig_poll_s": 0.2},
    "supervise": {"poll_s": 0.2, "silent_s": 0.6, "heartbeat_s": 600.0, "attempts": 3,
                  "backoff_s": [0.3], "human_poll_s": 0.3, "human_rearm_s": 0.0},
    "twili": {"timeout_s": 10.0, "terminate_wait_s": 2.0},
    "rig": {"ping_timeout_s": 0.2},
    "botbase": {"timeout_s": 3.0, "shot_timeout_s": 5.0},
}


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return repr(v)


def write_toml(path: Path, data: dict) -> None:
    lines = []
    for sec, values in data.items():
        lines.append(f"[{sec}]")
        lines += [f"{k} = {toml_value(v)}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n")


class Sim:
    """One simulator process plus a config file and environment that point the
    tool at it. Use as a context manager; the process is always reaped."""

    def __init__(self, *sim_args: str, refs_dir: Path | None = None, extra: dict | None = None,
                 env: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="coldstart-test-"))
        self.ports_file = self.tmp / "ports.json"
        self.sim_log = open(self.tmp / "sim.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(SIM), "--botbase-port", "0", "--rig-port", "0", "--ftp-port", "0",
             "--ctl-port", "0", "--boot-delay", "0.2", "--ports-file", str(self.ports_file), *sim_args],
            stdin=subprocess.DEVNULL, stdout=self.sim_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not self.ports_file.exists():
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("simulator did not start: " + (self.tmp / "sim.log").read_text())
            time.sleep(0.02)
        self.ports = json.loads(self.ports_file.read_text())
        data = {
            "console": {"host": "127.0.0.1", "ftp_port": self.ports["ftp"],
                        "twili_port": self.ports["ftp"], "probe_timeout_s": 0.5},
            "botbase": {"port": self.ports["botbase"]},
            "rig": {"port": self.ports["rig"]},
            "twili": {"twib": "twib", "twibd": "twibd"},
            "classify": {"refs_dir": str(refs_dir) if refs_dir else str(self.tmp / "refs")},
            "paths": {"runs_dir": str(self.tmp / "runs")},
        }
        for sec, values in FAST.items():
            data.setdefault(sec, {}).update(values)
        for sec, values in (extra or {}).items():
            data.setdefault(sec, {}).update(values)
        self.config = self.tmp / "coldstart.toml"
        write_toml(self.config, data)
        self.env = dict(os.environ)
        for var in ("COLDSTART_HOST", "SWITCH_IP", "COLDSTART_BOTBASE_PORT", "COLDSTART_RIG_PORT",
                    "COLDSTART_FTP_PORT", "COLDSTART_TWILI_PORT", "COLDSTART_CONFIG"):
            self.env.pop(var, None)
        self.env["PATH"] = f"{SIM_BIN}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["PYTHONPATH"] = str(REPO)
        self.env["COLDSTART_SIM_CTL"] = f"127.0.0.1:{self.ports['ctl']}"
        self.env["FAKE_TWIB_LOG"] = str(self.tmp / "twib.log")
        self.env.update(env or {})

    # --- simulator control -------------------------------------------------

    def ctl(self, **req) -> dict:
        with socket.create_connection(("127.0.0.1", self.ports["ctl"]), timeout=5) as s:
            s.sendall((json.dumps(req) + "\n").encode())
            resp = json.loads(s.makefile("rb").readline())
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error"))
        return resp

    def state(self) -> dict:
        return self.ctl(cmd="state")

    def set(self, **values) -> dict:
        return self.ctl(cmd="set", values=values)

    def twib_calls(self) -> list[str]:
        p = self.tmp / "twib.log"
        return p.read_text().splitlines() if p.exists() else []

    # --- the tool ----------------------------------------------------------

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "coldstart", "--config", str(self.config), *args]

    def run(self, *args: str, timeout: float = 60, env: dict | None = None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run(self.argv(*args), env=e, cwd=self.tmp, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    def spawn(self, *args: str, env: dict | None = None):
        e = dict(self.env)
        e.update(env or {})
        out = open(self.tmp / "spawn.log", "w")
        p = subprocess.Popen(self.argv(*args), env=e, cwd=self.tmp, stdout=out,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        p._outfile = out  # noqa: SLF001
        return p

    def spawn_output(self) -> str:
        p = self.tmp / "spawn.log"
        return p.read_text() if p.exists() else ""

    def wait_for_output(self, needle: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.spawn_output():
            if time.monotonic() > deadline:
                raise AssertionError(f"{needle!r} not seen in:\n{self.spawn_output()}")
            time.sleep(0.05)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.sim_log.close()
        if not os.environ.get("COLDSTART_KEEP_TEST_DIRS"):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def stop(proc: subprocess.Popen) -> None:
    """Reap a spawned process, whatever state it is in."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    f = getattr(proc, "_outfile", None)
    if f:
        f.close()


_REFS: Path | None = None


def shared_refs() -> Path:
    """Reference screens recorded once per test run with `coldstart learn`
    against the simulator (exercising learn itself)."""
    global _REFS
    if _REFS is not None and (_REFS / "index.json").exists():
        return _REFS
    _REFS = Path(tempfile.mkdtemp(prefix="coldstart-refs-"))
    atexit.register(shutil.rmtree, _REFS, True)
    with Sim(refs_dir=_REFS) as sim:
        for screen in ("home", "dialog", "user-select", "game", "waiter"):
            sim.set(screen=screen, cursor=0)
            r = sim.run("learn", screen)
            if r.returncode != 0:
                raise RuntimeError(f"learn {screen} failed: {r.stdout}{r.stderr}")
    return _REFS
