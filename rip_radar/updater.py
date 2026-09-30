"""Checks GitHub Releases for a newer RipRadar.exe and swaps it in on restart."""
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from . import GITHUB_REPO, __version__, paths

log = logging.getLogger("rip_radar")
CREATE_NO_WINDOW = 0x08000000
ASSET = "RipRadar.exe"


def _ver(tag):
    parts = []
    for p in str(tag).lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts + [0] * (3 - len(parts)))


def check():
    """Returns {"version","notes","url","size"} if a newer release exists, else None."""
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                     headers={"Accept": "application/vnd.github+json"}, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    rel = r.json()
    if _ver(rel.get("tag_name", "0")) <= _ver(__version__):
        return None
    asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
    if not asset:
        return None
    notes = (rel.get("body") or "").split("<!-- selftest -->")[0].strip()
    return {"version": rel["tag_name"].lstrip("v"), "notes": notes[:1500],
            "url": asset["browser_download_url"], "size": asset.get("size", 0)}


def download_and_restart(info, quit_app):
    """Download the new exe next to the current one, then a tiny script swaps it in once we exit."""
    if not paths.FROZEN or os.name != "nt":
        raise RuntimeError("Updates only apply to the installed Windows app.")
    exe = Path(sys.executable)
    new = exe.with_name("RipRadar.new.exe")
    with requests.get(info["url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(new, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    if new.stat().st_size < 5_000_000 or (info.get("size") and new.stat().st_size != info["size"]):
        new.unlink(missing_ok=True)
        raise RuntimeError("Download was incomplete - try again.")
    script = Path(tempfile.gettempdir()) / "ripradar_update.cmd"
    script.write_text(
        "@echo off\r\n"
        "set n=0\r\n"
        ":wait\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        f'move /y "{new}" "{exe}" >nul 2>&1 && goto done\r\n'
        "set /a n+=1\r\n"
        "if %n% lss 90 goto wait\r\n"
        ":done\r\n"
        f'start "" "{exe}" --updated\r\n'
        'del "%~f0"\r\n', encoding="ascii")
    from .winsys import launch_new_copy
    launch_new_copy(["cmd", "/c", str(script)])   # the script's `start` inherits the clean environment
    log.info("update %s downloaded; restarting", info["version"])
    quit_app()
