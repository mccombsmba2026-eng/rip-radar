# PyInstaller recipe for the single-file Windows app. Built by .github/workflows/build.yml.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("rip_radar/ui", "rip_radar/ui"), ("rip_radar/targets.yaml", "rip_radar")]
binaries = []
hiddenimports = ["pystray._win32", "clr"] + collect_submodules("pystray")
wv_datas, wv_bins, wv_hidden = collect_all("webview")
datas += wv_datas
binaries += wv_bins
hiddenimports += [h for h in wv_hidden
                  if not any(x in h for x in (".qt", ".gtk", ".cocoa", ".android", ".cef"))]

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3",
              "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cocoa",
              "webview.platforms.android", "webview.platforms.cef"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="RipRadar",
    console=False,
    icon="assets/icon.ico",
    upx=False,
)
