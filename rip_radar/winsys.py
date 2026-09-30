"""Windows plumbing: one copy running at a time, self-install, shortcuts, start with Windows."""
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading

from . import APP_NAME, paths

log = logging.getLogger("rip_radar")
PORT = 47831                     # localhost only; used so a 2nd launch just brings the window forward
CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = os.name == "nt"


def fresh_env():
    """Environment for launching another copy of the app. A PyInstaller one-file exe otherwise passes its
    private temp-folder settings to the child, which then tries to load Python from the parent's folder -
    deleted as soon as the parent exits ("Failed to load Python DLL ... _MEIxxxx\\python312.dll")."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("_PYI_", "_MEI")) and k not in ("_MEIPASS2", "PYTHONHOME", "PYTHONPATH")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch_new_copy(args):
    """Start the app (or a helper script that starts it) as a brand-new, independent process."""
    flags = 0
    if IS_WINDOWS:
        flags = 0x00000200 | CREATE_NO_WINDOW   # NEW_PROCESS_GROUP, hidden console (cmd needs one)
    return subprocess.Popen(args, env=fresh_env(), close_fds=True, creationflags=flags)


def claim_single_instance(on_show, wait_seconds=0):
    """True if we're the only copy. Otherwise tell the running copy to show itself and return False.
    wait_seconds: after an update/install the old copy may still be exiting, so keep trying briefly."""
    import time
    deadline = time.time() + wait_seconds
    while True:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", PORT))
            break
        except OSError:
            srv.close()
            if time.time() < deadline:
                time.sleep(0.5)
                continue
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=3) as c:
                    c.sendall(b"show")
            except OSError:
                pass
            return False
    srv.listen(2)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        on_show()
            except Exception as e:
                log.warning("instance listener: %s", e)

    threading.Thread(target=serve, daemon=True, name="single-instance").start()
    return True


def _same(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def is_installed_copy():
    return _same(sys.executable, paths.INSTALLED_EXE)


def install_and_relaunch():
    """Frozen exe run from Downloads etc: copy to the install folder, add shortcuts, start the installed copy.
    Returns True if the caller should exit (the installed copy is starting)."""
    if not (paths.FROZEN and IS_WINDOWS) or is_installed_copy():
        return False
    try:
        paths.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sys.executable, paths.INSTALLED_EXE)
    except OSError as e:
        log.warning("install copy failed (%s) - running from current location", e)
        return False
    make_shortcuts(paths.INSTALLED_EXE)
    launch_new_copy([str(paths.INSTALLED_EXE), "--installed"])
    return True


def make_shortcuts(exe):
    """Desktop + Start Menu shortcuts via PowerShell (handles OneDrive-redirected Desktops)."""
    exe = str(exe).replace("'", "''")
    workdir = str(paths.INSTALL_DIR).replace("'", "''")
    ps = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$targets=@([Environment]::GetFolderPath('Desktop'),"
        "(Join-Path ([Environment]::GetFolderPath('Programs')) ''));"
        "foreach($d in $targets){"
        f"$s=$w.CreateShortcut((Join-Path $d '{APP_NAME}.lnk'));"
        f"$s.TargetPath='{exe}';$s.WorkingDirectory='{workdir}';$s.IconLocation='{exe},0';"
        "$s.Description='Drop, raffle and queue alerts';$s.Save()}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       timeout=30, creationflags=CREATE_NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("shortcut creation failed: %s", e)


def set_autostart(enabled):
    """HKCU Run key -> starts minimized to the tray at login."""
    if not (IS_WINDOWS and paths.FROZEN):
        return
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, "RipRadar", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
            else:
                try:
                    winreg.DeleteValue(k, "RipRadar")
                except FileNotFoundError:
                    pass
    except OSError as e:
        log.warning("autostart setting failed: %s", e)


def open_path(path):
    """Open a file or folder with its default Windows app."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa: S606 - user-initiated
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        log.warning("open failed: %s", e)


def desktop_dir():
    """The real Desktop (OneDrive-redirected ones included)."""
    from pathlib import Path
    if IS_WINDOWS:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                                 capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
            p = Path(out.stdout.strip())
            if out.stdout.strip() and p.exists():
                return p
        except (OSError, subprocess.SubprocessError):
            pass
    p = Path.home() / "Desktop"
    return p if p.exists() else Path.home()


def reveal(path):
    """Open Explorer with the file selected."""
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            open_path(os.path.dirname(str(path)))
    except OSError as e:
        log.warning("reveal failed: %s", e)
