"""Screen classification by nearest reference image. No OCR, no learning.

Each image is cropped to ``classify.crop`` (fractions of the screen), then
downsampled to ``classify.grid`` (columns x rows) mean colours. The distance
between two grids is the mean Euclidean RGB distance over the cells (0 for
identical, at most ~441). The nearest reference wins if its distance is at
most ``classify.threshold``; otherwise the screen is ``unknown``.

References live in ``refs/<name>.jpg`` with ``refs/index.json``. A
reference's *label* is its name, or for names like ``home-2`` /
``user-select-night`` the known label it starts with, so several references
can stand for one screen.
"""
from __future__ import annotations

import io
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

LABELS = ("home", "dialog", "game", "waiter", "user-select")
UNKNOWN = "unknown"


class PillowMissing(Exception):
    def __init__(self):
        super().__init__(
            "Pillow is not installed, so screenshots cannot be decoded or classified. "
            "Install it with: python3 -m pip install --user pillow")


def _pil():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        raise PillowMissing() from None
    return Image


def pillow_available() -> bool:
    try:
        _pil()
        return True
    except PillowMissing:
        return False


def label_of(name: str) -> str:
    for lab in sorted(LABELS, key=len, reverse=True):
        if name == lab or name.startswith(lab + "-"):
            return lab
    return name


def grid_of(jpeg: bytes, grid=(16, 9), crop=(0.0, 0.0, 1.0, 1.0)) -> list[tuple[float, float, float]]:
    Image = _pil()
    with Image.open(io.BytesIO(jpeg)) as im:
        im = im.convert("RGB")
        w, h = im.size
        box = (round(crop[0] * w), round(crop[1] * h), round(crop[2] * w), round(crop[3] * h))
        im = im.crop(box)
        # BOX resampling = the mean of each cell's source pixels
        small = im.resize((int(grid[0]), int(grid[1])), Image.Resampling.BOX)
        raw = small.tobytes()
        return [(float(raw[i]), float(raw[i + 1]), float(raw[i + 2])) for i in range(0, len(raw), 3)]


def distance(a, b) -> float:
    if len(a) != len(b):
        raise ValueError("grids differ in size")
    return sum(math.dist(p, q) for p, q in zip(a, b)) / len(a)


@dataclass
class Result:
    label: str                 # home/dialog/... or "unknown"
    ref: str | None            # nearest reference name (even if over threshold)
    distance: float | None
    ranking: list = field(default_factory=list)   # [(name, distance)] nearest first
    note: str = ""

    def summary(self) -> str:
        if self.ref is None:
            return f"{self.label} ({self.note or 'no references'})"
        others = ", ".join(f"{n}={d:.1f}" for n, d in self.ranking[1:4])
        s = f"{self.label} (nearest {self.ref} d={self.distance:.1f}"
        if others:
            s += f"; next {others}"
        return s + ")" + (f" {self.note}" if self.note else "")


class Classifier:
    def __init__(self, refs_dir: Path, grid=(16, 9), crop=(0.0, 0.12, 1.0, 1.0),
                 threshold: float = 40.0):
        self.refs_dir = Path(refs_dir)
        self.grid = tuple(grid)
        self.crop = tuple(crop)
        self.threshold = float(threshold)
        self._refs: dict[str, list] | None = None

    @classmethod
    def from_config(cls, cfg) -> "Classifier":
        c = cfg.classify
        return cls(cfg.path(c.refs_dir), c.grid, c.crop, c.threshold)

    # --- reference store ------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.refs_dir / "index.json"

    def index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            return {}

    def ref_names(self) -> list[str]:
        idx = self.index()
        return sorted(n for n in idx if (self.refs_dir / idx[n]["file"]).is_file())

    def learn(self, name: str, jpeg: bytes) -> Path:
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError(f"bad reference name {name!r} (letters, digits, - and _ only)")
        grid_of(jpeg, self.grid, self.crop)  # validates the image (and Pillow)
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        path = self.refs_dir / f"{name}.jpg"
        path.write_bytes(jpeg)
        idx = self.index()
        idx[name] = {"file": path.name, "label": label_of(name),
                     "learned": time.strftime("%Y-%m-%d %H:%M:%S")}
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.index_path)
        self._refs = None
        return path

    def _load(self) -> dict[str, list]:
        if self._refs is None:
            refs = {}
            idx = self.index()
            for name in sorted(idx):
                p = self.refs_dir / idx[name]["file"]
                if p.is_file():
                    refs[name] = grid_of(p.read_bytes(), self.grid, self.crop)
            self._refs = refs
        return self._refs

    def has_refs(self) -> bool:
        return bool(self.ref_names())

    # --- classification ---------------------------------------------------

    def classify(self, jpeg: bytes | None) -> Result:
        if jpeg is None:
            return Result(UNKNOWN, None, None, note="no screenshot")
        if not self.ref_names():
            return Result(UNKNOWN, None, None, note="no references recorded; run coldstart learn")
        refs = self._load()
        g = grid_of(jpeg, self.grid, self.crop)
        ranking = sorted(((n, distance(g, r)) for n, r in refs.items()), key=lambda x: x[1])
        name, d = ranking[0]
        label = label_of(name) if d <= self.threshold else UNKNOWN
        note = "" if label != UNKNOWN else f"over threshold {self.threshold:g}"
        return Result(label, name, d, ranking, note)
