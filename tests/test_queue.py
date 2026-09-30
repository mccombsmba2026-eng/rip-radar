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
