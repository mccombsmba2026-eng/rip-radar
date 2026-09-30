"""1.0.13: one message per product, exact Target stock, Walmart drawing page, names, queue status line."""
import json
from datetime import datetime, timedelta

from rip_radar import settings
from rip_radar.cards import ProductCards, build_card
from rip_radar.parsing import CT, best_name, clean_name, parse_retail_tiles, walmart_json_items
from rip_radar.stock import parse_target_fulfillment, target_stock_text
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)

PAD = "x" * 21000


class Resp:
    def __init__(self, code=200, data=None):
        self.status_code, self._data = code, data or {}

    def json(self):
        return self._data


class FakeDiscord:
    def __init__(self):
        self.calls, self.n = [], 0

    def request(self, method, url, timeout=None, json=None):
        self.calls.append((method, url, json))
        if method == "POST":
            self.n += 1
            return Resp(200, {"id": f"m{self.n}"})
        return Resp(204)


def test_names_lose_ratings_and_badges():
    assert clean_name("10k+ bought in last month New at Topps™ 2026 Topps NFL Flagship Football Mega Box") == \
        "2026 Topps NFL Flagship Football Mega Box"
    assert best_name(["1.2 out of 5 stars with 24 ratings 24 reviews", "Pokémon SV10.5 White Flare Booster Pack",
                      "$29.99"]) == "Pokémon SV10.5 White Flare Booster Pack"
    html = ('<div><a href="/p/x/-/A-1004842211"><span>1.2 out of 5 stars with 24 ratings 24 reviews</span></a>'
            '<a href="/p/x/-/A-1004842211"><img alt="Pokémon SV10.5 White Flare Booster Pack" src="https://t/1.jpg"></a>'
            '<span>$4.99</span><button>Add to cart</button></div>' + PAD)
    (x,) = parse_retail_tiles(html, "https://www.target.com/s?searchTerm=pokemon", "target")
    assert x["name"] == "Pokémon SV10.5 White Flare Booster Pack" and x["price"] == "$4.99"


def test_card_has_links_picture_price_retail_and_stock():
    rec = {"name": "Pokémon TCG: Delta Reign Elite Trainer Box", "url": "https://www.target.com/p/x/-/A-1",
           "image": "https://t/1.jpg", "price": "$49.99", "retail": "~$49.99", "ratio": 1.0, "live": True,
           "status": "In stock", "stock": "143 available online", "stores": "Stores near 77002: Meyerland **4**",
           "limit": "Limit 2 per order"}
    body = build_card("target", rec, "🟢 BACK IN STOCK at Target")
    e = body["embeds"][0]
    assert e["title"].startswith("🟢 BACK IN STOCK at Target · Pokémon TCG: Delta Reign")
    assert e["image"]["url"] == "https://t/1.jpg" and "OPEN & ADD TO CART" in e["description"]
    f = {x["name"]: x["value"] for x in e["fields"]}
    assert f["Price"] == "$49.99" and f["Retail (MSRP)"] == "~$49.99 · ✅ at retail"
    assert f["Stock"] == "143 available online" and "Meyerland" in f["Stores"] and f["Limit"] == "Limit 2 per order"
    over = build_card("target", {**rec, "price": "$64.99", "ratio": 1.3})["embeds"][0]
    assert "⚠️ 30% above retail" in {x["name"]: x["value"] for x in over["fields"]}["Retail (MSRP)"]


def test_cards_post_once_edit_in_place_and_bump_when_back_in_stock(tmp_path):
    state = {}
    cards = ProductCards(lambda: {"webhooks": {"target": "https://hook"}}, state, pace=0)
    cards.http = d = FakeDiscord()
    rec = {"name": "Pokémon TCG: Delta Reign Elite Trainer Box", "url": "u", "price": "$49.99", "live": True,
           "status": "In stock"}
    cards.process("target:1", "target", rec, False, False, "")
    cards.process("target:1", "target", rec, False, False, "")                      # nothing changed: no call
    assert [c[0] for c in d.calls] == ["POST"]
    cards.process("target:1", "target", {**rec, "live": False, "status": "Out of stock"}, False, False, "")
    assert d.calls[-1][0] == "PATCH" and d.calls[-1][1] == "https://hook/messages/m1"
    cards.process("target:1", "target", rec, True, True, "🟢 BACK IN STOCK at Target")   # bump: fresh message
    assert [c[0] for c in d.calls[-2:]] == ["DELETE", "POST"] and d.calls[-1][2]["content"] == "@everyone"
    assert state["product_cards"]["target:1"]["id"] == "m2"
    cards.process("target:1", "target", None, False, False, "")                      # gone from listings
    assert d.calls[-1][0] == "DELETE" and "target:1" not in state["product_cards"]


def test_target_stock_counts():
    fulfil = {"data": {"product": {"tcin": "1", "fulfillment": {
        "is_out_of_stock_in_all_store_locations": False,
        "shipping_options": {"availability_status": "IN_STOCK", "available_to_promise_quantity": 143.0},
        "store_options": [{"location_name": "Houston Meyerland", "location_available_to_promise_quantity": 4.0}]}}}}
    fiats = {"data": {"fulfillment_fiats": {"locations": [
        {"store": {"location_name": "Bellaire"}, "location_available_to_promise_quantity": 7.0},
        {"store": {"location_name": "Houston Meyerland"}, "location_available_to_promise_quantity": 4.0}]}}}
    info = parse_target_fulfillment(fulfil, fiats)
    assert info["online"] == 143 and info["stores"] == [("Bellaire", 7), ("Houston Meyerland", 4)]
    stock, stores = target_stock_text(info, "77002")
    assert stock == "143 available online" and stores == "Stores near 77002: Bellaire **7** · Houston Meyerland **4**"


DRAW = ('<html><body><div class="grid">'
        '<div><img src="https://i5.walmartimages.com/a.jpeg"><span>$7994current price $79.94</span>'
        '<h3>Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle</h3><span>Drawing starts Sep 30, 2:00pm PDT</span></div>'
        '<div><img src="https://i5.walmartimages.com/b.jpeg"><span>$597current price $5.97</span>'
        '<h3>NeeDoh Nice Cube Squish Toy</h3><span>Drawing starts Sep 30, 2:00pm PDT</span></div>'
        '<div><img src="https://i5.walmartimages.com/c.jpeg"><span>$10997current price $109.97</span>'
        '<h3>Magic: The Gathering Reality Fracture Secret Lair Bundle</h3><span>Drawing starts Sep 30, 2:00pm PDT</span></div>'
        '</div></body></html>' + PAD)


def test_walmart_drawing_page_without_product_links(engine):
    import rip_radar.engine as eng_mod
    real = eng_mod.datetime

    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, 13, 0, tzinfo=CT)
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((title, fields, kw))
    engine.fetcher = FakeFetch(DRAW, "https://www.walmart.com/shop/collectibles/draw")
    t = {"name": "Walmart · drawings", "type": "walmart_drawings", "url": "https://www.walmart.com/shop/collectibles/draw",
         "channel": "walmart"}
    eng_mod.datetime = Morning
    try:
        health, _ = engine.check_walmart_drawings(t, {}, True)
    finally:
        eng_mod.datetime = real
    assert health.startswith("ok (1 card drawings")                      # Magic + NeeDoh skipped
    (title, fields, kw), = got
    assert title == "🎟️ WALMART DRAWING · opens Wed Sep 30, 4:00 PM CT: Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle"
    assert fields["Price"] == "$79.94" and kw["copy_to"] == ["drawings"]


def test_walmart_page_data_items():
    data = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"usItemId": "5550001", "name": "Pokémon TCG: Delta Reign Elite Trainer Box",
         "canonicalUrl": "/ip/Pokemon-Delta-Reign-ETB/5550001?x=1", "priceInfo": {"currentPrice": {"price": 49.97}},
         "imageInfo": {"thumbnailUrl": "https://i5.walmartimages.com/e.jpeg?odnHeight=1"},
         "availabilityStatusV2": {"value": "IN_STOCK"}, "maxOrderQuantity": 2}]}]}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>' + PAD
    (j,) = walmart_json_items(html)
    assert j["url"] == "https://www.walmart.com/ip/Pokemon-Delta-Reign-ETB/5550001" and j["price"] == "$49.97"
    (x,) = parse_retail_tiles(html, "https://www.walmart.com/search?q=pokemon", "walmart")
    assert x["live"] and x["limit"] == "Limit 2 per order" and "addToCart?items=5550001" in x["add_to_cart"]


def test_store_channel_gets_one_card_per_product_only_our_sports(engine):
    settings.update({"webhooks": {"target": "https://hook"}})
    queued = []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": queued.append((pid, bump, headline))
    engine.notify.send = lambda *a, **k: None
    t = next(x for x in engine.targets() if x["name"] == "Target · sports cards")
    st = {}

    def tile(tcin, name, price, button="Add to cart"):
        return (f'<div><a href="/p/x/-/A-{tcin}"><img alt="{name}" src="https://t/{tcin}.jpg"></a>'
                f'<span>{price}</span><button>{button}</button></div>')
    engine.fetcher = FakeFetch(tile("1012944732", "2026 Topps NFL Flagship Football Trading Card Mega Box", "$49.99")
                               + tile("1013322214", "2026 Topps MLS Chrome Soccer Trading Card Value Box", "$29.99")
                               + tile("1013250867", "2026 Topps WWE Universe Wrestling Trading Card Mega Box", "$49.99")
                               + PAD, t["url"])
    engine.check_retail_search(t, st, True)
    assert queued == [("1012944732", False, "")]                           # soccer + WWE left out, first look silent
    queued.clear()
    engine.fetcher = FakeFetch(tile("1012944732", "2026 Topps NFL Flagship Football Trading Card Mega Box", "$49.99")
                               + tile("1012944751", "2026 Topps NFL Flagship Football Trading Card Hanger Box", "$14.99")
                               + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    assert ("1012944751", True, "🟢 NEW & IN STOCK at Target") in queued


def test_queue_channel_status_line(engine, monkeypatch):
    import rip_radar.engine as eng_mod
    settings.update({"webhooks": {"pokemon_queue": "https://q"}})
    posts = []
    monkeypatch.setattr(eng_mod.requests, "post", lambda url, json=None, timeout=None: posts.append(("POST", json)) or Resp(200, {"id": "w1"}))
    monkeypatch.setattr(eng_mod.requests, "patch", lambda url, json=None, timeout=None: posts.append(("PATCH", json)) or Resp(200))
    engine.notify.send = lambda *a, **k: posts.append(("ALERT", a[1]))
    t = next(x for x in engine.targets() if x["name"] == "Pokémon Center queue")
    st = {}
    engine.fetcher = FakeFetch("<html>Shop the Pokémon Center " + PAD + "</html>", "https://www.pokemoncenter.com/")
    engine.check_keywords(t, st, True)
    assert posts[0][0] == "POST" and "No Pokémon Center queue right now" in posts[0][1]["content"]
    assert not any(p[0] == "ALERT" for p in posts)                           # no queue: nothing new posted
    engine.fetcher = FakeFetch("<html>You are now in line " + PAD + "</html>", "https://www.pokemoncenter.com/")
    engine.check_keywords(t, st, False)
    assert any(p == ("ALERT", "🚨 POKÉMON CENTER QUEUE IS LIVE") for p in posts)


def test_topps_calendar_board_uses_calendar_layout(engine):
    engine.topps = [{"slug": "bowman-football", "name": "2026 Bowman Football", "sport": "Football",
                     "url": "https://www.topps.com/pages/bowman-football", "when": "2026-09-30T11:00:00-05:00",
                     "has_time": True, "status": "Upcoming"}]
    title, text = engine._render_topps_board()
    assert "**Wednesday, Sep 30**" in text
    assert "`11:00 AM` 🃏 [2026 Bowman Football](https://www.topps.com/pages/bowman-football) · Football · Upcoming" in text
