"""Configuration: one TOML file plus environment overrides.

Every key has a default (DEFAULTS below), so an empty or missing file is a
valid configuration. The file is looked up in this order: the ``--config``
argument, ``$COLDSTART_CONFIG``, ``./coldstart.toml``. Unknown sections or
keys and wrongly typed values are errors, so a typo cannot silently fall back
to a default.

Environment overrides (applied after the file):

    COLDSTART_HOST (or SWITCH_IP)   console.host
    COLDSTART_BOTBASE_PORT          botbase.port
    COLDSTART_RIG_PORT              rig.port
    COLDSTART_FTP_PORT              console.ftp_port
    COLDSTART_TWILI_PORT            console.twili_port
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict[str, dict] = {
    "console": {
        "host": "192.168.128.150",
        # Services that always run on the console. "The console answers" means
        # at least one of them accepts a TCP connection (sys-ftpd, twili).
        "ftp_port": 5000,
        "twili_port": 15152,
        "probe_timeout_s": 2.0,
        # Also try one ICMP echo through the system `ping` (off: a sleeping
        # Switch may still answer ICMP, so TCP is the better signal).
        "icmp": False,
    },
    "botbase": {
        "port": 6000,
        "timeout_s": 5.0,
        "shot_timeout_s": 15.0,
        # >0: send `configure buttonClickSleepTime <ms>` after connecting
        # (sys-botbase's own default is 50 ms).
        "click_hold_ms": 0,
    },
    "screenshot": {
        # "album": CAPTURE + fetch the new JPEG from the SD card's album over
        # FTP (works without debug mode). "pixelpeek": sys-botbase pixelPeek
        # (needs capsrv's debug capture, refused on the rig's console).
        "source": "album",
        "capture_wait_s": 2.5,
        "capture_timeout_s": 8.0,
        "album_root": "/Nintendo/Album",
        "delete_after": True,
        "ftp_timeout_s": 10.0,
    },
    "rig": {
        "port": 5600,
        "ping_timeout_s": 1.0,
    },
    "twili": {
        # twili is optional (it no longer runs on the console by default).
        # Disabled or unreachable: a deaf/suspended application is closed
        # through the HOME menu (HOME, X, A) during the launch instead.
        "enabled": False,
        "twib": "twib",
        "twibd": "twibd",
        # Empty: start our own twibd with a socket in the run directory.
        # Set: use the twibd already listening on that socket.
        "socket": "",
        "hbl_title_id": "01007ef00011e000",
        "timeout_s": 15.0,
        "terminate_wait_s": 5.0,
    },
    "launch": {
        "tile": 0,
        "dleft_presses": 12,
        "settle_home_s": 2.5,
        "settle_dpad_s": 0.4,
        "settle_a_s": 3.0,
        "settle_dialog_s": 2.0,
        # after A on the HOME menu's "Close software" prompt
        "settle_close_s": 4.0,
        "max_normalize_steps": 5,
        "wait_rig_s": 120.0,
        "rig_poll_s": 2.0,
    },
    "classify": {
        "refs_dir": "refs",
        "grid": [16, 9],
        # Compared region as fractions of the screen: x0, y0, x1, y1. The
        # default drops the top 12 % (HOME menu clock, battery, user icon).
        "crop": [0.0, 0.12, 1.0, 1.0],
        # Mean per-cell RGB distance (0..441) above which a screen is unknown.
        "threshold": 40.0,
    },
    "supervise": {
        "poll_s": 5.0,
        "silent_s": 60.0,
        "heartbeat_s": 600.0,
        "attempts": 3,
        # Waits between launch attempts (the last value repeats if short).
        "backoff_s": [60.0, 120.0],
        "human_poll_s": 30.0,
        # After "launch failed N times": try again after this long (0 = never;
        # a human's launch is noticed either way).
        "human_rearm_s": 1800.0,
    },
    "notify": {
        # POST a JSON body here on "needs a human" (empty = off).
        "webhook": "",
        "timeout_s": 5.0,
    },
    "paths": {
        "runs_dir": "runs",
    },
}

ENV_OVERRIDES = [
    ("COLDSTART_HOST", "console", "host"),
    ("SWITCH_IP", "console", "host"),
    ("COLDSTART_BOTBASE_PORT", "botbase", "port"),
    ("COLDSTART_RIG_PORT", "rig", "port"),
    ("COLDSTART_FTP_PORT", "console", "ftp_port"),
    ("COLDSTART_TWILI_PORT", "console", "twili_port"),
]


class ConfigError(Exception):
    pass


class Section:
    """Attribute access to one config section (cfg.launch.tile)."""

    def __init__(self, name: str, values: dict):
        self._name = name
        self._values = values

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(f"{self._name}.{key}") from None

    def as_dict(self) -> dict:
        return dict(self._values)


class Config:
    def __init__(self, data: dict, source: str | None, overrides: list[str], base_dir: Path):
        self.data = data
        self.source = source
        self.overrides = overrides
        self.base_dir = base_dir
        for name, values in data.items():
            setattr(self, name, Section(name, values))

    def path(self, p: str) -> Path:
        """Resolve a configured path relative to the config file's directory."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else self.base_dir / q

    def dump(self) -> str:
        lines = [f"# config file: {self.source or '(none, defaults)'}"]
        for o in self.overrides:
            lines.append(f"# env override: {o}")
        for sec, values in self.data.items():
            lines.append(f"[{sec}]")
            for k, v in values.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return repr(v)


def _coerce(sec: str, key: str, default, value):
    where = f"{sec}.{key}"
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where}: expected true/false, got {value!r}")
    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    if isinstance(default, list):
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{where}: expected a non-empty list, got {value!r}")
        try:
            return [_coerce(sec, key, default[0], x) for x in value]
        except ConfigError:
            raise ConfigError(f"{where}: bad list element in {value!r}") from None
    raise ConfigError(f"{where}: unsupported type")


def _validate(data: dict) -> None:
    g = data["classify"]["grid"]
    if len(g) != 2 or min(g) < 1 or max(g) > 256:
        raise ConfigError("classify.grid: expected [columns, rows], each 1..256")
    c = data["classify"]["crop"]
    if len(c) != 4 or not (0.0 <= c[0] < c[2] <= 1.0 and 0.0 <= c[1] < c[3] <= 1.0):
        raise ConfigError("classify.crop: expected [x0, y0, x1, y1] fractions with x0<x1, y0<y1")
    if data["screenshot"]["source"] not in ("album", "pixelpeek"):
        raise ConfigError('screenshot.source: "album" or "pixelpeek"')
    if data["launch"]["tile"] < 0:
        raise ConfigError("launch.tile: must be >= 0")
    if data["supervise"]["attempts"] < 1:
        raise ConfigError("supervise.attempts: must be >= 1")
    try:
        int(data["twili"]["hbl_title_id"], 16)
    except ValueError:
        raise ConfigError("twili.hbl_title_id: expected a hex program id") from None
    for sec, key in (("botbase", "port"), ("rig", "port"), ("console", "ftp_port"),
                     ("console", "twili_port")):
        if not 0 < data[sec][key] < 65536:
            raise ConfigError(f"{sec}.{key}: not a port number")


def load(path: str | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    data = copy.deepcopy(DEFAULTS)
    source = None
    if path is None:
        path = env.get("COLDSTART_CONFIG") or None
        if path is None and Path("coldstart.toml").is_file():
            path = "coldstart.toml"
    base_dir = Path.cwd()
    if path is not None:
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text())
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {p}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: {e}") from None
        source = str(p.resolve())
        base_dir = p.resolve().parent
        for sec, values in raw.items():
            if sec not in DEFAULTS:
                raise ConfigError(f"{p}: unknown section [{sec}]")
            if not isinstance(values, dict):
                raise ConfigError(f"{p}: [{sec}] must be a table")
            for k, v in values.items():
                if k not in DEFAULTS[sec]:
                    raise ConfigError(f"{p}: unknown key {sec}.{k}")
                data[sec][k] = _coerce(sec, k, DEFAULTS[sec][k], v)
    overrides = []
    seen = set()
    for var, sec, key in ENV_OVERRIDES:
        if var in env and env[var] != "" and (sec, key) not in seen:
            seen.add((sec, key))
            raw_v = env[var]
            default = DEFAULTS[sec][key]
            if isinstance(default, int):
                try:
                    value = int(raw_v, 0)
                except ValueError:
                    raise ConfigError(f"${var}: expected an integer, got {raw_v!r}") from None
            else:
                value = raw_v
            data[sec][key] = value
            overrides.append(f"{var}={raw_v} -> {sec}.{key}")
    _validate(data)
    return Config(data, source, overrides, base_dir)
