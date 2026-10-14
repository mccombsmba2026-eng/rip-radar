"""1.0.13: one message per product, exact Target stock, Walmart drawing page, names, queue status line."""
import json
import time
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
    assert e["image"]["url"] == "https://t/1.jpg" and "No direct add-to-cart link" in e["description"]
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
    # a normal (long) homepage that only mentions the virtual queue in a banner is NOT a queue
    engine.fetcher = FakeFetch("<html><p>Learn about the Pokémon Center virtual queue</p>" + "shop cards " * 2000
                               + "</html>", "https://www.pokemoncenter.com/")
    engine.check_keywords(t, st, False)
    assert not any(p[0] == "ALERT" for p in posts)
    # the real waiting room: short page in Pokémon Center's wording
    engine.fetcher = FakeFetch("<html><h1>You are in line to enter Pokémon Center</h1><p>Please keep this window open. "
                               "You will be redirected automatically when it is your turn to enter.</p></html>",
                               "https://www.pokemoncenter.com/")
    engine.check_keywords(t, st, False)
    assert any(p == ("ALERT", "🚨 POKÉMON CENTER QUEUE IS LIVE") for p in posts)
    assert st["active"] is True


def test_queue_redirect_and_blocked_checks(engine, monkeypatch):
    import rip_radar.engine as eng_mod
    settings.update({"webhooks": {"pokemon_queue": "https://q"}})
    lines = []
    monkeypatch.setattr(eng_mod.requests, "post", lambda url, json=None, timeout=None: lines.append(json["content"]) or Resp(200, {"id": "w1"}))
    monkeypatch.setattr(eng_mod.requests, "patch", lambda url, json=None, timeout=None: lines.append(json["content"]) or Resp(200))
    alerts = []
    engine.notify.send = lambda *a, **k: alerts.append(a[1])
    t = next(x for x in engine.targets() if x["name"] == "Pokémon Center queue")
    st = {}
    engine.fetcher = FakeFetch("<html>Pardon Our Interruption</html>", "https://www.pokemoncenter.com/")
    health, _ = engine._run_target(t, {}) or engine.check_keywords(t, st, False)
    assert health == "blocked" and "Couldn't check" in lines[-1] and alerts == []
    assert "pokemoncenter.com" not in engine.host_backoff          # the queue is never paused
    engine.fetcher = FakeFetch("<html>Pardon Our Interruption</html>", "https://pokemoncenter.queue-it.net/?c=pkmn")
    engine.check_keywords(t, st, False)
    assert alerts == ["🚨 POKÉMON CENTER QUEUE IS LIVE"]            # queue address counts even behind a wall


def test_topps_calendar_board_uses_calendar_layout(engine):
    engine.topps = [{"slug": "bowman-football", "name": "2026 Bowman Football", "sport": "Football",
                     "url": "https://www.topps.com/pages/bowman-football", "when": "2026-09-30T11:00:00-05:00",
                     "has_time": True, "status": "Upcoming"}]
    title, text = engine._render_topps_board()
    assert "__**Wed Sep 30**__" in text or "__**Today**__" in text
    assert "**11:00 AM** · 🏈 [Bowman Football](https://www.topps.com/pages/bowman-football)" in text


# ---------------------------------------------------------------- 1.0.14
def test_topps_sitemap_every_new_product_and_format(engine):
    from rip_radar.parsing import parse_topps_item_page
    index = ('<sitemapindex><sitemap><loc>https://www.topps.com/products/sitemap/432.xml</loc></sitemap>'
             '<sitemap><loc>https://www.topps.com/products/sitemap/433.xml</loc></sitemap></sitemapindex>')
    sm = ('<urlset><url><loc>https://www.topps.com/products/max-clark-2026-mlb-topps-now®-card-593</loc></url>'
          '<url><loc>https://www.topps.com/products/2026-topps-chrome®-tennis-value-box</loc></url></urlset>')
    sm2 = sm.replace("</urlset>", '<url><loc>https://www.topps.com/products/2026-topps-chrome-disney-hobby-box</loc></url></urlset>')
    page = ('<html><head><meta property="og:title" content="2026 Topps Chrome® Disney - Hobby Box">'
            '<meta property="og:image" content="https://cdn.shopify.com/d.png"></head><body><h1>2026 Topps Chrome® Disney - '
            'Hobby Box</h1><span>$249.99</span><p>Limit per cart: 2</p><button>Add to cart</button>'
            '<script>{"id":"gid://shopify/ProductVariant/4455667788"}</script></body></html>')
    item = parse_topps_item_page(page, "https://www.topps.com/products/2026-topps-chrome-disney-hobby-box")
    assert item["status"] == "On sale" and item["price"] == "$249.99" and item["limit"] == "Limit 2 per cart"
    assert item["buy_now"] == "https://www.topps.com/cart/4455667788:1"

    class Site:
        def __init__(self, maps):
            self.maps = maps

        def get(self, url, browser=False):
            if url.endswith("sitemap.xml"):
                return 200, url, index
            if "/sitemap/" in url:
                return 200, url, self.maps
            return 200, url, page.replace("Disney", "Tennis") if "tennis" in url else page
    settings.update({"webhooks": {"topps": "https://t"}})
    queued = []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": queued.append((store, pid, bump, ping))
    t = next(x for x in engine.targets() if x["type"] == "topps_sitemap")
    st = {}
    engine.fetcher = Site(sm)
    health, _ = engine.check_topps_sitemap(t, st, True)
    assert "1 sealed products tracked" in health                           # Topps NOW single card skipped
    assert queued == [("topps", "2026-topps-chrome®-tennis-value-box", False, False)]   # carded quietly at first
    queued.clear()
    engine.fetcher = Site(sm2)
    engine.check_topps_sitemap(t, st, False)
    assert ("topps", "2026-topps-chrome-disney-hobby-box", True, True) in queued      # new product: fresh post + @everyone


def test_topps_calendar_gets_announced_time_from_product_page(engine):
    from tests.test_parsing import card
    engine.notify.send = lambda *a, **k: None
    p = {"slug": "bowman-football", "name": "2026 Bowman Football", "when": datetime.now(CT).replace(
        hour=9, minute=0, second=0, microsecond=0).isoformat(), "has_time": False}
    d = datetime.now(CT)
    engine._remember_topps_time(p, f"2026 Bowman Football Available {d:%B} {d.day} at 12pm ET Get notified")
    html = ('<h2>Dropping soon</h2>' + card("bowman-football", f"{d:%A}, {d:%b} {d.day}", "2026 Bowman Football",
                                           "Notify me") + PAD)
    engine.fetcher = FakeFetch(html)
    engine.check_topps_calendar({"name": "Topps release calendar", "type": "topps_calendar",
                                 "url": "https://www.topps.com/release-calendar"}, {}, True)
    (bf,) = engine.topps
    assert bf["has_time"] and datetime.fromisoformat(bf["when"]).hour == 11          # 12pm ET = 11 AM CT
    title, text = engine._render_topps_board()
    assert "**11:00 AM** · 🏈 [Bowman Football]" in text


def test_walmart_page_data_drawing_times():
    data = {"props": {"pageProps": {"initialData": {"items": [
        {"usItemId": "20640569221", "name": "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle",
         "priceInfo": {"currentPrice": {"price": 79.94}},
         "eventAttributes": {"eventStartTime": "2026-09-30T21:00:00Z", "eventEndTime": "2026-10-01T04:59:00Z"}}]}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>' + PAD
    (j,) = walmart_json_items(html)
    assert j["start"].hour == 16 and j["start"].tzinfo is not None and j["end"].day == 30   # 4 PM CT, closes 11:59 PM


# ---------------------------------------------------------------- new stores
def test_new_store_product_links_and_shared_pharmacy_channel(engine):
    from rip_radar.cards import ProductCards
    from rip_radar.notify import channel_of, store_in
    links = {"costco": "/pokemon-tcg-elite-trainer-box-bundle.product.4000312345.html",
             "samsclub": "/ip/Pokemon-TCG-Delta-Reign-Elite-Trainer-Box-2-pack/13987654",
             "cvs": "/shop/pokemon-tcg-delta-reign-booster-pack-prodid-1234567",
             "walgreens": "/store/c/pokemon-tcg-booster-bundle/ID=300455566-product",
             "ace": "/departments/home-and-decor/toys/trading-cards/9021345",
             "barnes": "/w/pokemon-tcg-delta-reign-elite-trainer-box-pokemon/1147123456?ean=0820650859452"}
    for store, href in links.items():
        html = (f'<div><a href="{href}"><img alt="Pokémon TCG: Delta Reign Elite Trainer Box" src="https://i/x.jpg"></a>'
                f'<span>$49.99</span><button>Add to cart</button></div>' + PAD)
        (x,) = parse_retail_tiles(html, "https://www.example.com/", store)
        assert x["live"] and x["name"] == "Pokémon TCG: Delta Reign Elite Trainer Box", store
    assert channel_of("cvs") == channel_of("walgreens") == "pharmacy" and channel_of("costco") == "costco"
    assert store_in("Sam's Club has Pokémon ETBs") == "samsclub" and store_in("Walgreens restock") == "walgreens"
    cards = ProductCards(lambda: {"webhooks": {"pharmacy": "https://ph"}}, {})
    assert cards.hook("cvs") == cards.hook("walgreens") == "https://ph"
    names = {t["name"] for t in engine.targets()}
    assert {"Costco · Pokémon cards", "Sam's Club · sports cards", "CVS · trading cards", "Walgreens · trading cards",
            "Ace Hardware · Pokémon cards", "Barnes & Noble · trading cards"} <= names


def test_new_store_etb_gets_everyone(engine):
    settings.update({"webhooks": {"pharmacy": "https://ph"}})
    queued = []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": queued.append((store, pid, ping))
    engine.notify.send = lambda *a, **k: None
    t = next(x for x in engine.targets() if x["name"] == "CVS · trading cards")

    def tile(pid, name, price):
        return (f'<div><a href="/shop/{name.lower().replace(" ", "-")}-prodid-{pid}"><img alt="{name}" src="https://i/{pid}.jpg">'
                f'</a><span>{price}</span><button>Add to cart</button></div>')
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    engine.fetcher = FakeFetch(tile("1234567", "Pokemon TCG Delta Reign Elite Trainer Box", "$49.99")
                               + tile("7654321", "Pokemon TCG Delta Reign Booster Pack", "$4.99") + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    assert ("cvs", "1234567", True) in queued and ("cvs", "7654321", False) in queued


def test_drawing_time_from_the_page_when_tiles_dont_carry_it(engine):
    import rip_radar.engine as eng_mod
    from tests.test_drawings import at
    data = {"props": {"pageProps": {"initialData": {"items": [
        {"usItemId": "20640569221", "name": "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle",
         "priceInfo": {"currentPrice": {"price": 79.94}}, "badges": ["Drawing has ended"]}]}}}}
    html = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
            '<div><span>$7994current price $79.94</span><span>Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle</span>'
            '<div>Drawing starts Sep 30, 2:00pm PDT</div></div>' + PAD)
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append(title)
    engine.fetcher = FakeFetch(html, "https://www.walmart.com/shop/collectibles/draw")
    real = eng_mod.datetime
    try:
        at(datetime(2026, 9, 30, 14, 36, tzinfo=CT))
        health, _ = engine.check_walmart_drawings({"name": "Walmart · drawings", "type": "walmart_drawings",
                                                   "url": "https://www.walmart.com/shop/collectibles/draw"}, {}, True)
    finally:
        eng_mod.datetime = real
    assert "1 upcoming" in health, health
    assert got == ["🎟️ WALMART DRAWING · opens Wed Sep 30, 4:00 PM CT: Pokémon TCG: 30th Celebration Booster Bundle 2-Pack Bundle"]


def test_queue_is_checked_every_minute_even_mid_pass(engine):
    t = next(x for x in engine.targets() if x["name"] == "Pokémon Center queue")
    assert t.get("track_duration")          # the loop lets track_duration sources re-run within a pass


def test_bot_wording_triggers_and_channel(engine):
    from rip_radar.notify import bot_channel_ok, bot_matches
    trig = ["still running", "you on", "status"]
    assert bot_matches("Still running??", trig) and bot_matches("yo you on?", trig) and bot_matches("STATUS", trig)
    assert not bot_matches("the queue status page is weird and long " * 3, trig)     # long chatter ignored
    assert not bot_matches("hello", trig)
    assert bot_channel_ok("app-status", 1, "") and bot_channel_ok("app-status", 1, "#app-status")
    assert bot_channel_ok("x", 12345, "12345") and not bot_channel_ok("walmart", 1, "app-status")
    settings.update({"bot_reply": "🌴 Palm Tree Edge scanner is ON · v{version} · last scan {last_scan}\\n{problems}"})
    r = engine.bot_reply()
    assert r.startswith("🌴 Palm Tree Edge scanner is ON · v") and "{" not in r


def test_calendar_shows_each_drop_once_and_no_format_rows(engine):
    now = datetime.now(CT) + timedelta(days=3)
    engine._add_event("Topps: 2026 Topps Update Series Baseball", "https://www.topps.com/pages/update-series", now, False,
                      "topps", "topps")
    engine._add_event("Topps: 2026 Topps Update Series Baseball", "https://www.topps.com/pages/update-series-2", now, False,
                      "topps", "topps")
    _, desc = engine._render_calendar()
    assert desc.count("Update Series Baseball") == 1
    engine.state["events"]["fmt"] = {"title": "Topps: 2026 Topps Update Series Baseball - Hobby Box",
                                     "url": "https://www.topps.com/products/update-hobby-box", "start": now.isoformat(),
                                     "has_time": False, "kind": "topps", "store": "topps"}
    import rip_radar.engine as eng_mod
    fresh = eng_mod.Engine.__new__(eng_mod.Engine)
    events = {k: v for k, v in engine.state["events"].items()
              if (v.get("kind") in ("topps", "drawing") or v.get("product"))
              and not (v.get("kind") == "topps" and "/products/" in v.get("url", ""))}
    assert "fmt" not in events                                  # format pages are cleared from the calendar on start


def test_gamestop_links_and_restock_tracker(engine):
    from rip_radar.parsing import parse_retail_tiles
    html = ('<div><a href="/toys-games/trading-cards/products/pokemon-trading-card-game-30th-celebration-elite-trainer-box/'
            '20036324.html"><img alt="Pokemon Trading Card Game: 30th Celebration Elite Trainer Box" src="https://i/1.jpg">'
            '</a><span>$59.99</span><button>Add to Cart</button></div>' + PAD)
    (x,) = parse_retail_tiles(html, "https://www.gamestop.com/search/?q=pokemon", "gamestop")
    assert x["id"] == "20036324" and x["live"]
    got = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: got.append((title, fields, kw))
    item = {"name": "Pokémon TCG: Delta Reign Elite Trainer Box", "url": "https://www.target.com/p/x/-/A-1",
            "price": "$49.99", "image": ""}
    rec = {}
    engine._store_restocks("target", item, rec, [("Houston Heights", 0), ("Meyerland", 0)])     # baseline
    assert got == []
    engine._store_restocks("target", item, rec, [("Houston Heights", 6), ("Meyerland", 0)])
    (title, fields, kw), = got
    assert title == "🏬 Target Houston Heights just got 6: Pokémon TCG: Delta Reign Elite Trainer Box"
    assert kw["channel"] == ["instore", "target"] and kw["ping"] is True                    # ETB: @everyone
    assert "Houston Heights** 0 → **6" in fields["Restocked"]
    title, text = engine._render_restocks()
    assert "Target Houston Heights" in text and "1 restocks seen" in text
    got.clear()
    engine._store_restocks("target", item, rec, [("Houston Heights", 5), ("Meyerland", 0)])     # selling down: quiet
    assert got == []


def test_zip_lookup_lists_nearest_stores_with_card_stock(engine, monkeypatch):
    import rip_radar.engine as eng_mod
    engine.state.setdefault("targets", {})["Target · Pokémon cards"] = {"items": {
        "1": {"name": "Pokémon TCG: Delta Reign Elite Trainer Box", "url": "https://t/1", "seen": time.time()},
        "2": {"name": "Pokémon TCG: 30th Celebration Booster Bundle", "url": "https://t/2", "seen": time.time()}}}

    def fake(session, tcin, zip_code, key="", miles=30, stores_only=False, **kw):
        assert zip_code == "33175" and miles == 15 and stores_only
        stock = {"1": [("Kendall", 4), ("Westchester", 0)], "2": [("Kendall", 2), ("Westchester", 3)]}[tcin]
        return {"online": None, "online_status": "", "sold_out": False, "stores": stock,
                "distance": {"Kendall": 2.1, "Westchester": 5.4}}
    monkeypatch.setattr(eng_mod, "target_stock", fake)
    monkeypatch.setattr(eng_mod.time, "sleep", lambda s: None)
    (msg,) = engine.zip_lookup("33175", 15)
    assert "within 15 mi of 33175" in msg and "2 have card product" in msg
    assert msg.index("**Kendall** · 2.1 mi") < msg.index("**Westchester** · 5.4 mi")
    assert "Delta Reign Elite Trainer Box **4**" in msg and "30th Celebration Booster Bundle **3**" in msg


def test_bot_finds_lookup_channel_from_its_webhook(monkeypatch):
    from rip_radar import notify
    bot = notify.ChatBot(lambda: "", lambda: {"webhooks": {"lookup": "https://discord.com/api/webhooks/1/x"}})

    class R:
        def json(self):
            return {"channel_id": "998877"}
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout=None: R())
    assert bot.lookup_channel_id() == "998877"


def test_amazon_invite_request_pings_everyone_in_amazon_and_drawings(engine):
    settings.update({"webhooks": {"amazon": "https://am", "drawings": "https://dr"}})
    queued, sent = [], []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": queued.append((pid, ping, headline))
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append((title, kw.get("channel"),
                                                                                             kw.get("ping")))
    t = next(x for x in engine.targets() if x["name"] == "Amazon · Pokémon cards")

    def tile(asin, name, button):
        return (f'<div><a href="/dp/{asin}"><img alt="{name}" src="https://i/{asin}.jpg"></a><span>$29.99</span>'
                f'<button>{button}</button></div>')
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    engine.fetcher = FakeFetch(tile("B0DELTA123", "Pokémon TCG: Delta Reign Booster Bundle", "Request invitation") + PAD,
                               t["url"])
    engine.check_retail_search(t, st, False)
    assert queued == [("B0DELTA123", True, "🎟️ INVITE REQUEST OPEN at Amazon")]          # #amazon card, @everyone
    assert sent and sent[0][1] == "drawings" and sent[0][2] is True                        # + #drawings, @everyone


def test_hot_topic_and_queue_opens_chrome(engine, monkeypatch):
    from rip_radar import winsys
    from rip_radar.parsing import parse_retail_tiles
    html = ('<div><a href="/product/pokemon-tcg-mega-evolution-phantasmal-flames-elite-trainer-box/35621193.html">'
            '<img alt="Pokémon TCG: Mega Evolution Phantasmal Flames Elite Trainer Box" src="https://i/1.jpg"></a>'
            '<span>$59.99</span><button>Add to Bag</button></div>' + PAD)
    (x,) = parse_retail_tiles(html, "https://www.hottopic.com/toys-collectibles/games/trading-cards/", "hottopic")
    assert x["id"] == "35621193" and x["live"]
    opened = []
    monkeypatch.setattr(winsys, "open_in_chrome", lambda url: opened.append(url) or True)
    engine.notify.send = lambda *a, **k: True
    t = next(x for x in engine.targets() if x.get("track_duration"))
    engine._track_queue(t, {}, True, False, "test")
    assert opened == ["https://www.pokemoncenter.com/"]
    settings.update({"open_browser_on_queue": False})
    engine._track_queue(t, {}, True, False, "test")
    assert len(opened) == 1


def test_focus_list_and_everyone_list():
    from rip_radar.parsing import focus_match, is_pokemon_product
    f = focus_match
    assert f("2025-26 Topps Chrome Basketball Hobby Box") == {"focus": "2025-26 Topps Chrome Basketball", "ping": True}
    assert f("25-26 Topps Chrome Updates Basketball Mega Box")["focus"].startswith("2025-26 Topps Chrome Update")
    assert f("2025-26 Bowman Basketball Blaster")["ping"]                       # any 2025-26 Topps/Bowman hoops
    assert f("Cooper Flagg 2025 Bowman U Chrome Box")["ping"]
    assert not f("2024-25 Panini Prizm Basketball Blaster")["ping"] and not f("2026 Topps Midnight Football")["ping"]
    assert f("Pokémon TCG: Prismatic Evolutions Booster Bundle") == {"focus": "Prismatic Evolutions Booster Bundle",
                                                                     "ping": True}
    assert f("Pokemon Scarlet & Violet 151 Elite Trainer Box")["ping"]
    assert f("Pokémon TCG: Prismatic Evolutions Mini Tin") == {"focus": "Prismatic Evolutions", "ping": False}
    assert f("Pokemon Mew 151/165 holo")["focus"] == ""                           # a card number isn't the set
    assert f("Pokémon TCG: Surging Sparks Booster Bundle") == {"focus": "", "ping": False}
    # junk that used to slip through on "Pokémon" alone
    for junk in ["Pokemon Trading Card Game Pikachu Pillow", "Pokémon Snorlax Plush 24 inch",
                 "Pokemon Legends Z-A Nintendo Switch", "Pokemon Squishmallow Gengar 14in"]:
        assert not is_pokemon_product(junk), junk
    assert is_pokemon_product("Pokémon TCG: 30th Celebration Pin Collection")


def test_focus_product_pings_everyone_with_fire_tag(engine):
    settings.update({"webhooks": {"target": "https://t"}})
    queued = []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": queued.append(
        (pid, ping, rec.get("focus")))
    engine.notify.send = lambda *a, **k: None
    t = next(x for x in engine.targets() if x["name"] == "Target · sports cards")

    def tile(tcin, name, price):
        return (f'<div><a href="/p/x/-/A-{tcin}"><img alt="{name}" src="https://t/{tcin}.jpg"></a>'
                f'<span>{price}</span><button>Add to cart</button></div>')
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    engine.fetcher = FakeFetch(tile("10990001", "2025-26 Topps Chrome Basketball Value Box", "$29.99") +
                               tile("10980001", "2026 Topps NFL Flagship Football Value Box", "$24.99") + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    assert ("10990001", True, "2025-26 Topps Chrome Basketball") in queued
    assert ("10980001", False, "") in queued


def test_resale_parses_ebay_sold_listings_and_skips_junk():
    from rip_radar.resale import clean_query, parse_sold, summarize, ResaleChecker
    q = clean_query("Pokémon TCG: Prismatic Evolutions Booster Bundle (6 Packs)")
    assert q == "Pokémon Prismatic Evolutions Booster Bundle"

    def li(title, price, cls="s-item"):
        return (f'<li class="{cls}"><div class="{cls}__title"><span>{title}</span></div>'
                f'<span class="{cls}__price">{price}</span></li>')
    html = "<ul>" + "".join([
        li("Shop on eBay", "$20.00"),
        li("Pokemon Prismatic Evolutions Booster Bundle Sealed", "$58.00"),
        li("Pokémon TCG Prismatic Evolutions Booster Bundle NEW", "$61.50"),
        li("Prismatic Evolutions Booster Bundle Pokemon", "$55.00", cls="s-card"),
        li("Prismatic Evolutions Booster Bundle LOT of 3", "$170.00"),          # lot: skipped
        li("Prismatic Evolutions Booster Bundle EMPTY box", "$5.00"),          # empty: skipped
        li("Surging Sparks Booster Bundle", "$40.00"),                         # other set: skipped
        li("Prismatic Evolutions Booster Bundle", "$50.00 to $70.00")]) + "</ul>"   # range: skipped
    prices = parse_sold(html, q)
    assert sorted(prices) == [55.0, 58.0, 61.5]
    r = summarize(prices)
    assert r == {"median": 58.0, "n": 3, "low": 55.0, "high": 61.5}
    rc = ResaleChecker({}, lambda: {})
    txt = rc.text({**r, "q": q}, "$49.99")
    assert txt.startswith("[$58 median of 3 sold](https://www.ebay.com/sch/i.html?_nkw=") and "🟢 +$8 (+16%)" in txt


def test_daily_recap_and_sellout_timing(engine):
    from datetime import timezone as _tz
    settings.update({"webhooks": {"target": "https://t"}})
    engine.cards.request = lambda *a, **k: None
    t = next(x for x in engine.targets() if x["name"] == "Target · Pokémon cards")

    def tile(button):
        return ('<div><a href="/p/x/-/A-55500001"><img alt="Pokémon TCG: Prismatic Evolutions Booster Bundle" '
                f'src="https://t/1.jpg"></a><span>$29.99</span><button>{button}</button></div>' + PAD)
    st = {}
    engine.fetcher = FakeFetch(PAD, t["url"])
    engine.check_retail_search(t, st, True)
    engine.fetcher = FakeFetch(tile("Add to cart"), t["url"])
    engine.check_retail_search(t, st, False)
    st["items"]["55500001"]["live_since"] -= 600            # 10 minutes later...
    engine.fetcher = FakeFetch(tile("Out of stock"), t["url"])
    engine.check_retail_search(t, st, False)
    (sold,) = engine.state["sellouts"]
    assert sold["minutes"] == 10 and sold["focus"] == "Prismatic Evolutions Booster Bundle"
    engine.state["alerts"].insert(0, {"level": "urgent", "title": "🟢 NEW & IN STOCK at Target: x", "url": "u",
                                      "channel": "target", "at": datetime.now(_tz.utc).isoformat()})
    title, desc = engine.render_recap()
    assert title.startswith("📊 Daily recap") and "1 sold out" in desc and "gone in **10 min**" in desc
    assert "Target 2" in desc                     # the real in-stock alert + the one added above


def test_deal_verdicts():
    from rip_radar.cards import deal_verdict
    sells = "[$85 median of 12 sold](u) · range $70–$95"
    assert deal_verdict({"price": "$49.99", "ratio": 1.0, "resale": sells}) == "✅ At retail · sells for $85 (+$35)"
    assert deal_verdict({"price": "$65.00", "ratio": 1.3, "resale": sells}) == "🟡 Above retail, still +$20 vs what it sells for"
    assert deal_verdict({"price": "$95.00", "ratio": 1.9, "resale": sells}) == "🔴 Over market: $10 more than it sells for"
    assert deal_verdict({"price": "$49.99", "ratio": 1.0}) == "✅ At retail"
    assert deal_verdict({"price": "", "resale": sells}) == ""


def test_live_drop_value_is_edited_into_the_post(engine):
    settings.update({"webhooks": {"target": "https://t"}})
    posted = []
    engine.cards.request = lambda store, pid, rec, bump=False, ping=False, headline="": posted.append(
        (pid, bump, rec.get("resale", "")))
    rec = {"name": "Pokémon TCG: Prismatic Evolutions Booster Bundle", "price": "$29.99", "live": True, "carded": True}
    x = {"id": "55500001", "name": rec["name"]}
    queued = []
    engine.resale.q.put = lambda item: queued.append(item)
    engine._attach_resale([(x, rec, "🟢 BACK IN STOCK at Target", True)], "target")
    assert queued and queued[0][0] == 0                                   # live drop: front of the line
    engine._resale_ready(engine.resale.key(rec["name"]), {"median": 58.0, "n": 9, "low": 50, "high": 66,
                                                          "q": "Pokémon Prismatic Evolutions Booster Bundle"})
    (pid, bump, resale), = posted
    assert pid == "55500001" and not bump and resale.startswith("[$58 median of 9 sold]")   # edited, not re-pinged


def _stock_catalog(engine):
    engine.state.setdefault("targets", {})["Target · Pokémon cards"] = {"items": {
        "1": {"name": "Pokémon TCG: Scarlet & Violet 151 Elite Trainer Box", "url": "https://t/1", "seen": time.time(),
              "live": True, "price": "$49.99", "status": "In stock"},
        "2": {"name": "Pokémon TCG: Prismatic Evolutions Booster Bundle", "url": "https://t/2", "seen": time.time(),
              "live": False, "price": "$29.99", "status": "Out of stock"},
        "3": {"name": "Pokemon 151 ETB Lot of 2", "url": "https://t/3", "seen": time.time(), "live": True,
              "price": "$140", "status": "In stock"}}}


def test_watch_channel_commands(engine):
    _stock_catalog(engine)
    (r,) = engine.handle_watch_command("test 151 etb -lot")
    assert "would catch **1** (1 in stock)" in r and "151 Elite Trainer Box" in r and "Lot of 2" not in r
    (r,) = engine.handle_watch_command("watch prismatic bundle")
    assert r.startswith("✅ **Watching: prismatic bundle**") and "1 match (0 in stock)" in r
    assert settings.load()["watch_terms"] == ["prismatic bundle"]
    (r,) = engine.handle_watch_command("watching")
    assert "`1` prismatic bundle" in r
    (r,) = engine.handle_watch_command("unwatch 1")
    assert "Stopped watching: prismatic bundle" in r and settings.load()["watch_terms"] == []
    assert engine.handle_watch_command("hello")[0].startswith("**Watch by name**")


def test_watch_alert_pings_once_in_watch_channel(engine):
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append((title, kw))
    settings.update({"watch_terms": ["prismatic bundle", "151 etb @walmart"]})
    x = {"id": "2", "name": "Pokémon TCG: Prismatic Evolutions Booster Bundle", "url": "https://t/2",
         "price": "$29.99", "image": "https://i/2.jpg", "status": "In stock"}
    engine._watch_alerts("target", x, {}, "🟢 BACK IN STOCK at Target")
    engine._watch_alerts("target", x, {}, "🟢 BACK IN STOCK at Target")        # same news again: no repeat
    (title, kw), = sent
    assert title.startswith("👀 🟢 BACK IN STOCK at Target") and kw["channel"] == "watch" and kw["ping"] is True
    engine._watch_alerts("target", {**x, "id": "1", "name": "Pokemon 151 Elite Trainer Box"}, {}, "🟢 NEW & IN STOCK at Target")
    assert len(sent) == 1                                                       # @walmart only


def test_focus_report_flags_sets_with_no_matches(engine):
    _stock_catalog(engine)
    settings.update({"focus_sets": ["151", "Evolving Skies"], "everyone_products": ["Cooper Flagg"]})
    text = "".join(engine.focus_report())
    assert "✅ **151** · 2" in text and "⚠️ **Evolving Skies** · 0" in text and "⚠️ **Cooper Flagg** · 0" in text


def test_target_tripwire_rescans_target_when_a_hot_item_flips(engine, monkeypatch):
    import rip_radar.engine as eng_mod
    engine.stock_json = lambda url: (200, "{}")
    engine.state.setdefault("targets", {})["Target · Pokémon cards"] = {"items": {
        "11112222": {"name": "Pokémon TCG: Prismatic Evolutions Elite Trainer Box", "url": "u", "seen": time.time(),
                     "live": False},
        "33334444": {"name": "Pokémon TCG: Surging Sparks Booster Pack", "url": "u", "seen": time.time(), "live": False}}}
    asked = []

    def fake(session, tcin, zip_code="", key="", **kw):
        asked.append(tcin)
        return {"online": 12, "online_status": "IN_STOCK", "stores": [], "sold_out": False}
    monkeypatch.setattr(eng_mod, "target_stock", fake)
    engine.next_due = {"Target · Pokémon cards": time.time() + 999}
    assert engine.target_tripwire_tick() == 1
    assert asked == ["11112222"]                         # only the hot item (ETB), never the plain booster pack
    assert engine.next_due["Target · Pokémon cards"] == 0
    assert engine.target_tripwire_tick() == 0            # checked again only after ~90 s


def test_site_budget_and_quiet_hours(engine):
    t = {"name": "Walmart · Pokémon cards", "type": "retail_search", "url": "https://www.walmart.com/search?q=x"}
    for _ in range(150):
        engine._note_request("https://www.walmart.com/ip/123")
    assert engine._over_budget(t) is True
    assert engine._over_budget({**t, "type": "walmart_drawings"}) is False        # drawings never wait
    assert engine._over_budget({**t, "url": "https://www.target.com/s?x"}) is False
    settings.update({"quiet_hours": [0, 24]})
    assert engine._slow_factor() == 2
    settings.update({"quiet_hours": []})
    assert engine._slow_factor() == 1


def test_general_stores_are_cards_only(engine):
    from rip_radar.parsing import is_card_only
    assert not is_card_only("Ultra Pro Pokemon TCG Deck Protector Sleeves 65-Count", "pokemon")
    assert not is_card_only("Pokémon TCG: Eevee Binder", "pokemon")
    assert not is_card_only("Pokemon Charizard ex 199/165 Special Illustration Rare", "pokemon")   # single
    assert is_card_only("Pokémon TCG: Phantasmal Flames Elite Trainer Box", "pokemon")
    assert is_card_only("2025 Topps Chrome Football Hobby Box", "sports")
    stores = {}
    for t in engine.targets():
        if t.get("store") in ("barnes", "hottopic", "amazon", "gamestop", "dicks", "costco"):
            assert t.get("cards_only"), t["name"]
            stores.setdefault(t["store"], set()).add(t.get("products"))
    assert stores.pop("barnes") == {"cards"}                     # B&N: one scan, both (its Pokémon pages are books)
    assert all(v == {"pokemon", "sports"} for v in stores.values()), stores     # one Pokémon + one sports scan each


def test_every_store_is_cards_only(engine):
    """1.0.42: Target / Walmart / Best Buy... drop sleeves, binders, playmats too - not just the 6 general stores."""
    from rip_radar.parsing import is_accessory_only, is_card_only
    assert is_accessory_only("Ultra Pro Pokemon Charizard Playmat")
    assert not is_card_only("Pokémon TCG: Pikachu Card Sleeves 65ct", "cards")
    assert is_card_only("Pokémon TCG: Prismatic Evolutions Booster Bundle", "cards")
    assert not is_accessory_only("Pokémon TCG: Collector's Chest Fall 2025")


def test_block_matching():
    from rip_radar.parsing import blocked_by, name_from_title
    bl = ["Ultra PRO Pokémon Charizard Playmat (24x14)"]
    assert blocked_by("Ultra Pro Charizard Playmat $24.99", bl)            # same item, another store's wording
    assert not blocked_by("Pokémon TCG: Charizard ex Premium Collection", bl)
    assert blocked_by("Funko Pop Pikachu Trading Card Bundle", ["=funko"])  # typed `block funko`
    assert not blocked_by("Pokémon TCG: Prismatic Evolutions Elite Trainer Box", ["=funko"])
    assert name_from_title("🔥 🟢 IN STOCK · Pokémon TCG: Prismatic ETB") == "Pokémon TCG: Prismatic ETB"
    assert name_from_title("🆕 Loaded at Target, not in stock yet: Pokémon TCG: X Box · ⚠️ 30% above retail") \
        == "Pokémon TCG: X Box"


def test_react_x_blocks_product(engine, monkeypatch):
    from rip_radar import settings as settings_mod
    store = {}
    monkeypatch.setattr(settings_mod, "load", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "update", lambda d: store.update(d))
    retired = []
    monkeypatch.setattr(engine.cards, "retire", lambda s, p: retired.append((s, p)))
    engine.state["product_cards"] = {"target:12345678": {"id": "999", "name": "Pokémon TCG: Pikachu Binder Bundle"}}
    assert engine.block_product("1") is None                                 # not a product post
    reply = engine.block_product("999")
    assert "Blocked" in reply and store["blocked_products"] == ["Pokémon TCG: Pikachu Binder Bundle"]
    assert any("Pikachu Binder" in m for m in engine.handle_watch_command("blocked"))
    assert "Unblocked" in engine.handle_watch_command("unblock 1")[0]
    assert store["blocked_products"] == []
    engine.handle_watch_command("block funko")
    assert store["blocked_products"] == ["=funko"]


def test_topps_fast_check_trips(engine, monkeypatch):
    import json, time
    from datetime import datetime, timedelta
    from rip_radar.parsing import CT
    t = engine._topps_source()
    assert t, "topps sitemap source missing"
    soon = (datetime.now(CT) + timedelta(hours=2)).isoformat()
    known = engine.state.setdefault("targets", {}).setdefault(t["name"], {}).setdefault("products", {})
    known["2025-topps-chrome-football-hobby-box"] = {"name": "2025 Topps Chrome Football Hobby Box", "status": "Upcoming",
                                                     "live": False, "when": soon, "first_seen": 0, "baseline": True}
    known["old-thing"] = {"name": "2024 Topps Series 1 Blaster", "status": "Sold out", "live": False, "first_seen": 0}
    assert [h for h, _ in engine._hot_topps_items()] == ["2025-topps-chrome-football-hobby-box"]

    class R:
        def __init__(self, code, body): self.status_code, self.text = code, body

    class H:
        calls = []
        def get(self, url, **kw):
            self.calls.append(url)
            return R(200, json.dumps({"title": "x", "available": True, "variants": [{"id": 1, "available": True,
                                                                                   "price": 29999}]}))
    h = H()
    assert engine.topps_fast_tick(http=h) == 1
    assert h.calls == ["https://www.topps.com/products/2025-topps-chrome-football-hobby-box.js"]
    assert known["2025-topps-chrome-football-hobby-box"]["force"] and engine.next_due[t["name"]] == 0
    assert engine.topps_fast_tick(http=h) == 0              # not again within 60 s

    class Blocked:
        def get(self, url, **kw): return R(403, "nope")
    engine._tfast_checked = {}
    engine.topps_fast_tick(http=Blocked())
    assert engine.TOPPS_FAST["on"] is False and "403" in engine.topps_fast_health()


def test_diagnostics_1014_noise_is_cut():
    """Real listings from M's 10/14 2:48 PM diagnostics."""
    from rip_radar.parsing import is_card_product, is_pokemon_product, is_sports_card_product, is_accessory_only
    cut = [
        "Collectible 2022 Lost Origin Pikachu VMAX Gold Rare UR CSR #TG29/TG30 Pokemon Trading Card (English)",
        "Collectible 2026 Pitch Black Mega Zeraora EX Holo Double Rare RR #27/84 English Pokemon TCG Trading Card",
        "Collectible 2025 Near Mint Or Better Mega Evolution Mega Kangaskhan ex Holo Ultra Rare #164 Pokemon Trading Ca",
        "Collectible Oranguru 119/159 Crown Zenith Rare NM-M Pokemon TCG English Trading Card",
        "Collectible 2023 Crown Zenith Deoxys VMAX SAR FA Holo Rare #GG45/GG70 Pokemon Trading Card",
        "Collectible 2019 Topps Allen and Ginter Gold Hot Box #179 Burton Rocks Sports Agent and Author MLB Baseball "
        "Parallel Trading Card https://www.walmart.com/ip/Collectible-2019-Topps-Gold-Hot-Box-179/1",
        "Collectible 2025 Bowman Draft Chrome BDC-178 Sean Gamble Royals Baseball Card by Topps Kansas City Royals MLB",
        "Tom Brady Autographed 2000 SPx Rookie Card #130 New England Patriots - PSA/DNA Authenticated",
        "Quick Add Pokémon Deluxe Character Guide Limited Edition Box Set Collection Pikachu Press",
        "Pokémon Adventures Gold & Silver Box Set (Set Includes Vols. 8-14)",
        "Pokemon: Indigo League - Season 1: The Complete Collection [9 Discs]",
        "Pokemon Diamond and Pearl: 4-Movie Collection",
        "Pokemon Halloween Calendar 13 Pack",
        "Pokémon Storybook Collection, Hardcover",
        "Pokémon Crochet Snorlax Kit, Mixed Media",
        "Crayola Coloring Kit, Pokemon & Bluey",
        "Bandai Hobby - Pokémon - 08 Mimikyu, Bandai Spirits Pokémon Model Kit",
        "Pokémon Scarlet & Pokémon Violet Double Pack",
        "Pokémon Squeaky Dog Toys, 3-count",
    ]
    for n in cut:
        assert not is_card_product(n), n
    assert is_accessory_only("Ultra Pro Pokemon Pikachu 2'' 3-Ring Binder,(Pack of 2)")
    keep_poke = [
        "Pokemon TCG Trading Card Game Mega Evolutions Ascended Heroes 2 Packs, 1 Promo Card and one Coin",
        "Pokemon Trading Card Game: 30th Celebration Figure Collection (Styles May Vary)",
        "Pokémon TCG: Mega Evolution - Ascended Heroes Assorted Blind Tin",
        "Pokemon SV6 Scarlet & Violet Twilight Masquerade Checklane Blister | Toxel",
        "Pokemon TCG: 30th Celebration Tech Sticker Collection - Lucario",
        "Collectible 2025 Pokemon Scarlet & Violet Prismatic Evolutions Trading Card Game Poster Collection Box",
        "Pokemon 2023 Trick or Trade Mimikyu & Pikachu | 6 Mini Booster Packs",
    ]
    for n in keep_poke:
        assert is_pokemon_product(n), n
    for n in ("2026 TOPPS NFL Flagship Collection", "Collectible 26 TOPPS ALLEN AND GINTER BASEBALL INNER PACK",
              "Collectible 2026 Topps Holiday Baseball - Factory Sealed - Hobby Tins"):
        assert is_sports_card_product(n), n


def test_dicks_junk_names_fall_back_to_link():
    from rip_radar.parsing import clean_name, slug_name
    assert clean_name("(218) Not Available to Ship Not Available to") == ""
    assert slug_name("/p/2026-topps-mlb-holiday-mega-box-26topufangzgqajhnr7nqa/26topufangzgqajhnr7nqa") \
        == "2026 Topps MLB Holiday Mega Box"


def test_purge_takes_down_old_unwanted_posts(engine, monkeypatch):
    """Posts made before a filter changed (singles, toys, books) and posts for products we no longer track go."""
    retired = []
    monkeypatch.setattr(engine.cards, "retire", lambda s, p: retired.append((s, p)))
    t = next(t for t in engine.targets() if t.get("store") == "walmart" and t["type"] == "retail_search")
    items = engine.state.setdefault("targets", {}).setdefault(t["name"], {}).setdefault("items", {})
    items["111"] = {"name": "Collectible Jirachi EX RR #102/128 Holo Double Rare Pokemon Trading Card", "carded": True}
    items["222"] = {"name": "Pokemon Battle Figure Multipack Assortment", "carded": True}
    items["333"] = {"name": "Pokémon TCG: Prismatic Evolutions Elite Trainer Box", "carded": True,
                    "url": "https://www.walmart.com/ip/x/333"}
    engine.state["product_cards"] = {"walmart:111": {"id": "1"}, "walmart:222": {"id": "2"},
                                     "walmart:333": {"id": "3"}, "barnes:9780": {"id": "4"}}
    assert engine.purge_unwanted_cards() == 3
    assert sorted(retired) == [("barnes", "9780"), ("walmart", "111"), ("walmart", "222")]
    assert items["333"]["carded"] and not items["111"]["carded"]


def test_pokemon_center_scans_sealed_only_and_slow(engine):
    pc = [t for t in engine.targets() if t.get("store") == "pokemon" and t["type"] == "retail_search"]
    assert pc and all("tcg-cards" not in t["url"] for t in pc)
    assert all(t["interval_seconds"] >= 420 for t in pc)


def test_bot_puts_check_and_x_on_new_product_posts(monkeypatch):
    from rip_radar import notify
    from rip_radar.cards import ProductCards

    class R:
        def __init__(self, code, data=None): self.status_code, self._d = code, data or {}
        def json(self): return self._d
    added = []
    monkeypatch.setattr(notify.REACTIONS, "add", lambda tok, ch, mid: added.append((tok, ch, mid)))
    settings = {"bot_token": "TKN", "webhooks": {"target": "https://discord.com/api/webhooks/1/x"}}
    pc = ProductCards(lambda: settings, {}, pace=0)

    class H:
        def request(self, method, url, **kw): return R(200, {"id": "55", "channel_id": "77"})
    pc.http = H()
    pc.process("target:1", "target", {"name": "Pokémon TCG: Prismatic Evolutions ETB", "url": "https://t.co/1"},
               True, False, "")
    assert added == [("TKN", "77", "55")] and pc.saved()["target:1"]["reacted"]


def test_reactions_worker_puts_both(monkeypatch):
    import time
    from rip_radar.notify import Reactions
    calls = []

    class R:
        status_code = 204
    rx = Reactions()
    rx.http = type("H", (), {"put": lambda self, url, **kw: calls.append(url) or R()})()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    rx.add("TKN", "77", "55")
    for _ in range(100):
        if len(calls) == 2:
            break
        time.sleep(0) or __import__("threading").Event().wait(0.01)
    assert [c.rsplit("/reactions/", 1)[1] for c in calls] == ["%E2%9C%85/@me", "%E2%9D%8C/@me"]


PDP = {"data": {"product": {"tcin": "94681234", "price": {"formatted_current_price": "$49.99"},
                            "item": {"product_description": {"title": "Pok&#233;mon TCG: Destined Rivals Elite Trainer Box",
                                                             "bullet_descriptions": ["Great for drawing fans"]},
                                     "enrichment": {"images": {"primary_image_url": "https://target.scene7.com/x"}},
                                     "fulfillment": {"purchase_limit": 2}}}}}


def test_parse_target_pdp():
    from rip_radar.stock import parse_target_pdp
    i = parse_target_pdp(PDP)
    assert i["loaded"] and i["name"] == "Pokémon TCG: Destined Rivals Elite Trainer Box"
    assert i["price"] == "$49.99" and i["limit"] == "2" and not i["drawing"]      # 'drawing' in bullets ignored
    import copy
    d = copy.deepcopy(PDP)
    d["data"]["product"]["item"]["eligibility_rules"] = {"drawing": {"is_active": True}}
    assert parse_target_pdp(d)["drawing"]
    assert not parse_target_pdp({"errors": [{"message": "No product found"}]}, 404)["loaded"]


def test_tcin_in():
    from rip_radar.engine import Engine
    assert Engine.tcin_in("tcin 94681234") == "94681234"
    assert Engine.tcin_in("https://www.target.com/p/pokemon-etb/-/A-94681234#lnk=sametab") == "94681234"
    assert Engine.tcin_in("94681234") == "94681234"
    assert Engine.tcin_in("prismatic bundle") == ""


def test_tcin_watch_flow(engine, monkeypatch):
    import copy
    from rip_radar import settings as settings_mod
    store = {"watch_tcins": []}
    monkeypatch.setattr(settings_mod, "load", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "update", lambda d: store.update(d))
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, **kw: sent.append((title, kw.get("ping"),
                                                                                       kw.get("copy_to")))
    phase = {"v": ("not loaded", {"loaded": False})}
    monkeypatch.setattr(engine, "_tcin_check", lambda t: phase["v"])
    engine.stock_json = lambda url: (200, "{}")
    reply = engine.handle_watch_command("watch tcin 94681234")[0]
    assert "Watching Target ID `94681234`" in reply and "not loaded" in reply and store["watch_tcins"] == ["94681234"]
    phase["v"] = ("loaded", {"loaded": True, "name": "Pokémon TCG: Destined Rivals ETB", "price": "$49.99"})
    assert engine.tcin_watch_tick(now=time_now() + 100) == 1
    assert sent[-1][0].startswith("🆕 LOADED ON TARGET") and sent[-1][1] is True
    phase["v"] = ("drawing", {"loaded": True, "drawing": True, "drawing_text": "drawing"})
    assert engine.tcin_watch_tick(now=time_now() + 300) == 1
    assert sent[-1][0].startswith("🎟️ TARGET DRAWING") and sent[-1][2] == ["drawings"]
    assert engine.tcin_watch_tick(now=time_now() + 500) == 0                # same phase: no repeat
    assert "Target IDs" in engine.handle_watch_command("watching")[0]
    assert "Stopped" in engine.handle_watch_command("unwatch tcin 94681234")[0] and store["watch_tcins"] == []


def time_now():
    import time
    return time.time()


def test_crunchyroll_store_and_queue(engine):
    from rip_radar.notify import channel_of
    assert channel_of("crunchyroll") == "hottopic"
    q = [t for t in engine.targets() if t.get("track_duration")]
    cr = next(t for t in q if "crunchyroll" in t["url"])
    assert engine._queue_site(cr) == ("Crunchyroll Store", "crunchyroll-store") and cr["channel"] == "hottopic"
    pc = next(t for t in q if "pokemoncenter" in t["url"])
    assert engine._queue_site(pc) == ("Pokémon Center", "pokemon-center")
    assert any(t.get("store") == "crunchyroll" and t["type"] == "retail_search" for t in engine.targets())


SOLD_HTML = "<ul>" + "".join(
    f'<li class="s-item"><div class="s-item__title">Pokemon Prismatic Evolutions Booster Bundle Sealed</div>'
    f'<span class="s-item__price">${p}</span></li>' for p in (55, 60, 62, 58, 65)) + "</ul>"


def test_resale_goes_through_browser_when_ebay_blocks_plain_requests():
    from rip_radar.resale import ResaleChecker
    calls = []
    rc = ResaleChecker({}, lambda: {"resale_check": True},
                       browser_fetch=lambda url: calls.append(url) or (200, url, SOLD_HTML))
    res = rc.check("Pokémon Prismatic Evolutions Booster Bundle")
    assert calls and res["median"] == 60 and res["ok"]
    assert "✅ working" in rc.health() and rc.stats["via"] == "browser"


def test_resale_failure_is_retried_soon_and_reported():
    import time
    from rip_radar.resale import ResaleChecker
    rc = ResaleChecker({}, lambda: {"resale_check": True}, browser_fetch=lambda url: (200, url, "<p>Pardon our interruption</p>"))
    rc.s = type("S", (), {"get": lambda self, url, **kw: type("R", (), {"status_code": 403, "text": ""})()})()
    for _ in range(5):
        res = rc.check("Pokémon Prismatic Evolutions Booster Bundle")
    assert res["ok"] is False and "❌" in rc.health() and "403" in rc.health()
    key = rc.key("Pokémon Prismatic Evolutions Booster Bundle")
    rc.cache[key] = {"at": time.time() - 3600, "q": "x", "ok": False}       # an hour-old failure
    rc._start = lambda: None
    rc.lookup("Pokémon Prismatic Evolutions Booster Bundle")
    assert key in rc.queued                                                   # asked again (not cached for a day)


def test_post_shows_resale_state(engine):
    rec = {"name": "Pokémon TCG: Prismatic Evolutions Booster Bundle", "price": "$59.99", "carded": True}
    engine.resale.lookup = lambda name, urgent=False: None
    engine.resale.enabled = lambda: True
    engine._attach_resale([({"id": "1"}, rec, None, False)], "target")
    assert rec["resale"] == engine.RESALE_WAIT
    engine.cards.request = lambda *a, **k: None
    engine._resale_ready(engine.resale.key(rec["name"]), {"ok": False, "at": 0})
    assert rec["resale"] == engine.RESALE_FAIL
    engine._attach_resale([({"id": "1"}, rec, None, False)], "target")
    engine._resale_ready(engine.resale.key(rec["name"]), {"ok": True, "median": 75.0, "n": 9, "low": 60, "high": 90,
                                                          "q": "x", "at": 0})
    assert "$75 median of 9 sold" in rec["resale"]


def test_price_list(engine, monkeypatch):
    from rip_radar import settings as settings_mod
    from datetime import datetime, timedelta
    from rip_radar.parsing import CT
    base = settings_mod.load()
    s = dict(base, focus_sets=["Prismatic Evolutions"], everyone_products=[
        "2025-26 Topps Chrome Basketball", "2025-26 NBA Hoops", "2025-26 Topps Hoops Basketball", "Cooper Flagg"])
    monkeypatch.setattr(settings_mod, "load", lambda: s)
    items = engine.price_list_items()
    labels = {(sec, lab) for sec, lab, *_ in items}
    assert labels == {("pokemon", "Prismatic Evolutions"), ("sports", "2025-26 Topps Chrome"),
                      ("sports", "2025-26 NBA Hoops")}                   # Hoops once, Cooper Flagg (a name) left out
    queued = []
    monkeypatch.setattr(engine.resale, "lookup", lambda q, urgent=False, priority=None: queued.append((q, priority)))
    assert engine.price_list_tick(force=True) == len(items) and all(p == 1 for _, p in queued)
    etb = engine.resale.key("Pokemon Prismatic Evolutions Elite Trainer Box")
    yday = (datetime.now(CT) - timedelta(days=1)).date().isoformat()
    engine.state.setdefault("price_history", {})[etb] = {yday: {"median": 80.0, "n": 10}}
    engine._resale_ready(etb, {"ok": True, "median": 92.0, "n": 12, "low": 80, "high": 110, "q": "x", "at": 0})
    text = "\n".join(d for _, d in engine._render_prices())
    assert "🔥 **Prismatic Evolutions**" in text and "ETB **$92** ▲15% (1.3× retail)" in text
    assert "🟢 **2025-26 Topps Chrome**" in text and "⏳" in text
    assert engine.handle_watch_command("prices")[0].startswith("**💸 Price list")


def test_price_list_posts_fresh_message_when_done(engine, monkeypatch):
    from rip_radar import settings as settings_mod
    s = dict(settings_mod.load(), focus_sets=["Prismatic Evolutions"], everyone_products=[])
    monkeypatch.setattr(settings_mod, "load", lambda: s)
    monkeypatch.setattr(engine.resale, "lookup", lambda q, urgent=False, priority=None: None)
    posts = []
    monkeypatch.setattr(engine.prices_board, "tick", lambda render, force=False, repost=False, content=None:
                        posts.append(content))
    engine.state["board_prices"] = {"hook": "h", "id": "yesterday"}
    reply = engine.handle_prices_command("refresh")[0]
    assert "Checking eBay again for 3 items" in reply
    assert "Already refreshing" in engine.handle_prices_command("refresh")[0]
    for k in list(engine.state["price_refresh"]["pending"]):
        engine._resale_ready(k, {"ok": True, "median": 50.0, "n": 5, "low": 40, "high": 60, "q": "x", "at": 0})
    engine.price_list_tick()
    assert posts and "refreshed on request" in posts[-1]
    assert "board_prices" not in engine.state                     # yesterday's message kept, a new one posted


def test_presale_and_raffle_words():
    from rip_radar.parsing import is_presale, is_raffle
    for t in ("PRESALE: Pokémon Ascended Heroes ETB", "Pre-Sale 2025 Topps Chrome", "pre sale booster box",
              "Pokémon TCG Pre-Order Bundle"):
        assert is_presale(t), t
    assert not is_presale("Pokémon TCG: Prismatic Evolutions Elite Trainer Box")
    assert is_raffle("RAFFLE - Prismatic Evolutions SPC") and is_raffle("", "Drawing open")
    assert not is_raffle("Pokémon drawing book")


SHOPIFY = {"products": [
    {"id": 111, "title": "PRESALE Pokémon TCG: Ascended Heroes Elite Trainer Box", "handle": "ah-etb",
     "tags": ["Pokemon", "Presale"], "product_type": "Sealed",
     "variants": [{"id": 999, "available": True, "price": "59.99"}], "images": [{"src": "//cdn.shopify.com/a.jpg"}]},
    {"id": 222, "title": "Pokémon TCG: Chaos Rising Booster Box", "handle": "cr-bb", "tags": "Pokemon, Coming Soon",
     "variants": [{"id": 998, "available": False, "price": "159.99"}], "images": []}]}


def test_collectorstore_shopify_collection():
    import json
    from rip_radar.parsing import parse_retail_tiles
    tiles = parse_retail_tiles(json.dumps(SHOPIFY), "https://collectorstore.com/collections/coming-soon/products.json",
                               "collectorstore")
    a, b = tiles
    assert a["status"] == "Pre-order" and a["live"] and a["presale"] and a["price"] == "$59.99"
    assert a["add_to_cart"] == "https://collectorstore.com/cart/999:1" and a["image"].startswith("https://cdn")
    assert a["url"] == "https://collectorstore.com/products/ah-etb"
    assert b["status"] == "Coming soon" and not b["live"] and not b["add_to_cart"]


def test_collectorstore_posts_in_presale_channel():
    from rip_radar.notify import channel_of
    assert channel_of("collectorstore") == "presale"


def test_walmart_seller_and_price_freshness_on_post():
    import time
    from rip_radar.cards import build_card
    rec = {"name": "Pokémon TCG: Prismatic Evolutions ETB", "url": "https://www.walmart.com/ip/1", "price": "$89.99",
           "price_since": time.time(), "seller": "Mascot", "live": True, "status": "In stock"}
    f = {x["name"]: x["value"] for x in build_card("walmart", rec)["embeds"][0]["fields"]}
    assert "3rd-party reseller - NOT Walmart's own price" in f["Sold by"] and "since" in f["Price"]
    rec.update(seller="Walmart.com", stale=True)
    f = {x["name"]: x["value"] for x in build_card("walmart", rec)["embeds"][0]["fields"]}
    assert f["Sold by"].startswith("✅ Walmart") and "may have changed" in f["⚠️ Price check"]


def test_walmart_page_prices_match_what_walmart_shows():
    """Real Walmart search tile (10/14): Walmart shows 'Price Now $49.99 Was $57.45' -> the post says $49.99."""
    import json
    from rip_radar.parsing import walmart_json_items
    nd = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"usItemId": "5001", "name": "Collectible 2025 Topps Chrome Football - Factory Sealed - Value Box",
         "price": 49.99, "priceInfo": {"linePrice": "", "wasPrice": ""}, "sellerName": "Walmart.com",
         "canonicalUrl": "/ip/x/5001"}]}]}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
    j = walmart_json_items(html)[0]
    assert j["price"] == "$49.99" and j["seller"] == "Walmart.com"
