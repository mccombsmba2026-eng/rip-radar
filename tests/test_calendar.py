from datetime import datetime, timedelta, timezone

from rip_radar import settings
from rip_radar.parsing import CT
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)


def capture(engine):
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((level, title, kw))
    return got


def test_calendar_channels_never_spill_into_main(engine, monkeypatch):
    from rip_radar import notify as notify_mod
    posted = []
    monkeypatch.setattr(notify_mod.Notifier, "_post_discord", staticmethod(lambda hook, payload: posted.append(hook) or True))
    settings.update({"discord_webhook": "https://main"})
    n = notify_mod.Notifier(settings.load)
    assert n.send("normal", "📅 added", channel="calendar", strict=True) is False
    assert n.send("normal", "📅 added", channel="calendar") is False           # calendar is always strict
    assert posted == []
    n.send("normal", "topps date moved", channel=["topps_calendar", "topps"])  # not strict: falls back to main
    assert posted == ["https://main"]
    posted.clear()
    settings.update({"webhooks": {"topps_calendar": "https://tcal", "calendar": "https://cal"}})
    n.send("normal", "topps date moved", channel=["topps_calendar", "topps"])
    n.send("normal", "📅 added", channel="calendar", strict=True)
    assert posted == ["https://tcal", "https://cal"]


def test_calendar_tick_baseline_added_reminder_digest(engine):
    got = capture(engine)
    engine.first_pass_done = True
    engine.state["boards_redropped"] = "1.0.14"                      # skip the one-time re-post
    boards = []
    engine.cal_board.tick = lambda render, force=False, repost=False, content=None: boards.append((repost, content, render()))
    engine.topps_board.tick = lambda render, force=False, repost=False, content=None: render()
    now = datetime.now(CT)
    engine._add_event("Topps: 2026 Bowman Football", "https://topps/b", now + timedelta(days=1), False, "topps", "topps")
    engine.calendar_tick()                                           # first run: existing dates are baseline
    assert not [b for b in boards if b[0]]
    got.clear()
    engine._add_event("Walmart drawing: 30th Booster Bundle", "https://walmart/draw", now + timedelta(hours=5), True,
                      "drawing", "walmart")
    engine._add_event("Pokémon Center 30th restock drops", "https://news/1", now + timedelta(minutes=10), True, "news",
                      "pokemon")                                     # news: not a calendar entry
    engine._add_event("Target: Pokémon TCG Delta Reign ETB", "https://www.target.com/p/x/-/A-1", now + timedelta(minutes=10),
                      True, "release", "target", product=True)       # a product page showing its date: yes
    boards.clear()
    engine.calendar_tick()
    assert got == []                                                 # never single posts in the calendar channel
    reposts = [b for b in boards if b[0]]
    assert len(reposts) == 1 and "Walmart drawing: 30th Booster Bundle" in reposts[0][1] and "Delta Reign ETB" in reposts[0][1]
    assert "restock" not in reposts[0][1]
    boards.clear()
    engine.calendar_tick()                                           # nothing new: no fresh post
    assert not [b for b in boards if b[0]]
    title, desc = engine._render_calendar()
    assert "Drop calendar" in title and "🎟️ [30th Booster Bundle](https://walmart/draw)" in desc
    assert "*time TBA* · 🏈 [Bowman Football](https://topps/b)" in desc          # short names, sport icon, no "All day"


def test_news_and_reddit_never_add_to_the_calendar(engine):
    got = capture(engine)
    items = [("Pokémon TCG Delta Reign release date: November 6, 2026", "https://news/dr")]
    body = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
                   f"<pubDate>{datetime.now(timezone.utc):%a, %d %b %Y %H:%M:%S} GMT</pubDate></item>" for t, l in items)
    rss = f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    t = {"name": "Dates · Pokémon TCG releases", "url": "u", "calendar_only": True, "kind": "release"}
    st = {}
    engine.fetcher = FakeFetch(empty)
    engine.check_feed(t, st, True)
    engine.fetcher = FakeFetch(rss)
    engine.check_feed(t, st, False)
    assert got == []                                                 # no ping
    assert not [e for e in engine.state["events"].values() if e["url"] == "https://news/dr"]


def test_topps_board_lists_products(engine):
    engine.topps = [{"slug": "bowman-football", "name": "2026 Bowman Football", "sport": "Football",
                     "url": "https://www.topps.com/pages/bowman-football", "when": "2026-09-30T11:00:00-05:00",
                     "has_time": True, "status": "On sale", "section": "Dropping soon"}]
    engine.topps_formats = {"bowman-football": [{"name": "Mega"}, {"name": "Blaster"}]}
    title, desc = engine._render_topps_board()
    assert "1 products" in title and "[Bowman Football]" in desc and "2 formats" in desc and "On sale" in desc
