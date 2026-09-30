import time

from rip_radar import settings
from rip_radar.parsing import is_etb_or_upc
from tests.test_engine import TOPPS, FakeFetch, engine, page  # noqa: F401  (fixture)
from tests.test_parsing import SOON, AVAIL

PAD = "x" * 21000


def test_etb_upc_detection():
    assert is_etb_or_upc("Pokémon TCG: Delta Reign Elite Trainer Box")
    assert is_etb_or_upc("Pokemon 30th Celebration ETB")
    assert is_etb_or_upc("Pokémon TCG: Charizard ex Ultra-Premium Collection")
    assert is_etb_or_upc("Mega Evolution UPC")
    assert not is_etb_or_upc("Pokémon TCG: 30th Booster Bundle")
    assert not is_etb_or_upc("Pokémon TCG: Knock Out Collection")


def test_everyone_only_when_asked(monkeypatch):
    from rip_radar import notify as notify_mod
    payloads = []
    monkeypatch.setattr(notify_mod.Notifier, "_post_discord", staticmethod(lambda h, p: payloads.append(p) or True))
    monkeypatch.setattr(settings, "load", lambda: {"discord_webhook": "https://main", "webhooks": {}})
    n = notify_mod.Notifier(settings.load)
    n.send("urgent", "In stock: booster bundle")
    n.send("urgent", "In stock: ETB", ping=True)
    assert "content" not in payloads[0] and payloads[1]["content"] == "@everyone"


def tile(tcin, name, price, button):
    return (f'<div><a href="/p/x/-/A-{tcin}"><img alt="{name}" src="https://t/{tcin}.jpg"></a>'
            f'<span>{price}</span><button>{button}</button></div>')


def test_store_pings_everyone_only_for_etb_upc_and_tracks_link_time(engine):
    t = {"name": "Target · Pokémon cards", "type": "retail_search", "store": "target", "products": "pokemon",
         "url": "https://www.target.com/s?searchTerm=pokemon"}
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", ping=False, **kw: sent.append((title, ping, fields))
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    loaded = tile("94300010", "Pokémon TCG: Delta Reign Elite Trainer Box", "$49.99", "Coming soon")
    bundle = tile("94300011", "Pokémon TCG: Delta Reign Booster Bundle", "$29.99", "Coming soon")
    engine.fetcher = FakeFetch(loaded + bundle + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    assert [(x[0], x[1]) for x in sent] == [
        ("🆕 Loaded at Target, not in stock yet: Pokémon TCG: Delta Reign Elite Trainer Box", True),
        ("🆕 Loaded at Target, not in stock yet: Pokémon TCG: Delta Reign Booster Bundle", False)]
    sent.clear()
    st["items"]["94300010"]["first_seen"] -= 2 * 86400 + 3 * 3600          # page was up 2 days 3 hours ago
    engine.fetcher = FakeFetch(tile("94300010", "Pokémon TCG: Delta Reign Elite Trainer Box", "$49.99", "Add to cart")
                               + tile("94300011", "Pokémon TCG: Delta Reign Booster Bundle", "$29.99", "Add to cart")
                               + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    (etb_title, etb_ping, etb_fields), (bb_title, bb_ping, _) = sent
    assert etb_title.startswith("🟢 BACK IN STOCK") and etb_ping is True and bb_ping is False
    assert etb_fields["Link was up"] == "2d 3h before stock"


def test_topps_calendar_changes_ping_everyone(engine):
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", ping=False, **kw: sent.append((title, ping))
    st = {}
    engine.fetcher = FakeFetch(page())
    engine.check_topps_calendar(dict(TOPPS), st, True)
    sent.clear()
    soon = [list(c) for c in SOON]
    soon[4][1] = "Wednesday, Nov 18"
    soon.append(("topps-midnight-football", "Monday, Oct 12", "2026 Topps Midnight Football", "Pre-order"))
    engine.fetcher = FakeFetch(page([tuple(c) for c in soon], AVAIL))
    engine.check_topps_calendar(dict(TOPPS), st, False)
    changes = [(t, p) for t, p in sent if "date moved" in t or "New on Topps" in t]
    assert changes and all(p for _, p in changes)
