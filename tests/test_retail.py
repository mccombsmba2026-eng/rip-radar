from rip_radar.parsing import cart_links, is_card_product, is_sports_card_product, parse_retail_tiles
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)


def tile(href, name, price, button, img="https://img.example.com/p.jpg"):
    return (f'<div class="tile"><a href="{href}"><img src="{img}" alt="{name}"></a>'
            f'<a href="{href}">{name}</a><span>{price}</span><button>{button}</button></div>')


PAD = "x" * 21000


def test_sports_filter():
    yes = ["2026 Topps Series 2 Baseball Hanger Box", "2025 Panini Prizm Football Blaster Box",
           "2026 Bowman Chrome Baseball Mega Box", "2025-26 Topps Chrome Basketball Value Box",
           "Panini Donruss Football Trading Card Fat Pack"]
    no = ["Ultra Pro 9-Pocket Binder", "BCW Top Loaders 3x4 (25 ct)", "Topps Baseball Card Sleeves",
          "Wilson NFL Football", "Rawlings Baseball Glove", "PSA 10 Graded Card Topps Chrome"]
    assert [x for x in yes if not is_sports_card_product(x)] == []
    assert [x for x in no if is_card_product(x)] == []


def test_target_tiles():
    html = (tile("/p/pokemon-tcg-delta-reign-elite-trainer-box/-/A-94300071", "Pokémon TCG: Delta Reign Elite Trainer Box",
                 "$49.99", "Add to cart")
            + tile("/p/pokemon-tcg-booster-bundle/-/A-94300072?preselect=1", "Pokémon TCG: 30th Booster Bundle",
                   "$26.99", "Out of stock")
            + tile("/p/pikachu-hat/-/A-11111111", "Pikachu Hat", "$19.99", "Add to cart") + PAD)
    t = {x["id"]: x for x in parse_retail_tiles(html, "https://www.target.com/s?searchTerm=pokemon", "target")}
    etb = t["94300071"]
    assert etb["live"] and etb["status"] == "In stock" and etb["price"] == "$49.99"
    assert etb["url"] == "https://www.target.com/p/pokemon-tcg-delta-reign-elite-trainer-box/-/A-94300071"
    assert etb["image"] == "https://img.example.com/p.jpg" and etb["add_to_cart"] == ""
    assert not t["94300072"]["live"] and t["94300072"]["status"] == "Out of stock"


def test_walmart_amazon_bestbuy_links_and_drawing():
    w = parse_retail_tiles(tile("/ip/Pokemon-TCG-30th-ETB/20640569221", "Pokémon TCG: 30th Celebration ETB",
                                "$69.97", "Enter drawing") + PAD, "https://www.walmart.com/search", "walmart")[0]
    assert w["id"] == "20640569221" and w["live"] and w["status"].startswith("Drawing")
    assert w["add_to_cart"] == "https://affil.walmart.com/cart/addToCart?items=20640569221"
    assert w["buy_now"] == "https://affil.walmart.com/cart/buynow?items=20640569221"
    a = parse_retail_tiles(tile("/Pokemon-Elite-Trainer/dp/B0DXYZ1234/ref=sr_1_1", "Pokémon TCG Elite Trainer Box",
                                "$49.99", "") + PAD, "https://www.amazon.com/s", "amazon", live_if_price=True)[0]
    assert a["id"] == "B0DXYZ1234" and a["live"]
    assert a["add_to_cart"] == "https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0DXYZ1234&Quantity.1=1"
    b = parse_retail_tiles(tile("/product/pokemon-tcg-etb/JJ8VPZ2K/sku/6612345", "Pokémon TCG ETB", "$49.99",
                                "Sold Out") + PAD, "https://www.bestbuy.com/site/searchpage.jsp", "bestbuy")[0]
    assert b["id"] == "6612345" and not b["live"] and cart_links("bestbuy", "6612345")[0].endswith("/6612345/cart")


def test_retail_watcher_pings_only_when_in_stock(engine):
    t = {"name": "Target · Pokémon cards", "type": "retail_search", "store": "target", "products": "pokemon",
         "url": "https://www.target.com/s?searchTerm=pokemon"}
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append((level, title, kw))
    etb = ("/p/etb/-/A-94300100", "Pokémon TCG: Delta Reign Elite Trainer Box", "$49.99")
    bundle = ("/p/bundle/-/A-94300200", "Pokémon TCG: 30th Booster Bundle", "$26.99")
    st = {}
    engine.fetcher = FakeFetch(tile(*etb, "Add to cart") + tile(*bundle, "Out of stock") + PAD, t["url"])
    assert engine.check_retail_search(t, st, True)[0].startswith("ok (2 card products · 1 in stock")
    assert sent == []                                              # first run: quiet
    new = ("/p/upc/-/A-94300300", "Pokémon TCG: Charizard Ultra-Premium Collection", "$119.99")
    engine.fetcher = FakeFetch(tile(*etb, "Add to cart") + tile(*bundle, "Add to cart")
                               + tile(*new, "Add to cart") + tile("/p/hat/-/A-94300400", "Pikachu Hat", "$20", "Add to cart")
                               + PAD, t["url"])
    engine.check_retail_search(t, st, False)
    titles = [s[1] for s in sent]
    assert titles == ["🟢 BACK IN STOCK at Target: Pokémon TCG: 30th Booster Bundle",
                      "🟢 NEW & IN STOCK at Target: Pokémon TCG: Charizard Ultra-Premium Collection"]
    kw = sent[1][2]
    assert kw["image"] == "https://img.example.com/p.jpg"
    assert ("Product page", "https://www.target.com/p/upc/-/A-94300300") in kw["links"]
    sent.clear()
    engine.check_retail_search(t, st, False)                       # nothing changed -> no repeat pings
    assert sent == []


def test_retail_watcher_checks_product_pages_when_tiles_hide_stock(engine):
    t = {"name": "Dick's · sports cards", "type": "retail_search", "store": "dicks", "products": "sports",
         "url": "https://www.dickssportinggoods.com/search/SearchDisplay?searchTerm=trading+cards", "verify_pages": 1}
    sent = []
    engine.notify.send = lambda level, title, url="", fields=None, desc="", **kw: sent.append(title)
    search = ('<div><a href="/p/topps-2026-series-2-hanger-26tpps2hngr/26tpps2hngr">'
              '<img src="https://dks/i.jpg" alt="Topps 2026 Series 2 Baseball Hanger Box"></a><span>$24.99</span></div>' + PAD)

    class Fetch:
        def __init__(self, product_page):
            self.product_page = product_page

        def get(self, url, browser=False):
            return 200, url, (search if "SearchDisplay" in url else self.product_page)

    st = {}
    engine.fetcher = Fetch("<button>Out of Stock</button>" + PAD)
    engine.check_retail_search(t, st, True)
    engine.fetcher = Fetch("<button>Add to Cart</button>" + PAD)
    engine.check_retail_search(t, st, False)
    assert sent == ["🟢 BACK IN STOCK at Dick's: Topps 2026 Series 2 Baseball Hanger Box"]
