"""Rip Radar desktop app: window + tray icon + background scanner + self-update."""
import argparse
import json
import logging
import logging.handlers
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__, paths, settings, updater, winsys

log = logging.getLogger("rip_radar")


def setup_logging():
    paths.ensure_dirs()
    h = logging.handlers.RotatingFileHandler(paths.LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    if sys.stdout:
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


class BrowserFetcher:
    """Loads a page in a hidden window running the real Edge engine (WebView2), then reads its HTML.
    Sites with bot walls (Pokémon Center, Walmart...) treat it like a normal browser visit."""

    def __init__(self):
        self.window = None
        self._lock = threading.Lock()

    def __call__(self, url):
        if self.window is None:
            raise RuntimeError("browser not ready")
        with self._lock:
            w = self.window
            w.load_url(url)
            if not w.events.loaded.wait(45):
                raise TimeoutError("page took too long")
            from .parsing import is_challenge
            deadline = time.time() + 25
            time.sleep(4)  # let redirects / scripts settle
            # store pages draw their product tiles after load and more as you scroll: scroll down in steps and wait
            # until the number of links stops growing (max ~10 s)
            last = -1
            for _ in range(8):
                try:
                    n = w.evaluate_js("window.scrollBy(0, Math.max(900, window.innerHeight)); document.links.length") or 0
                except Exception:
                    break
                if n == last:
                    break
                last = n
                time.sleep(1.2)
            try:
                w.evaluate_js("window.scrollTo(0, 0); 0")
            except Exception:
                pass
            while True:
                try:
                    html = w.evaluate_js("document.documentElement.outerHTML") or ""
                except Exception:
                    html = ""  # mid-navigation (e.g. a "checking your browser" page redirecting)
                if html and (not is_challenge(html) or time.time() > deadline):
                    break
                if time.time() > deadline:
                    break
                time.sleep(2)
            current = w.get_current_url() or url
            return 200, current, html


class Api:
    """Everything the UI can ask for. Only methods are exposed to the page."""

    def __init__(self, app):
        self._app = app

    # --- read
    def get_state(self):
        a = self._app
        snap = a.engine.snapshot()
        s = settings.load()
        builtin = a.engine.builtin
        disabled = set(s.get("disabled_sources", []))
        snap["sources"] = [{"name": t["name"], "type": t["type"], "url": t.get("url", ""),
                            "enabled": t["name"] not in disabled} for t in builtin["targets"]]
        snap["presets"] = {k: v["label"] for k, v in builtin.get("watch_presets", {}).items()}
        snap["settings"] = s
        from .notify import CHANNELS
        snap["channels"] = CHANNELS
        snap["update"] = a.update_info
        snap["update_status"] = a.update_status
        snap["installed"] = winsys.is_installed_copy()
        return snap

    # --- actions
    def save_settings(self, changes):
        allowed = {"discord_webhook", "discord_webhook_urgent", "webhooks", "ntfy_topic", "auto_update", "keep_running_when_closed", "twilio", "sms_for", "bot_token",
                   "status_every_minutes", "sports", "start_with_windows", "zip", "topps_all_products",
                   "bot_triggers", "bot_reply", "bot_channel"}
        s = settings.update({k: v for k, v in (changes or {}).items() if k in allowed})
        winsys.set_autostart(bool(s.get("start_with_windows")))
        self._app.engine.reload()
        return {"ok": True}

    def test_alert(self, channel=None):
        from .notify import CHANNELS
        where = f"{CHANNELS[channel]} channel" if channel in CHANNELS else "main channel"
        ok = self._app.engine.notify.send(
            "normal", f"✅ Channel connected · {where}", "",
            {"Note": "Alerts for this channel will post here."}, channel=channel)
        return {"ok": bool(ok)}

    def test_queue_alert(self):
        """Posts exactly what a Pokémon Center queue alert looks like (marked TEST, no @everyone)."""
        return {"ok": bool(self._app.engine.test_queue_alert())}

    def test_all_channels(self):
        """One test message to the main channel and to every store channel that has a webhook."""
        from .notify import CHANNELS
        s = settings.load()
        results = {"main": self.test_alert(None)["ok"]}
        for key in CHANNELS:
            if (s.get("webhooks") or {}).get(key, "").startswith("http"):
                results[key] = self.test_alert(key)["ok"]
        return {"ok": all(results.values()), "results": results}

    def set_paused(self, paused):
        settings.update({"paused": bool(paused)})
        self._app.engine.scan_now()
        self._app.refresh_tray()
        return {"ok": True}

    def scan_now(self):
        self._app.engine.scan_now()
        return {"ok": True}

    def sync_channels(self):
        self._app.engine.request_sync()
        return {"ok": True}

    def set_source_enabled(self, name, enabled):
        s = settings.load()
        dis = set(s.get("disabled_sources", []))
        (dis.discard if enabled else dis.add)(name)
        settings.update({"disabled_sources": sorted(dis)})
        return {"ok": True}

    def add_watch_page(self, name, url, preset, keywords=""):
        url = (url or "").strip()
        if not url.startswith("http"):
            return {"ok": False, "error": "Paste the full page link, starting with https://"}
        words = [w.strip() for w in (keywords or "").split(",") if w.strip()]
        if preset == "custom" and not words:
            return {"ok": False, "error": "Add at least one word or phrase to look for."}
        pages = settings.load().get("watch_pages", [])
        pages.append({"name": (name or "").strip() or url[:60], "url": url, "preset": preset or "custom",
                      "keywords": words, "enabled": True})
        settings.update({"watch_pages": pages})
        self._app.engine.scan_now()
        return {"ok": True}

    def remove_watch_page(self, index):
        pages = settings.load().get("watch_pages", [])
        if 0 <= int(index) < len(pages):
            pages.pop(int(index))
            settings.update({"watch_pages": pages})
        return {"ok": True}

    def open_url(self, url):
        if str(url).startswith("http"):
            webbrowser.open(url)
        return {"ok": True}

    def open_file(self, which):
        target = {"csv": paths.TOPPS_CSV, "calendar": paths.ICS_FILE, "folder": paths.DATA_DIR,
                  "log": paths.LOG_FILE}.get(which)
        if target and target.exists():
            winsys.open_path(target)
            return {"ok": True}
        return {"ok": False, "error": "Nothing saved there yet - give it a minute."}

    def check_update(self):
        self._app.check_update()
        return {"update": self._app.update_info, "status": self._app.update_status}

    def install_update(self):
        info = self._app.update_info
        if not info:
            return {"ok": False, "error": "No update available."}
        threading.Thread(target=self._app.install_update, daemon=True).start()
        return {"ok": True}

    def save_diagnostics(self):
        """Zip what each source last saw + the log to the Desktop, for tuning. Never includes settings/webhooks."""
        import zipfile
        from datetime import datetime
        out_dir = winsys.desktop_dir()
        out = out_dir / f"RipRadar-diagnostics-{datetime.now():%Y%m%d-%H%M}.zip"
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                debug = paths.DATA_DIR / "debug"
                for f in sorted(debug.glob("*.html")) if debug.exists() else []:
                    z.write(f, f"pages/{f.name}")
                for f in (paths.LOG_FILE, paths.TOPPS_CSV):
                    if f.exists():
                        z.write(f, f.name)
                snap = self._app.engine.snapshot()
                z.writestr("status.json", json.dumps({"version": snap["version"], "health": snap["health"],
                                                      "topps": snap["topps"], "topps_formats": snap["topps_formats"]},
                                                     indent=1, default=str))
        except OSError as e:
            return {"ok": False, "error": f"Couldn't save: {e}"}
        winsys.reveal(out)
        return {"ok": True, "path": str(out)}

    def hide_window(self):
        self._app.hide()
        return {"ok": True}

    def quit_app(self):
        threading.Thread(target=self._app.quit, daemon=True).start()
        return {"ok": True}


class App:
    def __init__(self, background=False):
        import webview
        from .engine import Engine
        self.webview = webview
        self.background = background
        self.quitting = False
        self.update_info = None
        self.update_status = ""
        self.tray = None
        self.visible = not background        # auto-updates restart the app only in the tray or right after launch
        self.started_at = time.time()
        self.fetcher = BrowserFetcher()
        self.engine = Engine(browser_fetch=self.fetcher)
        base_alert = self.engine.notify.on_alert

        def on_alert(a):
            base_alert(a)
            if a["level"] == "urgent" and self.tray is not None:
                try:
                    self.tray.notify(a["title"][:200], APP_NAME)   # (message, title)
                except Exception:
                    pass

        self.engine.notify.on_alert = on_alert
        self.api = Api(self)
        html = paths.resource("ui", "index.html").read_text(encoding="utf-8")
        self.window = webview.create_window(
            f"{APP_NAME}", html=html, js_api=self.api, width=1100, height=760, min_size=(420, 560),
            hidden=background, background_color="#0f121c", text_select=True)
        self.window.events.closing += self._on_closing
        # second, hidden window = the scanner's browser (no js_api: it only visits outside sites)
        self.fetcher.window = webview.create_window("Rip Radar scanner", url="about:blank", hidden=True)

    # --- window / tray
    def _on_closing(self):
        if self.quitting:
            return True
        if settings.load().get("keep_running_when_closed"):
            threading.Thread(target=self.hide, daemon=True).start()
            return False   # cancel close; keep scanning from the tray
        # X = quit: stop scanning and remove the tray icon, then let the window close
        self.quitting = True
        log.info("window closed - quitting")
        try:
            self.engine.stop()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        return True

    def show(self):
        self.visible = True
        try:
            self.window.show()
            self.window.restore()
        except Exception as e:
            log.warning("show failed: %s", e)

    def hide(self):
        self.visible = False
        try:
            self.window.hide()
        except Exception as e:
            log.warning("hide failed: %s", e)

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image
        except ImportError as e:
            log.warning("tray unavailable: %s", e)
            return
        img = Image.open(paths.resource("ui", "icon.png"))

        def paused(_item):
            return bool(settings.load().get("paused"))

        menu = pystray.Menu(
            pystray.MenuItem("Open Rip Radar", lambda: self.show(), default=True),
            pystray.MenuItem("Scan now", lambda: self.engine.scan_now()),
            pystray.MenuItem("Paused", lambda: self.api.set_paused(not paused(None)), checked=paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Rip Radar", lambda: threading.Thread(target=self.quit, daemon=True).start()),
        )
        self.tray = pystray.Icon("RipRadar", img, f"{APP_NAME} {__version__}", menu)
        # win32 backend runs its own message loop; keep it off the GUI thread
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

    def refresh_tray(self):
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    # --- updates
    def check_update(self):
        try:
            self.update_info = updater.check()
            self.update_status = "available" if self.update_info else "up to date"
        except Exception as e:
            self.update_status = f"couldn't check: {e}"[:120]

    def install_update(self):
        info = self.update_info
        if not info or self.update_status == "downloading":
            return
        self.update_status = "downloading"
        try:
            (paths.DATA_DIR / "just_updated.json").write_text(json.dumps(
                {"from": __version__, "to": info["version"], "notes": info.get("notes", "")}), encoding="utf-8")
            updater.download_and_restart(info, self.quit)
        except Exception as e:
            log.exception("update failed")
            self.update_status = f"failed: {e}"
            self.engine.notify.send("system", f"⚠️ Update to {info['version']} failed: {e}"[:200])

    def _update_loop(self):
        """Check GitHub every 15 min. Announce a new version in Discord once. With auto-update on, install it
        as soon as the window is closed (app in the tray) - otherwise the blue bar waits for a click."""
        time.sleep(20)
        last_check = 0
        while not self.quitting:
            if time.time() - last_check >= 15 * 60:
                last_check = time.time()
                self.check_update()
                info = self.update_info
                if info and self.engine.state.get("announced_update") != info["version"]:
                    self.engine.state["announced_update"] = info["version"]
                    auto = settings.load().get("auto_update", True)
                    how = ("It installs itself the next time you open Rip Radar, or right away if it's in the tray. "
                           "To get it now, click **Restart to update** in the app." if auto
                           else "Open Rip Radar and click **Restart to update**.")
                    self.engine.notify.send("normal", f"⬆️ Rip Radar {info['version']} is ready", channel="status",
                                            desc=f"{(info.get('notes') or '').splitlines()[0] if info.get('notes') else ''}"
                                                 f"\n\n{how}")
                    if self.tray is not None:
                        try:
                            self.tray.notify(how.replace("**", ""), f"Update {info['version']} ready")
                        except Exception:
                            pass
            info = self.update_info
            just_opened = time.time() - self.started_at < 120
            if info and settings.load().get("auto_update", True) and (not self.visible or just_opened) \
                    and not str(self.update_status).startswith("failed"):
                self.install_update()      # in the tray, or in the first 2 minutes after opening the app
            time.sleep(60)

    def _report_finished_update(self):
        f = paths.DATA_DIR / "just_updated.json"
        if not f.exists():
            return
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            f.unlink()
        except (OSError, ValueError):
            return
        if info.get("to") == __version__:
            notes = (info.get("notes") or "").strip()
            self.engine.notify.send("normal", f"✅ Rip Radar updated to {__version__}", channel="status",
                                    desc=(f"What's new: {notes.splitlines()[0]}" if notes else ""))
        else:
            self.engine.notify.send("system", f"⚠️ Update to {info.get('to')} didn't finish - still on {__version__}. "
                                              "It will try again.")

    # --- lifecycle
    def _on_started(self):
        self._start_tray()
        self.engine.start()
        threading.Thread(target=self._report_finished_update, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True, name="updater").start()

    def run(self):
        self.webview.start(self._on_started, private_mode=False, storage_path=str(paths.WEBVIEW_DIR))

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        log.info("quitting")
        try:
            self.engine.stop()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        for w in list(self.webview.windows):
            try:
                w.destroy()
            except Exception:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="RipRadar")
    ap.add_argument("--background", action="store_true", help="start in the tray (used at login)")
    ap.add_argument("--installed", action="store_true")
    ap.add_argument("--updated", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-out", default="")
    ap.add_argument("--selftest-relaunch", default="", help="build check: start a fresh copy, then exit at once")
    args, _ = ap.parse_known_args(argv)

    if args.selftest_relaunch:
        # the same way updates and installs relaunch the app: the child must survive this process exiting
        winsys.launch_new_copy([sys.executable, "--selftest", "--selftest-out", args.selftest_relaunch])
        sys.exit(0)

    if args.selftest:
        from .selftest import run
        sys.exit(run(args.selftest_out))

    setup_logging()
    log.info("Rip Radar %s starting (%s)", __version__, sys.executable)
    if winsys.install_and_relaunch():
        return
    holder = {}
    if not winsys.claim_single_instance(lambda: holder["app"].show() if "app" in holder else None,
                                        wait_seconds=15 if (args.installed or args.updated) else 0):
        log.info("already running - asked it to show itself")
        return
    s = settings.load()
    winsys.set_autostart(bool(s.get("start_with_windows")))
    first_run = not s.get("discord_webhook")
    # at sign-in, only start hidden if the user chose to keep it running in the tray
    app = App(background=args.background and not first_run and bool(s.get("keep_running_when_closed")))
    holder["app"] = app
    app.run()
