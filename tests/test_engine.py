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
    e.resale._start = lambda: None          # tests never search eBay
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
    monkeypatch.setattr(notify_mod.Notifier, "_post_discord", staticmethod(lambda hook, payload, *_: posted.append(hook) or True))
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


def test_card_raffle_and_announcement_news_only(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", channel=None, **kw: got.append((title, channel))

    def rss(items):
        body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
                       f"<pubDate>{datetime.now(timezone.utc):%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>" for t, l in items)
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'
    story = "Walmart opens Pokémon Delta Reign ETB drawing - polygon.com"
    noise = "Linkind Matter Smart Light Bulbs Drop to $13.99 on Amazon - SammyGuru"
    feeds = [{"name": f"News {i}", "url": "u", "must_have": ["pre-order", "drawing"]} for i in range(3)]
    sts = [{}, {}, {}]
    engine.fetcher = FakeFetch(rss([]))
    for f, st in zip(feeds, sts):
        engine.check_feed(f, st, True)
    engine.fetcher = FakeFetch(rss([(story, "https://polygon.com/a")]))
    for i, (f, st) in enumerate(zip(feeds, sts)):
        engine.fetcher = FakeFetch(rss([(story, f"https://news.google.com/{i}"), (noise, f"https://n/{i}"),
                                        ("The Pokémon Center Opens Pre-Orders For Delta Reign", f"https://p/{i}")]))
        engine.check_feed(f, st, False)
    # raffle: once across 3 searches, store channel; announcement: #alerts; the light-bulb deal: nowhere
    assert got == [("🎟️ " + story, "walmart"), ("📣 The Pokémon Center Opens Pre-Orders For Delta Reign", "main")]

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
                   ("📣 Pokémon Delta Reign preorders open", "main", None)]
    got.clear()
    page = {"name": "Dick's ETB", "url": "https://www.dickssportinggoods.com/p/x", "keywords": ["enter drawing"],
            "alert_title": "🚨 DICK'S ENTRY OPEN: Dick's ETB"}
    engine.fetcher = FakeFetch("<button>Enter drawing</button>" + "x" * 25000, page["url"])
    engine.check_keywords(page, {}, False)
    assert got == [("🚨 DICK'S ENTRY OPEN: Dick's ETB", None, ["drawings"])]


def test_health_check_report(engine):
    import time as _t
    engine.targets = lambda: [{"name": "Walmart Pokémon", "type": "retail_search", "url": "https://walmart.com/s"},
                              {"name": "Dick's cards", "type": "retail_search", "url": "https://dickssportinggoods.com/s"},
                              {"name": "CVS cards", "type": "retail_search", "url": "https://cvs.com/s"}]
    ts = engine.state.setdefault("targets", {})
    ts["Walmart Pokémon"] = {"health": "ok (12 card products · 2 in stock)", "last_ok_at": _t.time(),
                             "last_found_at": _t.time(), "since": _t.time() - 99999}
    ts["Dick's cards"] = {"health": "empty", "bad_since": _t.time() - 3 * 86400, "last_ok_at": 0, "since": 0}
    ts["CVS cards"] = {"health": "ok (0 card products, 4 other items skipped)", "last_ok_at": _t.time(),
                       "last_found_at": _t.time() - 2 * 86400, "since": _t.time() - 9 * 86400}
    title, desc = engine.render_health()
    assert title.startswith("🩺 Daily health check")
    assert "1 of 3 sources working" in desc
    assert "❌ **Dick's cards**" in desc and "last worked never" in desc
    assert "🟡 **CVS cards**" in desc and "2d" in desc
    assert "✅ Walmart Pokémon" in desc
    assert engine.handle_watch_command("health")[0].startswith("**🩺")


def test_run_target_records_last_found(engine):
    engine.check_fake = lambda t, st, first: ("ok (0 card products)", 200)
    t = {"name": "X", "type": "fake", "url": "https://x.com"}
    engine._run_target(t, {})
    st = engine.state["targets"]["X"]
    assert st.get("last_ok_at") and not st.get("last_found_at")
    engine.check_fake = lambda t, st, first: ("ok (3 card products)", 200)
    engine._run_target(t, {})
    assert st.get("last_found_at")


class MapFetch:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, browser=False):
        html = self.pages.get(url)
        return (200, url, html) if html is not None else (404, url, "")


EQL = {"name": "Topps raffles (EQL)", "type": "eql_launches", "url": "https://launches.topps.com/en-US/",
       "browser": True}
SPONGE = "https://launches.topps.com/en-US/launch/2026-topps-chrome-spongebob-hobby-box"
TRIB = "https://launches.topps.com/en-US/launch/2026-topps-tribute-baseball-hobby-box"


def launch_page(name, closes="2030-10-22T16:00:00Z", opens="2020-10-20T16:00:00Z"):
    return (f'<html><head><meta property="og:image" content="https://img/{name[:4]}.jpg"></head><body><h1>{name}</h1>'
            f'<p>US$249.99</p><p>Maximum of 2 per customer</p>'
            f'<script>{{"openingTime":"{opens}","closingTime":"{closes}"}}</script></body></html>')


def test_eql_raffles(engine):
    calls = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: calls.append((title, url, fields, kw))
    st = {}
    engine.fetcher = MapFetch({EQL["url"]: f'<a href="{TRIB}">Tribute</a>', TRIB: launch_page("2026 Topps Tribute")})
    assert engine.check_eql_launches(dict(EQL), st, True)[0].startswith("ok (1 launches")
    assert calls == []                                        # already up when Rip Radar first looked: quiet
    # a topps.com product page links a NEW launch before the list shows it
    engine._note_eql_links(f'<a href="{SPONGE}">Enter the drawing</a>')
    engine.fetcher = MapFetch({EQL["url"]: f'<a href="{TRIB}">Tribute</a>',
                               SPONGE: launch_page("2026 Topps Chrome SpongeBob - Hobby Box")})
    engine.check_eql_launches(dict(EQL), st, False)
    (title, url, fields, kw), = calls
    assert title.startswith("🎟️ NEW TOPPS RAFFLE (EQL) · OPEN NOW: 2026 Topps Chrome SpongeBob") and url == SPONGE
    assert kw["ping"] and kw["copy_to"] == ["drawings"] and kw["links"] == [("🎟️ ENTER THE RAFFLE ON EQL", SPONGE)]
    assert fields["Price"] == "$249.99" and fields["Limit"] == "2 per person" and "Entries close (CT)" in fields
    assert any(e["title"].startswith("Topps raffle closes: 2026 Topps Chrome SpongeBob")
               for e in engine.state["events"].values())
    # closing within the hour: one reminder
    calls.clear()
    from rip_radar.engine import CT
    engine.state["eql_launches"][SPONGE]["closes"] = (datetime.now(CT) + timedelta(minutes=40)).isoformat()
    engine.state["eql_launches"][SPONGE]["checked"] = 10 ** 10
    engine.check_eql_launches(dict(EQL), st, False)
    engine.check_eql_launches(dict(EQL), st, False)
    assert [c[0] for c in calls] == [f"⏳ Topps raffle closes in 39 min: 2026 Topps Chrome SpongeBob - Hobby Box"] or \
        (len(calls) == 1 and calls[0][0].startswith("⏳ Topps raffle closes in"))


def test_eql_empty_list_is_healthy(engine):
    engine.fetcher = MapFetch({EQL["url"]: "<p>No live launches. Check back soon.</p>"})
    assert engine.check_eql_launches(dict(EQL), {}, True)[0] == "ok · no Topps raffles open right now"


def test_pokemon_release_sources_fill_calendar(engine):
    from rip_radar.engine import CT
    from rip_radar.releases import month_article_urls
    d = datetime.now(CT) + timedelta(days=20)
    day = f"{d:%B} {d.day}, {d.year}"
    article = (f"<div><h2>Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box</h2><p>Release date: {day}</p>"
               f"<h2>Pokémon TCG: Delta Reign Plush Pikachu - {day}</h2></div><p>{'x' * 2500}</p>")
    urls = month_article_urls()
    engine.fetcher = MapFetch({urls[0][1]: article, urls[1][1]: article})    # second URL style answers
    h, _ = engine.check_pokemon_releases({"name": "Pokémon releases", "type": "pokemon_releases"}, {}, True)
    assert h.startswith("ok (1 Pokémon releases known · 1 new)"), h
    press = {"name": "press", "type": "release_schedule", "url": "https://press.pokemon.com/s"}
    engine.fetcher = MapFetch({press["url"]: f"<table><tr><td><a href='/i/1'>Pokémon TCG: Mega Evolution Delta Reign "
                                             f"Elite Trainer Box</a></td><td>{d.month}/{d.day}/{d.year}</td></tr></table>"})
    h, _ = engine.check_release_schedule(dict(press), {}, True)
    assert h.startswith("ok (1 products · 1 upcoming"), h
    titles = [e["title"] for e in engine.state["events"].values() if e["kind"] == "pokemon"]
    assert len(titles) == 1 and "Delta Reign Elite Trainer Box" in titles[0]     # same box from 2 sources = once


def test_release_dates_on_calendar_board(engine):
    from rip_radar.engine import CT
    engine._add_pokemon_release("Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box",
                                datetime.now(CT) + timedelta(days=5), "https://press.pokemon.com/s", "press")
    engine._add_pokemon_release("Mega Evolution Delta Reign Elite Trainer Box", datetime.now(CT) + timedelta(days=5),
                                "https://pokemon.com/a", "article")
    engine._add_pokemon_release("Pokémon TCG: 30th Celebration Ultra-Premium Collection",
                                datetime.now(CT) + timedelta(days=40), "https://press.pokemon.com/s", "press")
    assert len(engine.state["events"]) == 2
    title, body = engine._render_calendar()
    assert "🎴 [Mega Evolution—Delta Reign Elite Trainer Box]" in body
    assert "Pokémon releases after that" in body and "30th Celebration Ultra-Premium Collection" in body


def test_queue_drop_window(engine):
    from rip_radar.engine import CT
    assert engine.queue_window(datetime(2026, 10, 20, 11, 30, tzinfo=CT))          # Tuesday 11:30 AM
    assert not engine.queue_window(datetime(2026, 10, 20, 15, 5, tzinfo=CT))       # after 3 PM
    assert not engine.queue_window(datetime(2026, 10, 18, 11, 0, tzinfo=CT))       # Sunday
    settings.update({"queue_window": {"days": []}})
    assert not engine.queue_window(datetime(2026, 10, 20, 11, 30, tzinfo=CT))
    engine._queue_count(False)
    engine._queue_count(True)
    assert engine.queue_reliability().startswith("today 1 of 2 checks got through (50%)")


def test_drawings_board_only_when_a_raffle_is_caught(engine, monkeypatch):
    from rip_radar import notify
    assert engine._render_drawings() is None
    calls = []
    monkeypatch.setattr(notify.requests, "delete", lambda url, **kw: calls.append(("DELETE", url)))
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: calls.append(("POST", url)))
    engine.drawings_board.hook = lambda: "https://hook"
    engine.state["board_drawings"] = {"hook": "https://hook", "id": "55"}
    engine.drawings_board.tick(engine._render_drawings, force=True, repost=True)
    assert calls == [("DELETE", "https://hook/messages/55")] and "board_drawings" not in engine.state
    engine.state["eql_launches"] = {"https://launches.topps.com/en-US/launch/x": {"name": "2026 Topps Chrome F1",
                                                                                "status": "Open", "price": "$479.99"}}
    (title, body), = engine._render_drawings()
    assert "Topps (EQL)" in body and "Chrome F1" in body


ARCHIVE = """<html><body><a href="/tcgdropradar/archive/">Archive</a>
<a href="https://buttondown.com/tcgdropradar/archive/pokemon-center-queue-just-opened-delta-reign/"><h3>Pokémon Center queue
 just opened — Delta Reign likely next</h3><p>Sep 30</p></a>
<a href="/tcgdropradar/archive/rgb-mew-vs-mewtwo-fur/"><h3>RGB Mew vs Mewtwo FUR — the $3k secret</h3></a></body></html>"""


def test_dropradar_archive(engine):
    calls = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: calls.append((title, fields, kw))
    t = {"name": "TCG Drop Radar posts", "type": "dropradar_archive", "url": "https://buttondown.com/tcgdropradar/archive/",
         "rss": ["https://buttondown.com/tcgdropradar/rss"]}
    st = {}
    engine.fetcher = MapFetch({t["url"]: ARCHIVE})                       # no RSS -> the archive page
    assert engine.check_dropradar_archive(dict(t), st, True)[0] == "ok (2 posts)"
    assert calls == []
    more = ARCHIVE + ('<a href="/tcgdropradar/archive/pokemon-center-queue-is-live-right-now/"><h3>Pokémon Center queue '
                      'is live right now</h3></a><a href="/tcgdropradar/archive/walmart-drawing-oct-21/"><h3>Walmart drawing '
                      'Oct 21 at 5pm ET — Delta Reign Elite Trainer Box</h3></a><a href="/tcgdropradar/archive/best-pulls/">'
                      '<h3>Best pulls of the week</h3></a>')
    engine.fetcher = MapFetch({t["url"]: more})
    engine.check_dropradar_archive(dict(t), st, False)
    titles = {c[0]: c for c in calls}
    q = titles["📰 TCG Drop Radar: Pokémon Center queue is live right now"]
    assert q[2]["ping"] is True and "NOT seen" in q[1]["Our queue check"]
    w = titles["📰 TCG Drop Radar: Walmart drawing Oct 21 at 5pm ET — Delta Reign Elite Trainer Box"]
    assert w[2]["channel"] == ["walmart", "pokemon"] and w[2]["copy_to"] == ["drawings"] and w[2]["ping"] is True
    assert len(calls) == 2                                               # the pulls article is skipped


def test_phone_alarm_only_for_queue(engine, monkeypatch):
    from rip_radar import notify
    posts = []
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: posts.append((url, kw)) or type(
        "R", (), {"status_code": 200, "json": lambda self: {"id": "1"}, "text": ""})())
    settings.update({"ntfy_topic": "rr-test", "twilio": {"account_sid": "AC1", "auth_token": "t", "from": "+1", "to": "+2"}})
    n = notify.Notifier(settings.load)
    n.send("urgent", "IN STOCK: ETB", "https://x")
    assert not [u for u, _ in posts if "ntfy" in u or "twilio" in u]          # a normal drop: no text, no alarm
    n.send("urgent", "🚨 POKÉMON CENTER QUEUE IS LIVE", "https://www.pokemoncenter.com/", alarm=True)
    ntfy = [kw for u, kw in posts if "ntfy.sh/rr-test" in u]
    sms = [kw for u, kw in posts if "api.twilio.com" in u]
    assert ntfy and ntfy[0]["headers"]["Priority"] == "max" and sms and "QUEUE IS LIVE" in sms[0]["data"]["Body"]
    for v in ntfy[0]["headers"].values():          # HTTP headers must be latin-1 (an emoji crashed 1.0.68's alarm)
        v.encode("latin-1")
    import requests as _rq
    _rq.Request("POST", "https://ntfy.sh/rr-test", headers=ntfy[0]["headers"], data=ntfy[0]["data"]).prepare()
