"""Local state: config, watches, notification memory. Plain JSON files under
$BBT_HOME (default ~/.config/besser-bahn-tickets). Nothing leaves the machine
except the DB queries and the notifications you configure."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import tempfile

DEFAULT_CONFIG = {
    "notify": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "telegram_token": "",
        "telegram_chat_id": "",
        "command": "",
    },
    "defaults": {
        "bahncard": None,
        "first_class": False,
        "adults": 1,
        "dticket": False,
    },
}

ENV_OVERRIDES = {
    "BBT_NTFY_SERVER": ("notify", "ntfy_server"),
    "BBT_NTFY_TOPIC": ("notify", "ntfy_topic"),
    "BBT_TELEGRAM_TOKEN": ("notify", "telegram_token"),
    "BBT_TELEGRAM_CHAT_ID": ("notify", "telegram_chat_id"),
    "BBT_NOTIFY_COMMAND": ("notify", "command"),
}


def home() -> pathlib.Path:
    p = pathlib.Path(os.environ.get("BBT_HOME")
                     or pathlib.Path.home() / ".config" / "besser-bahn-tickets")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(name: str, default):
    f = home() / name
    try:
        return json.loads(f.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:
        raise SystemExit(f"{f} is not valid JSON — fix or delete it")


def _write(name: str, data) -> None:
    """Atomic: cron and the web UI may write concurrently."""
    f = home() / name
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)  # holds bot tokens
    os.replace(tmp, f)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for section, values in _read("config.json", {}).items():
        if isinstance(values, dict) and isinstance(cfg.get(section), dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    return cfg


def set_config(dotted: str, value) -> dict:
    raw = _read("config.json", {})
    section, _, key = dotted.partition(".")
    if not key or section not in DEFAULT_CONFIG or key not in DEFAULT_CONFIG[section]:
        valid = [f"{s}.{k}" for s, v in DEFAULT_CONFIG.items() for k in v]
        raise SystemExit(f"unknown key '{dotted}'. Valid: {', '.join(valid)}")
    raw.setdefault(section, {})[key] = value
    _write("config.json", raw)
    return load_config()


def load_watches() -> list[dict]:
    return _read("watches.json", [])


def save_watches(watches: list[dict]) -> None:
    _write("watches.json", watches)


def load_state() -> dict:
    return _read("state.json", {})


def save_state(state: dict) -> None:
    _write("state.json", state)


@contextlib.contextmanager
def lock(name: str = "check", blocking: bool = True):
    """Inter-process lock. "check" serialises whole check runs (cron + web UI)
    so two runs don't double-notify or trip the DB rate limit together;
    "watches" guards the short read-modify-write of watches.json. Yields
    False if non-blocking and busy."""
    try:
        import fcntl
    except ImportError:  # Windows: best effort, no lock
        yield True
        return
    fh = open(home() / f".{name}.lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        fh.close()


def update_watches(fn) -> list[dict]:
    """Read-modify-write watches.json under the "watches" lock."""
    with lock("watches"):
        watches = fn(load_watches())
        save_watches(watches)
        return watches
