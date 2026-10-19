"""Run directory and timestamped log lines.

Every command that touches the console gets ``runs/<timestamp>/`` with
``log.txt`` (the same lines as stdout) and the screenshots, named
``NN-<step>.jpg`` in the order they were taken.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path


def _stamp() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


class RunLog:
    def __init__(self, runs_dir: Path | None, name: str = "", stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.dir: Path | None = None
        self._fh = None
        self._shot_no = 0
        if runs_dir is not None:
            base = time.strftime("%Y%m%d-%H%M%S") + (f"-{name}" if name else "")
            runs_dir.mkdir(parents=True, exist_ok=True)
            d = runs_dir / base
            n = 1
            while d.exists():
                n += 1
                d = runs_dir / f"{base}.{n}"
            d.mkdir()
            self.dir = d
            self._fh = open(d / "log.txt", "a", encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"[{_stamp()}] {msg}"
        print(line, file=self.stream, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def shot_path(self, step: str) -> Path | None:
        if self.dir is None:
            return None
        self._shot_no += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", step).strip("-") or "shot"
        return self.dir / f"{self._shot_no:02d}-{safe}.jpg"

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class NullLog(RunLog):
    """Logs to a stream only; no run directory."""

    def __init__(self, stream=None):
        super().__init__(None, stream=stream)
