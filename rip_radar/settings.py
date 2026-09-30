"""User settings (settings.json in %APPDATA%\\RipRadar). Secrets live only here, never in the repo."""
import copy
import json
import threading

from . import paths

DEFAULTS = {
    "discord_webhook": "",
    "discord_webhook_urgent": "",
    # one channel per store; empty = that store's alerts go to discord_webhook
    "webhooks": {"topps": "", "pokemon": "", "walmart": "", "target": "", "dicks": "", "amazon": "", "bestbuy": ""},
    "ntfy_topic": "",
    "twilio": {"account_sid": "", "auth_token": "", "from": "", "to": ""},
    "sms_for": ["urgent"],
    "bot_token": "",
    "status_every_minutes": 5,
    "sports": ["Baseball", "Basketball", "Football"],
    "start_with_windows": True,
    "auto_update": True,             # install updates by itself while the app is in the tray
    "paused": False,
    "disabled_sources": [],
    # pages you add in the app: {"name","url","preset","keywords","enabled"}
    "watch_pages": [],
}

_lock = threading.Lock()


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        try:
            data = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        merged = _merge(DEFAULTS, data)
        old = merged.pop("discord_webhook_pokemon", "")
        if old and not merged["webhooks"].get("pokemon"):
            merged["webhooks"]["pokemon"] = old
        return merged


def save(settings):
    with _lock:
        paths.ensure_dirs()
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_FILE)


def update(changes):
    s = _merge(load(), changes)
    save(s)
    return s
