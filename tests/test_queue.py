from datetime import datetime, timedelta

import rip_radar.engine as eng_mod
from rip_radar.parsing import CT
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)

PAD = "x" * 25000
Q = {"name": "Pokémon Center queue", "type": "keywords", "url": "https://www.pokemoncenter.com/",
     "url_contains": ["queue-it"], "keywords": ["you are now in line"], "track_duration": True,
     "channel": ["pokemon_queue", "pokemon"], "alert_title": "🚨 POKÉMON CENTER QUEUE IS LIVE"}


class Clock:
    def __init__(self, start):
        self.t = start

    def install(self, monkeypatch):
        real = eng_mod.datetime
        clock = self

        class Fake(real):
            @classmethod
            def now(cls, tz=None):
                return clock.t.astimezone(tz) if tz else clock.t
        monkeypatch.setattr(eng_mod, "datetime", Fake)


def test_queue_up_duration_and_close(engine, monkeypatch):
    got, live_msgs = [], []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((level, title, fields, desc))
    engine._queue_live_message = lambda t, st, text: live_msgs.append(text)
    clock = Clock(datetime(2026, 10, 2, 14, 14, tzinfo=CT))
    clock.install(monkeypatch)
    st = {}
    engine.fetcher = FakeFetch("<html>shop" + PAD, "https://www.pokemoncenter.com/")
    assert engine.check_keywords(Q, st, True)[0] == "ok · not live"
    engine.fetcher = FakeFetch("<html>You are now in line</html>", "https://pokemoncenter.queue-it.net/?c=pkmn")
    engine.check_keywords(Q, st, False)
    assert got[0][0] == "urgent" and got[0][1] == "🚨 POKÉMON CENTER QUEUE IS LIVE"
    assert got[0][2]["Went up (CT)"] == "2:14 PM"
    clock.t += timedelta(minutes=47)
    health, _ = engine.check_keywords(Q, st, False)
    assert health == "ok · LIVE for 47 min" and "up for **47 min**" in live_msgs[-1]
    assert len(got) == 1                                              # no repeat pings while it's up
    clock.t += timedelta(minutes=36)                                  # 1h 23m total
    engine.fetcher = FakeFetch("<html>shop" + PAD, "https://www.pokemoncenter.com/")
    engine.check_keywords(Q, st, False)
    level, title, fields, desc = got[-1]
    assert title == "⚪ Pokémon Center queue closed · was up 1h 23m"
    assert fields == {"Went up (CT)": "2:14 PM", "Closed (CT)": "3:37 PM", "Up for": "1h 23m"}
    assert "2:14 PM–3:37 PM (1h 23m)" in desc and "was up **1h 23m**" in live_msgs[-1]
    assert engine.snapshot()["queue_history"][0]["minutes"] == 83


def test_queue_stays_open_while_site_blocks_us(engine, monkeypatch):
    engine.notify.send = lambda *a, **k: None
    engine._queue_live_message = lambda *a: None
    st = {}
    engine.fetcher = FakeFetch("<html>You are now in line</html>", "https://pokemoncenter.queue-it.net/")
    engine.check_keywords(Q, st, False)
    engine.fetcher = FakeFetch("<html>Pardon Our Interruption</html>", "https://www.pokemoncenter.com/")
    assert engine.check_keywords(Q, st, False)[0] == "blocked"
    assert st["active"] is True and st.get("live_since")             # not wrongly marked closed


def test_drop_mode_commands_and_timer(engine):
    from rip_radar.notify import drop_mode_command
    assert drop_mode_command("drop mode on") == 3 and drop_mode_command("Drop Mode 5 hours") == 5
    assert drop_mode_command("drop mode off") == 0 and drop_mode_command("dropping soon") is None
    left = engine.set_drop_mode(3)
    assert 3 * 3600 - 5 < left <= 3 * 3600 and engine.snapshot()["drop_mode"] > 0
    engine.set_drop_mode(0)
    assert engine.drop_mode() == 0


def test_queue_has_its_own_browser_and_drop_mode_pauses_pc_scans(engine):
    from rip_radar.engine import Engine
    calls = []
    e = Engine(browser_fetch=lambda url: (200, url, "store"), queue_fetch=lambda url: calls.append(url) or
               (200, url, "<html><h1>You are in line to enter Pokémon Center</h1><p>Please keep this window open.</p></html>"))
    e.notify.send = lambda *a, **k: True
    t = next(x for x in e.targets() if x.get("track_duration"))
    health, _ = e.check_keywords(t, {}, False)
    assert calls == ["https://www.pokemoncenter.com/"] and "LIVE" in health


def test_bot_check_heads_up_on_and_off(engine):
    from rip_radar.parsing import wall_kind
    assert wall_kind(200, "<html>Pardon Our Interruption...</html>").startswith("Imperva")
    assert wall_kind(200, '<p>Please complete the captcha</p><iframe src="https://newassets.hcaptcha.com/x"></iframe>') == "hCaptcha"
    assert wall_kind(200, "<html>shop cards</html>") == ""
    # a normal Pokémon Center page that merely loads its bot-protection scripts is NOT a captcha
    normal = ('<script src="/_Incapsula_Resource?x"></script><script src="https://www.google.com/recaptcha/api.js">'
              '</script><main>' + "Shop the newest Pokémon TCG Elite Trainer Box and more " * 80 + "</main>")
    assert wall_kind(200, normal) == ""
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append(title)
    t = {"url": "https://www.pokemoncenter.com/", "channel": ["pokemon_queue", "pokemon"]}
    st, now = {}, datetime.now(CT)
    engine._watch_bot_check(t, st, "hCaptcha", now)
    engine._watch_bot_check(t, st, "hCaptcha", now)
    assert sent == []                                    # two checks aren't enough
    engine._watch_bot_check(t, st, "hCaptcha", now)
    assert sent == ["🛡️ Pokémon Center turned on its bot check · hCaptcha"]
    st["wall_posted"] = 0
    for _ in range(3):
        engine._watch_bot_check(t, st, "", now)
    assert sent[-1].startswith("🛡️ Pokémon Center bot check is off again")


def test_drop_mode_only_from_urgent_channel(monkeypatch):
    from rip_radar import notify
    bot = notify.ChatBot(lambda: "", lambda: {"discord_webhook_urgent": "https://discord.com/api/webhooks/9/u"})

    class R:
        def json(self):
            return {"channel_id": "555"}
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout=None: R())
    assert bot.drop_mode_allowed("urgent-only", 555) and not bot.drop_mode_allowed("alerts", 777)
    bot2 = notify.ChatBot(lambda: "", lambda: {})
    assert bot2.drop_mode_allowed("urgent-only", 1) and not bot2.drop_mode_allowed("app-status", 2)
