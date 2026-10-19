import json

from rip_radar import paths, settings


def test_settings_backup_round_trip(tmp_path, monkeypatch):
    for name, fn in (("SETTINGS_FILE", "settings.json"), ("STATE_FILE", "state.json")):
        monkeypatch.setattr(paths, name, tmp_path / fn)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    from rip_radar import app as app_mod
    monkeypatch.setattr(app_mod.winsys, "desktop_dir", lambda: tmp_path)
    monkeypatch.setattr(app_mod.winsys, "reveal", lambda p: None)
    monkeypatch.setattr(app_mod.winsys, "set_autostart", lambda on: None)
    settings.update({"webhooks": {"watch": "https://discord.com/api/webhooks/1/abc"}, "watch_terms": ["prismatic etb"]})

    class FakeApp:
        engine = type("E", (), {"reload": lambda self: None})()
        window = None
    api = app_mod.Api(FakeApp())
    r = api.export_settings()
    assert r["ok"]
    saved = json.loads(open(r["path"], encoding="utf-8").read())
    assert saved["webhooks"]["watch"].endswith("/abc") and saved["watch_terms"] == ["prismatic etb"]
    # a fresh install: empty settings, then restore from the file
    paths.SETTINGS_FILE.unlink()
    assert settings.load()["webhooks"].get("watch", "") == ""
    FakeApp.window = type("W", (), {"create_file_dialog": lambda self, *a, **k: [r["path"]]})()
    assert api.import_settings()["ok"]
    assert settings.load()["webhooks"]["watch"].endswith("/abc") and settings.load()["watch_terms"] == ["prismatic etb"]
    assert "_ripradar_backup" not in settings.load()


def test_ebay_block_pauses_and_survives_restart():
    from rip_radar.resale import ResaleChecker
    state = {}
    rc = ResaleChecker(state, lambda: {"resale_check": False})
    rc._get = lambda url: (403, "<html>Access denied</html>", "browser")
    rc.check("Pokémon TCG: Prismatic Evolutions Elite Trainer Box")
    assert rc.paused() and "paused 3 h" in rc.stats["last_page"]
    rc2 = ResaleChecker(state, lambda: {})                      # app restarted: still paused
    assert rc2.paused() and "⏸️" in rc2.health()
    rc2.stats["paused_until"] = 0
    rc2.blocked("eBay answered HTTP 403")                         # second block in a day: longer
    assert "paused 6 h" in rc2.stats["last_page"]
