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
    e.notify.send = lambda level, title, url="", fields=None, desc="": e.sent.append((level, title))
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
    assert [lvl for lvl, _ in engine.sent] == ["normal"]              # one summary, no spam
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
    assert any("date moved: 2026 Topps Museum" in t for t in titles)
    assert any("New on Topps calendar: 2026 Topps Midnight Football" in t for t in titles)
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
    assert engine.sent == [("urgent", "Walmart Pokemon drawing now open")]


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
