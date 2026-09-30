from datetime import datetime, timedelta

from rip_radar import settings
from rip_radar.parsing import CT
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)

PAD = "x" * 21000
DRAW_URL = "https://www.walmart.com/shop/collectibles/draw"


def item(pid, name, price, status):
    return (f'<div class="tile"><a href="/ip/{name.replace(" ", "-")}/{pid}"><img src="https://i5.walmartimages.com/{pid}.jpg" '
            f'alt="{name}"></a><a href="/ip/x/{pid}">{name}</a><div>{price}</div><div>{status}</div>'
            f'<button>Enter drawing</button></div>')


def page(status):
    return (item("20640569221", "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle", "$79.94", status)
            + item("20640569222", "Pokémon TCG: 30th Celebration Mini Tin 10-Count Display Box", "$129.70", status)
            + item("30000000001", "NeeDoh Dream Drop", "$5.97", status)
            + item("30000000002", "Magic: The Gathering Reality Fracture Collector Booster Box", "$383.64", status) + PAD)


T = {"name": "Walmart · drawings", "type": "walmart_drawings", "url": DRAW_URL}


def at(moment):
    import rip_radar.engine as eng_mod

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    eng_mod.datetime = Clock


def test_drawings_announce_then_remind_on_the_clock(engine):
    import rip_radar.engine as eng_mod
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((title, fields, kw))
    start = (datetime.now(CT) + timedelta(hours=3)).replace(second=0, microsecond=0)
    when = f"{start:%b} {start.day}, {start.hour % 12 or 12}:{start:%M}{'pm' if start.hour >= 12 else 'am'} CT"
    st = {}
    engine.state.setdefault("targets", {})[T["name"]] = st
    engine.fetcher = FakeFetch(page(f"Drawing starts {when}"), DRAW_URL)
    health, _ = engine.check_walmart_drawings(dict(T), st, True)
    assert health == "ok (2 card drawings · 2 upcoming · 0 open · 2 other items skipped)"   # NeeDoh + Magic skipped
    titles = [g[0] for g in got]
    assert len(titles) == 2 and all(x.startswith("🎟️ WALMART DRAWING · opens") for x in titles)   # even on first run
    f = got[0][1]
    assert f["Price"] == "$79.94" and f["Opens (CT)"] and "Calendar" in f and f["Retail (MSRP)"].startswith("~$79.98")
    assert got[0][2].get("copy_to") == ["drawings"] and got[0][2].get("channel") == "walmart"
    enter = dict(got[0][2]["links"])["🎟️ ENTER THE DRAWING"]
    assert enter.startswith("https://www.walmart.com/ip/") and enter.endswith("/20640569221")
    got.clear()
    engine.check_walmart_drawings(dict(T), st, False)
    assert got == []                                                                # no repeats
    real = eng_mod.datetime
    try:
        for moment, expect in [(start - timedelta(minutes=14, seconds=30), "⏰ Walmart drawing opens in 14 min"),
                               (start - timedelta(minutes=10), None),
                               (start - timedelta(seconds=50), "⏰ Walmart drawing opens in 1 min"),
                               (start + timedelta(seconds=5), "🟢 OPEN NOW · enter the Walmart drawing"),
                               (start + timedelta(minutes=3), None)]:
            got.clear()
            at(moment)
            engine.drawings_tick()
            if expect:
                assert len(got) == 2 and all(g[0].startswith(expect) for g in got), (moment, got)
            else:
                assert got == []
    finally:
        eng_mod.datetime = real


def test_drawing_without_a_time_is_not_called_open(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append(title)
    engine.fetcher = FakeFetch(item("20640569221", "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle", "$79.94",
                                    "").replace("<button>Enter drawing</button>", "<button>Get notified</button>") + PAD,
                               DRAW_URL)
    health, _ = engine.check_walmart_drawings(dict(T), {}, True)
    assert "0 open" in health and got == ["🎟️ WALMART DRAWING listed (open time not shown yet): "
                                          "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle"]


def test_reseller_prices_are_ignored(engine):
    t = {"name": "Walmart · Pokémon cards", "type": "retail_search", "store": "walmart", "products": "pokemon",
         "url": "https://www.walmart.com/search?q=pokemon+trading+cards"}
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append((title, fields))

    def tile(pid, name, price):
        return (f'<div><a href="/ip/{pid}/{pid}"><img alt="{name}" src="https://i/{pid}.jpg"></a>'
                f'<span>{price}</span><button>Add to cart</button></div>')
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    engine.fetcher = FakeFetch(tile("11111111", "Pokémon TCG: 30th Celebration Elite Trainer Box", "$221.98")
                               + tile("22222222", "Pokémon TCG: Delta Reign Booster Bundle", "$29.99")
                               + tile("33333333", "Pokémon TCG: Delta Reign Elite Trainer Box", "$89.99") + PAD, t["url"])
    health, _ = engine.check_retail_search(t, st, False)
    assert "1 over retail ignored" in health
    titles = [x[0] for x in sent]
    assert titles == ["🟢 NEW & IN STOCK at Walmart: Pokémon TCG: Delta Reign Booster Bundle",
                      "🟢 NEW & IN STOCK at Walmart: Pokémon TCG: Delta Reign Elite Trainer Box · ⚠️ 28% above retail"]
    assert sent[1][1]["Retail (MSRP)"] == "~$69.99"


def test_blocked_site_gets_a_rest(engine):
    t = {"name": "Pokémon Center queue", "type": "keywords", "url": "https://www.pokemoncenter.com/", "keywords": ["x"]}
    engine.fetcher = FakeFetch("<html>Pardon Our Interruption</html>")
    engine._run_target(t, {})
    resume, n = engine.host_backoff["pokemoncenter.com"]
    assert n == 1 and 290 < resume - __import__("time").time() <= 300
    engine._run_target(t, {})
    assert engine.host_backoff["pokemoncenter.com"][1] == 2
    engine.fetcher = FakeFetch("<html>shop</html>" + PAD)
    engine._run_target(t, {})
    assert "pokemoncenter.com" not in engine.host_backoff


def test_drawings_and_status_channels(engine, monkeypatch):
    from rip_radar import notify as notify_mod
    posted = []
    monkeypatch.setattr(notify_mod.Notifier, "_post_discord", staticmethod(lambda hook, payload: posted.append(hook) or True))
    settings.update({"discord_webhook": "https://main", "webhooks": {"walmart": "https://walmart"}})
    n = notify_mod.Notifier(settings.load)
    n.send("urgent", "drawing", channel="walmart", copy_to=["drawings"])
    n.send("system", "source check")
    assert posted == ["https://walmart", "https://main"]           # no drawings/status hooks yet
    posted.clear()
    settings.update({"webhooks": {"drawings": "https://draw", "status": "https://status"}})
    n.send("urgent", "drawing", channel="walmart", copy_to=["drawings"])
    assert sorted(posted) == ["https://draw", "https://walmart"]   # a raffle posts in BOTH channels
    posted.clear()
    n.send("system", "source check")
    n.send("normal", "update ready", channel="status")
    assert posted == ["https://status", "https://status"]
