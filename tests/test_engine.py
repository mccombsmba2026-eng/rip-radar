from datetime import datetime, timedelta, timezone

import pytest

from rip_radar import paths, settings
from tests.test_parsing import AVAIL, SOON, page


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for name in ("DATA_DIR", "WEBVIEW_DIR"):
        monkeypatch.setattr(paths, name, tmp_path / name)
    for name, fn in (("SETTINGS_FILE", "settings.json"), ("STATE_FILE", "state.json"), ("ICS_FILE", "drops.ics"),
                     ("TOPPS_CSV", "topps.csv")):
        monkeypatch.setattr(paths, name, tmp_path / fn)
    from rip_radar.engine import Engine
    e = Engine()
    e.sent = []
    e.notify.send = lambda level, title, url="", fields=None, desc="", **kw: e.sent.append((level, title))
    return e


class FakeFetch:
    def __init__(self, html, url="https://www.topps.com/release-calendar"):
        self.html, self.url = html, url

    def get(self, url, browser=False):
        return 200, self.url, self.html


TOPPS = {"name": "Topps release calendar", "type": "topps_calendar", "url": "https://www.topps.com/release-calendar"}


def test_topps_first_run_then_changes(engine):
    st = {}
    engine.fetcher = FakeFetch(page())
    health, _ = engine.check_topps_calendar(dict(TOPPS), st, True)
    assert health.startswith("ok (5 products)")                      # tennis filtered out
    # first look: quiet (a real drop-time reminder may still fire, depending on today's clock)
    assert [lvl for lvl, t in engine.sent if "drop in" not in t and "OPEN NOW" not in t] == []
    engine.sent.clear()
    soon = [list(c) for c in SOON]
    soon[3][3] = "Pre-order"                                          # unchanged
    soon[4][1] = "Wednesday, Nov 18"                                  # Museum moves
    soon[1][3] = "Buy now"                                            # Bowman Football goes live
    soon.append(("topps-midnight-football", "Monday, Oct 12", "2026 Topps Midnight Football", "Pre-order"))
    engine.fetcher = FakeFetch(page([tuple(c) for c in soon], AVAIL))
    engine.check_topps_calendar(dict(TOPPS), st, False)
    titles = [t for _, t in engine.sent]
    assert any("TOPPS LIVE: 2026 Bowman Football" in t for t in titles)
    (summary,) = [t for t in titles if "Topps calendar updated" in t]          # no board webhook: one message
    alert_titles = [a["title"] for a in engine.state["alerts"]]
    assert any("2026 Topps Museum" in a and "moved" in a for a in alert_titles)
    assert any("added 2026 Topps Midnight Football" in a for a in alert_titles)
    assert paths.TOPPS_CSV.exists() and paths.ICS_FILE.exists()


def test_topps_reminder_fires_once(engine):
    soon = datetime.now(timezone.utc) + timedelta(minutes=9)
    txt = f"{soon:%A}, {soon:%b} {soon.day} at {soon.hour % 12 or 12}:{soon:%M} {'PM' if soon.hour >= 12 else 'AM'} UTC"
    html = (f'<h2>Dropping soon</h2><a href="/pages/bowman-football"><img alt="2026 Bowman Football"></a>'
            f'<a href="/pages/bowman-football">{txt} 2026 Bowman Football</a><button>Notify me</button>' + "x" * 21000)
    engine.fetcher = FakeFetch(html)
    st = {}
    engine.check_topps_calendar(dict(TOPPS), st, True)
    engine.check_topps_calendar(dict(TOPPS), st, False)
    assert sum("drop in" in t for _, t in engine.sent) == 1


def test_queue_detection(engine):
    q = {"name": "PC queue", "url": "https://www.pokemoncenter.com/", "url_contains": ["queue"],
         "keywords": ["you are now in line"], "alert_on_end": True}
    st = {}
    engine.fetcher = FakeFetch("<html>shop" + "x" * 25000, "https://www.pokemoncenter.com/")
    assert engine.check_keywords(q, st, True)[0] == "ok · not live"
    engine.fetcher = FakeFetch("<html>You are now in line</html>", "https://pokemoncenter.queue-it.net/?c=x")
    assert engine.check_keywords(q, st, False)[0] == "ok · LIVE now"
    engine.fetcher = FakeFetch("<html>shop" + "x" * 25000, "https://www.pokemoncenter.com/")
    engine.check_keywords(q, st, False)
    assert [lvl for lvl, _ in engine.sent] == ["urgent", "normal"]
    engine.fetcher = FakeFetch("<html>Robot or human?</html>")
    assert engine.check_keywords(q, {}, False)[0] == "blocked"


def test_feed_filters_and_urgency(engine):
    def rss(items):
        body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
                       f"<pubDate>{datetime.now(timezone.utc):%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>"
                       for t, l in items)
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'
    f = {"name": "News", "url": "u", "must_have": ["drawing"], "urgent_if": ["now open"], "category": "Pokémon"}
    st = {}
    engine.fetcher = FakeFetch(rss([("old drawing story", "https://a/1")]))
    engine.check_feed(f, st, True)
    engine.fetcher = FakeFetch(rss([("old drawing story", "https://a/1"),
                                    ("Walmart Pokemon drawing now open", "https://a/2"),
                                    ("Pokemon plush sale", "https://a/3")]))
    engine.check_feed(f, st, False)
    assert engine.sent == [("urgent", "🎟️ Walmart Pokemon drawing now open")]


def test_user_watch_pages_become_targets(engine):
    settings.update({"watch_pages": [{"name": "Walmart 30th ETB", "url": "https://www.walmart.com/ip/1",
                                      "preset": "walmart", "enabled": True}],
                     "disabled_sources": ["Topps.com homepage"]})
    names = {t["name"]: t for t in engine.targets()}
    assert "Topps.com homepage" not in names
    t = names["Walmart 30th ETB"]
    assert t["type"] == "keywords" and t["browser"] and "enter drawing" in t["keywords"]
    assert t["alert_title"].startswith("🚨 WALMART DRAWING OPEN")


def test_status_text(engine):
    txt = engine.status_text()
    assert "Yes, running" in txt and "Sources OK" in txt


def test_blocked_source_switches_to_browser(engine):
    calls = []

    class Blocked:
        browser_fetch = object()

        def get(self, url, browser=False):
            calls.append(browser)
            return (200, url, page()) if browser else (403, url, "")

    engine.fetcher = Blocked()
    st = {}
    health, _ = engine.check_topps_calendar(dict(TOPPS), st, True)
    assert health.startswith("ok") and calls == [False, True] and st["auto_browser"]
    engine.check_topps_calendar(dict(TOPPS), st, False)
    assert calls == [False, True, True]                  # stays on the browser afterwards


def test_challenge_detection():
    from rip_radar.parsing import is_challenge
    assert is_challenge("<html><title>Just a moment...</title></html>")
    assert not is_challenge(page())


# ---------------------------------------------------------------- 1.0.3
def test_calendar_keeps_last_known_time(engine):
    st = {}
    engine.fetcher = FakeFetch(page())
    engine.check_topps_calendar(dict(TOPPS), st, True)
    countdown = [list(c) for c in SOON]
    countdown[1][1] = "Dropping in 00:12:00"                     # Bowman Football near drop
    engine.fetcher = FakeFetch(page([tuple(c) for c in countdown], AVAIL))
    engine.check_topps_calendar(dict(TOPPS), st, False)
    bf = next(p for p in engine.topps if p["slug"] == "bowman-football")
    assert bf["has_time"] and bf["when"].startswith("2026-09-30T11:00")
    assert not any("date moved" in t or "New on Topps" in t for _, t in engine.sent)


def test_open_now_alert_links_to_page(engine):
    now = datetime.now(timezone.utc) - timedelta(minutes=2)
    txt = f"{now:%A}, {now:%b} {now.day} at {now.hour % 12 or 12}:{now:%M} {'PM' if now.hour >= 12 else 'AM'} UTC"
    html = (f'<h2>Dropping soon</h2><a href="/pages/bowman-football"><img alt="2026 Bowman Football"></a>'
            f'<a href="/pages/bowman-football">{txt} 2026 Bowman Football</a><button>Notify me</button>' + "x" * 21000)
    engine.fetcher = FakeFetch(html)
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((title, url, fields))
    engine.check_topps_calendar(dict(TOPPS), {}, True)
    opened = [g for g in got if "OPEN NOW" in g[0]]
    assert len(opened) == 1 and opened[0][1] == "https://www.topps.com/pages/bowman-football"
    assert opened[0][2]["Store"] == "Topps" and opened[0][2]["When (CT)"]


def test_format_monitor(engine):
    engine.topps = [{"slug": "bowman-football", "name": "2026 Bowman Football", "sport": "Football",
                     "url": "https://www.topps.com/pages/bowman-football", "when": datetime.now(timezone.utc).isoformat(),
                     "has_time": True, "status": "Upcoming", "section": "Dropping soon"}]
    t = {"name": "Topps product pages", "type": "topps_products", "url": "u"}
    st = {}
    engine.fetcher = FakeFetch("<p>Available soon. Get notified</p>", "https://www.topps.com/pages/bowman-football")
    assert engine.check_topps_products(t, st, True)[0].startswith("ok (0 formats")
    fmt = lambda h, name, price, btn: (f'<div><a href="/products/{h}">{name}</a><span>{price}</span>'
                                       f'<button>{btn}</button></div>')
    engine.fetcher = FakeFetch(fmt("bf-mega", "2026 Bowman Football Mega Box", "$59.99", "Notify me")
                               + fmt("bf-blaster", "2026 Bowman Football Blaster Box", "$29.99", "Add to cart"),
                               "https://www.topps.com/pages/bowman-football")
    engine.check_topps_products(t, st, False)
    titles = [x for _, x in engine.sent]
    assert "🆕 Topps 2026 Bowman Football Mega Box listed · Upcoming" in titles
    assert "🆕 Topps 2026 Bowman Football Blaster Box listed · On sale" in titles
    engine.sent.clear()
    engine.fetcher = FakeFetch(fmt("bf-mega", "2026 Bowman Football Mega Box", "$59.99", "Add to cart")
                               + fmt("bf-blaster", "2026 Bowman Football Blaster Box", "$29.99", "Sold out"),
                               "https://www.topps.com/pages/bowman-football")
    engine.check_topps_products(t, st, False)
    assert engine.sent == [("urgent", "🚨 LIVE: 2026 Bowman Football Mega Box · On sale"),
                           ("normal", "Sold out: 2026 Bowman Football Blaster Box")]
    assert [f["name"] for f in engine.snapshot()["topps_formats"]["bowman-football"]] == \
        ["2026 Bowman Football Mega Box", "2026 Bowman Football Blaster Box"]


def test_listing_cards_only(engine):
    t = {"name": "Pokémon Center · card products", "url": "https://www.pokemoncenter.com/category/trading-card-game",
         "link_pattern": "/product/", "tcg_only": True, "category": "Pokémon"}
    def pc(items):
        return "<div>" + "".join(f'<a href="/product/{i}/{s}">{n}</a>' for i, (s, n) in enumerate(items)) + "</div>" + "x" * 21000
    base = [("pokemon-tcg-delta-reign-etb", "Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box"),
            ("pikachu-hat", "Pikachu 30th Celebration Hat")]
    st = {}
    engine.fetcher = FakeFetch(pc(base), t["url"])
    assert engine.check_listing(t, st, True)[0] == "ok (1 card products)"
    engine.fetcher = FakeFetch(pc(base + [("lanyard", "Pokémon Center Lanyard"),
                                          ("pokemon-tcg-booster-bundle", "Pokémon TCG: 30th Celebration Booster Bundle")]),
                               t["url"])
    engine.check_listing(t, st, False)
    assert engine.sent == [("urgent", "New card product on Pokémon Center · card products: "
                                      "Pokémon TCG: 30th Celebration Booster Bundle")]


def test_pokemon_channel_routing(engine, monkeypatch):
    from rip_radar import notify as notify_mod
    from rip_radar.notify import Notifier
    posted = []
    monkeypatch.setattr(notify_mod.Notifier, "_post_discord", staticmethod(lambda hook, payload: posted.append(hook) or True))
    settings.update({"discord_webhook": "https://main", "discord_webhook_urgent": "https://urgent",
                     "webhooks": {"pokemon": "https://poke"}})
    n = Notifier(settings.load)
    n.set_channel("pokemon")
    n.send("urgent", "PC queue live")
    assert posted == ["https://poke"]                       # only the Pokémon channel
    posted.clear()
    n.set_channel(None)
    n.send("urgent", "Topps live")
    assert sorted(posted) == ["https://main", "https://urgent"]
    posted.clear()
    settings.update({"webhooks": {"pokemon": ""}})
    n.send("normal", "PC product", channel="pokemon")
    assert posted == ["https://main"]                       # no Pokémon webhook -> main channel


def test_store_detection():
    from rip_radar.notify import store_in
    assert store_in("Walmart's Pokémon drawing opens Oct 7") == "walmart"
    assert store_in("Pokémon Center queue is live for Delta Reign") == "pokemon"
    assert store_in("Target restocks Pokémon cards Friday") == "target"
    assert store_in("Retailers targeting scalpers with new limits") is None
    assert store_in("Dick's Sporting Goods Pokémon entry") == "dicks"
    assert store_in("https://www.bestbuy.com/site/pokemon-etb/123.p") == "bestbuy"
    assert store_in("Topps Chrome Football preorder") == "topps"


def test_news_goes_to_news_channel_once(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", channel=None, **kw: got.append((title, channel))

    def rss(items):
        body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
                       f"<pubDate>{datetime.now(timezone.utc):%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>" for t, l in items)
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'
    story = "The Pokémon Center Opens Pre-Orders For Delta Reign, The Next Big TCG Set - polygon.com"
    feeds = [{"name": f"News {i}", "url": "u", "must_have": ["pre-order", "drawing"]} for i in range(3)]
    sts = [{}, {}, {}]
    engine.fetcher = FakeFetch(rss([]))
    for f, st in zip(feeds, sts):
        engine.check_feed(f, st, True)
    engine.fetcher = FakeFetch(rss([(story, "https://polygon.com/a")]))
    for i, (f, st) in enumerate(zip(feeds, sts)):
        engine.fetcher = FakeFetch(rss([(story, f"https://news.google.com/{i}")]))   # same story, 3 searches
        engine.check_feed(f, st, False)
    assert got == [("📰 " + story, "main")]                         # once, main channel, not the store channel

def test_watch_page_channel_and_settings_migration(engine):
    import json
    paths.SETTINGS_FILE.write_text(json.dumps({"discord_webhook_pokemon": "https://poke"}))
    s = settings.load()
    assert s["webhooks"]["pokemon"] == "https://poke" and "discord_webhook_pokemon" not in s
    settings.update({"watch_pages": [
        {"name": "Target ETB", "url": "https://www.target.com/p/x", "preset": "target"},
        {"name": "Mine", "url": "https://www.dickssportinggoods.com/p/y", "preset": "custom", "keywords": ["enter"]}]})
    ch = {t["name"]: t.get("channel") for t in engine.targets()}
    assert ch["Target ETB"] == "target" and ch["Mine"] == "dicks"
    assert ch["Topps release calendar"] == ["topps_calendar", "topps"] and ch["Pokémon Center queue"] == ["pokemon_queue", "pokemon"]



def test_raffles_post_to_store_and_drawings_everywhere(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", channel=None, copy_to=None, **kw: \
        got.append((title, channel, copy_to))
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
                   f"<pubDate>{datetime.now(timezone.utc):%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>"
                   for t, l in [("Dick's Sporting Goods opens Pokémon raffle entries", "https://a/1"),
                                ("Target Pokémon drawing this weekend", "https://a/2"),
                                ("Pokémon Delta Reign preorders open", "https://a/3")])
    rss = f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'
    f = {"name": "News", "url": "u", "must_have": ["raffle", "drawing", "preorder"]}
    st = {}
    engine.fetcher = FakeFetch('<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>')
    engine.check_feed(f, st, True)
    engine.fetcher = FakeFetch(rss)
    engine.check_feed(f, st, False)
    assert got == [("🎟️ Dick's Sporting Goods opens Pokémon raffle entries", "dicks", ["drawings"]),
                   ("🎟️ Target Pokémon drawing this weekend", "target", ["drawings"]),
                   ("📰 Pokémon Delta Reign preorders open", "main", None)]
    got.clear()
    page = {"name": "Dick's ETB", "url": "https://www.dickssportinggoods.com/p/x", "keywords": ["enter drawing"],
            "alert_title": "🚨 DICK'S ENTRY OPEN: Dick's ETB"}
    engine.fetcher = FakeFetch("<button>Enter drawing</button>" + "x" * 25000, page["url"])
    engine.check_keywords(page, {}, False)
    assert got == [("🚨 DICK'S ENTRY OPEN: Dick's ETB", None, ["drawings"])]
