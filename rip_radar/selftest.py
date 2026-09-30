"""`RipRadar.exe --selftest`: run on the Windows build machine before every release.
Checks that the packaged app has everything it needs, then runs every non-browser source once
against the live sites and writes a report (it ends up in the release notes)."""
import logging
import sys
import tempfile
import traceback
from pathlib import Path


def run(out_path=""):
    lines, ok = [], True

    def say(s):
        lines.append(s)

    try:
        import webview  # noqa: F401  (GUI stack is bundled)
        import pystray  # noqa: F401
        import discord  # noqa: F401
        from PIL import Image  # noqa: F401
        from . import __version__, paths
        say(f"Rip Radar {__version__} selftest")
        assert paths.resource("ui", "index.html").exists(), "ui/index.html missing"
        assert paths.resource("ui", "icon.png").exists(), "ui/icon.png missing"
        say("✅ bundled UI, icon, GUI and Discord libraries load")
    except Exception as e:
        ok = False
        say(f"❌ packaging problem: {e!r}")
        say(traceback.format_exc()[-1500:])

    if ok:
        try:
            from . import paths
            tmp = Path(tempfile.mkdtemp())
            for name, fn in (("DATA_DIR", ""), ("SETTINGS_FILE", "settings.json"), ("STATE_FILE", "state.json"),
                             ("ICS_FILE", "drops.ics"), ("TOPPS_CSV", "topps.csv"), ("WEBVIEW_DIR", "wv")):
                setattr(paths, name, tmp / fn if fn else tmp)
            logging.basicConfig(level=logging.WARNING)
            from .engine import Engine
            eng = Engine()
            eng.notify.send = lambda *a, **k: True          # never post from the build machine
            say("")
            say("Live source check from the build machine (browser sources run only in the app):")
            for t in eng.targets():
                if t.get("browser"):
                    continue
                report = {}
                eng._run_target(t, report)
                v = report[t["name"]]
                say(f"{'✅' if v.startswith('ok') else '⚠️'} {t['name']}: {v}")
            if eng.topps:
                say("")
                say(f"Topps calendar ({len(eng.topps)} baseball / basketball / football):")
                from datetime import datetime
                from .parsing import fmt_when
                for p in eng.topps:
                    say(f"- {p['sport']}: {p['name']} · {fmt_when(datetime.fromisoformat(p['when']), p['has_time'])} CT"
                        f" · {p['status']}")
        except Exception as e:
            say(f"⚠️ live check error (not a packaging problem): {e!r}")

    text = "\n".join(lines)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    try:
        print(text)
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else ""))
