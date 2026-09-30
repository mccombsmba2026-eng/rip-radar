"""Where things live on disk.

User data (settings, state, calendar, CSV, logs) goes in %APPDATA%\\RipRadar so it survives updates.
The installed app goes in %LOCALAPPDATA%\\Programs\\RipRadar\\RipRadar.exe."""
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def resource(*parts):
    """A file shipped inside the app (works both from source and inside the PyInstaller exe)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath("rip_radar", *parts)


def _base(env, fallback):
    root = os.environ.get(env)
    return Path(root) if root else Path.home() / fallback


DATA_DIR = _base("APPDATA", ".config") / "RipRadar"
INSTALL_DIR = _base("LOCALAPPDATA", ".local") / "Programs" / "RipRadar"
INSTALLED_EXE = INSTALL_DIR / "RipRadar.exe"
WEBVIEW_DIR = _base("LOCALAPPDATA", ".local") / "RipRadar" / "webview"

SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "state.json"
ICS_FILE = DATA_DIR / "drops.ics"
TOPPS_CSV = DATA_DIR / "topps_calendar.csv"
LOG_FILE = DATA_DIR / "rip-radar.log"


def ensure_dirs():
    for d in (DATA_DIR, WEBVIEW_DIR):
        d.mkdir(parents=True, exist_ok=True)
