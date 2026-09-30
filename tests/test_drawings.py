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


def test_drawings_ping_on_first_look_and_when_they_open(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((title, fields, kw))
    later = datetime.now(CT) + timedelta(hours=3)
    when = f"{later:%b} {later.day}, {later.hour % 12 or 12}:{later:%M}{'pm' if later.hour >= 12 else 'am'} CT"
    st = {}
    engine.fetcher = FakeFetch(page(f"Drawing starts {when}"), DRAW_URL)
    health, _ = engine.check_walmart_drawings(dict(T), st, True)
    assert health == "ok (2 card drawings · 0 open · 2 other items skipped)"      # NeeDoh + Magic skipped
    titles = [g[0] for g in got]
    assert len(titles) == 2 and all(x.startswith("🎟️ WALMART DRAWING · opens") for x in titles)   # even on first run
    f = got[0][1]
    assert f["Price"] == "$79.94" and f["Entries open (CT)"] and "Calendar" in f
    assert f["Typical retail"].startswith("~$79.98")
    enter = dict(got[0][2]["links"])["🎟️ Enter the drawing"]
    assert enter.startswith("https://www.walmart.com/ip/") and enter.endswith("/20640569221")
    got.clear()
    engine.check_walmart_drawings(dict(T), st, False)
    assert got == []                                                                # no repeats
    # entries open (start time now in the past, page shows the same item)
    past = datetime.now(CT) - timedelta(minutes=1)
    for rec in st["drawings"].values():
        rec["phase"] = "upcoming"
    key_before = set(st["drawings"])
    engine.fetcher = FakeFetch(page(f"Drawing starts {when}"), DRAW_URL)
    import rip_radar.engine as eng_mod
    real = eng_mod.datetime

    class Later(real):
        @classmethod
        def now(cls, tz=None):
            return (later + timedelta(minutes=1)).astimezone(tz) if tz else later + timedelta(minutes=1)
    eng_mod.datetime = Later
    try:
        engine.check_walmart_drawings(dict(T), st, False)
    finally:
        eng_mod.datetime = real
    assert set(st["drawings"]) == key_before
    assert [g[0] for g in got] == [
        "🎟️ OPEN NOW · enter the Walmart drawing: Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle",
        "🎟️ OPEN NOW · enter the Walmart drawing: Pokémon TCG: 30th Celebration Mini Tin 10-Count Display Box"]


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
    assert sent[1][1]["Typical retail"] == "~$69.99"


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
    n.send("urgent", "drawing", channel=("drawings", "walmart"))
    n.send("system", "source check")
    assert posted == ["https://walmart", "https://main"]           # no drawings/status hooks yet -> fallbacks
    posted.clear()
    settings.update({"webhooks": {"drawings": "https://draw", "status": "https://status"}})
    n.send("urgent", "drawing", channel=("drawings", "walmart"))
    n.send("system", "source check")
    n.send("normal", "update ready", channel="status")
    assert posted == ["https://draw", "https://status", "https://status"]
