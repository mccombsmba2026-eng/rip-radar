import json
import types

from rip_radar import settings
from rip_radar.app import Api
from tests.test_engine import engine  # noqa: F401  (fixture)


def make_api(engine):
    app = types.SimpleNamespace(engine=engine, update_info=None, update_status="up to date",
                                refresh_tray=lambda: None, hide=lambda: None, quit=lambda: None)
    return Api(app)


def test_state_is_json_for_the_ui(engine):
    api = make_api(engine)
    state = api.get_state()
    json.dumps(state)                                   # must cross the JS bridge
    assert state["version"] and "presets" in state and state["sources"]
    assert state["settings"]["sports"] == ["Baseball", "Basketball", "Football"]


def test_save_settings_only_accepts_known_keys(engine):
    api = make_api(engine)
    api.save_settings({"discord_webhook": "https://discord.com/api/webhooks/1/x", "paused": True, "evil": 1})
    s = settings.load()
    assert s["discord_webhook"].endswith("/1/x") and s["paused"] is False and "evil" not in s


def test_watch_page_add_validate_remove(engine):
    api = make_api(engine)
    assert not api.add_watch_page("x", "walmart.com/ip/1", "walmart")["ok"]           # no https
    assert not api.add_watch_page("x", "https://a.com", "custom", "")["ok"]           # custom needs words
    assert api.add_watch_page("ETB", "https://www.walmart.com/ip/1", "walmart")["ok"]
    assert api.add_watch_page("Mine", "https://a.com/p", "custom", "restock, add to cart")["ok"]
    pages = settings.load()["watch_pages"]
    assert [p["name"] for p in pages] == ["ETB", "Mine"] and pages[1]["keywords"] == ["restock", "add to cart"]
    names = [t["name"] for t in engine.targets()]
    assert "ETB" in names and "Mine" in names
    api.remove_watch_page(0)
    assert [p["name"] for p in settings.load()["watch_pages"]] == ["Mine"]


def test_toggle_source_and_pause(engine):
    api = make_api(engine)
    api.set_source_enabled("Topps.com homepage", False)
    assert "Topps.com homepage" not in [t["name"] for t in engine.targets()]
    api.set_source_enabled("Topps.com homepage", True)
    assert "Topps.com homepage" in [t["name"] for t in engine.targets()]
    api.set_paused(True)
    assert settings.load()["paused"] and api.get_state()["paused"]


def test_diagnostics_zip_never_includes_webhooks(engine, tmp_path, monkeypatch):
    import zipfile
    from rip_radar import paths, winsys
    monkeypatch.setattr(winsys, "desktop_dir", lambda: tmp_path)
    monkeypatch.setattr(winsys, "reveal", lambda p: None)
    settings.update({"discord_webhook": "https://discord.com/api/webhooks/SECRET"})
    engine._save_debug("Target · Pokémon cards", "<html>tiles</html>")
    r = make_api(engine).save_diagnostics()
    assert r["ok"]
    with zipfile.ZipFile(r["path"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert "pages/target-pok-mon-cards.html" in names and "status.json" in names
    assert b"SECRET" not in blob and not any("settings" in n for n in names)


def test_keep_running_setting_defaults_off_and_saves(engine):
    assert settings.load()["keep_running_when_closed"] is False      # X quits by default
    make_api(engine).save_settings({"keep_running_when_closed": True})
    assert settings.load()["keep_running_when_closed"] is True


def test_relaunch_env_drops_pyinstaller_temp_settings(monkeypatch):
    from rip_radar import winsys
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Users\x\AppData\Local\Temp\_MEI00000bd82")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI1")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    env = winsys.fresh_env()
    assert not any(k.startswith(("_PYI_", "_MEI")) for k in env)
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and "PATH" in env
