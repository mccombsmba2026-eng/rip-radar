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
            "Ace Hardware · Pokémon cards", "Barnes & Noble · Pokémon cards"} <= names


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
