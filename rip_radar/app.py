"""Rip Radar desktop app: window + tray icon + background scanner + self-update."""
import argparse
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
            time.sleep(4)  # let redirects / scripts settle
            html = w.evaluate_js("document.documentElement.outerHTML") or ""
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
        snap["update"] = a.update_info
        snap["update_status"] = a.update_status
        snap["installed"] = winsys.is_installed_copy()
        return snap

    # --- actions
    def save_settings(self, changes):
        allowed = {"discord_webhook", "discord_webhook_urgent", "ntfy_topic", "twilio", "sms_for", "bot_token",
                   "status_every_minutes", "sports", "start_with_windows"}
        s = settings.update({k: v for k, v in (changes or {}).items() if k in allowed})
        winsys.set_autostart(bool(s.get("start_with_windows")))
        self._app.engine.reload()
        return {"ok": True}

    def test_alert(self):
        ok = self._app.engine.notify.send(
            "urgent", "Test alert from Rip Radar", "https://www.topps.com/release-calendar",
            {"Source": "Test", "Note": "If your phone buzzed, you're set."})
        return {"ok": bool(ok)}

    def set_paused(self, paused):
        settings.update({"paused": bool(paused)})
        self._app.engine.scan_now()
        self._app.refresh_tray()
        return {"ok": True}

    def scan_now(self):
        self._app.engine.scan_now()
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
        self._app.update_status = "downloading"

        def run():
            try:
                updater.download_and_restart(info, self._app.quit)
            except Exception as e:
                log.exception("update failed")
                self._app.update_status = f"failed: {e}"

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

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
        threading.Thread(target=self.hide, daemon=True).start()
        return False   # cancel close; we live in the tray

    def show(self):
        try:
            self.window.show()
            self.window.restore()
        except Exception as e:
            log.warning("show failed: %s", e)

    def hide(self):
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

    def _update_loop(self):
        time.sleep(20)
        while not self.quitting:
            had = self.update_info
            self.check_update()
            if self.update_info and not had and self.tray is not None:
                try:
                    self.tray.notify("Open Rip Radar and click Restart to update.",
                                     f"Update {self.update_info['version']} available")
                except Exception:
                    pass
            time.sleep(6 * 3600)

    # --- lifecycle
    def _on_started(self):
        self._start_tray()
        self.engine.start()
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
    args, _ = ap.parse_known_args(argv)

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
    app = App(background=args.background and not first_run)
    holder["app"] = app
    app.run()
