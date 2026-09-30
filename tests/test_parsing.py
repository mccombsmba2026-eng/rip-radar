from datetime import datetime

from rip_radar.parsing import (CT, categorize, extract_when, fmt_when, looks_blocked, parse_card_date,
                               parse_topps_calendar, sport_of)

NOW = datetime(2026, 9, 29, 13, 0, tzinfo=CT)


def w(text):
    r = extract_when(text, NOW)
    return (fmt_when(r[0], r[1]) + r[2]) if r else None


def test_extract_when_converts_zones_to_central():
    assert w("Walmart's Pokémon 30th Celebration drawing opens September 30 at 2 PM PT") == "Wed Sep 30, 4:00 PM"
    assert w("Dick's entry starts Oct. 2 at 9am ET for Delta Reign") == "Fri Oct 2, 8:00 AM"
    assert w("Best Buy invites 10/7 at 11 a.m. CT") == "Wed Oct 7, 11:00 AM"


def test_extract_when_date_only_and_noise():
    assert w("Topps Flagship Basketball preorders open October 6") == "Tue Oct 6 (time TBA)"
    assert w("Pokemon 30th Celebration ETB restock") is None      # '30th' is not a date
    assert w("drawing ended Sept 20") is None                      # in the past


def test_categorize_and_sport():
    assert categorize("Pokémon 30th ETB") == "Pokémon"
    assert sport_of("2026 Bowman Football") == "Football"
    assert sport_of("2026 Bowman Chrome® Baseball Sapphire Edition") == "Baseball"
    assert sport_of("2026-27 Topps Flagship Basketball") == "Basketball"
    assert sport_of("Topps Stadium Club Chrome® UEFA Champions League 2025/26") == ""
    assert sport_of("2026 Topps Chrome® Formula 1") == ""
    assert sport_of("2026 Topps Chrome® Tennis") == ""


def test_card_date_utc_to_ct():
    d, has_time, rest = parse_card_date("Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football", NOW)
    assert has_time and fmt_when(d) == "Wed Sep 30, 11:00 AM"
    assert rest.strip() == "2026 Bowman Football"
    d, has_time, _ = parse_card_date("Wednesday, Nov 11 2026 Topps Museum Collection Baseball", NOW)
    assert not has_time and d.month == 11 and d.day == 11


def test_looks_blocked():
    assert looks_blocked(403, "")
    assert looks_blocked(200, "<html>Robot or human?</html>")
    assert not looks_blocked(200, "<html>" + "x" * 30000 + "</html>")


def card(slug, datetext, name, button):
    return (f'<div class="card"><a href="/pages/{slug}"><img src="x.jpg" alt="{name}"></a>'
            f'<div class="info"><a href="https://www.topps.com/pages/{slug}"><span>{datetext}</span> '
            f'<h3>{name}</h3></a><button>{button}</button></div></div>')


SOON = [("topps-chrome-tennis", "Wednesday, Sep 30 at 3:00 PM UTC", "2026 Topps Chrome® Tennis", "Notify me"),
        ("bowman-football", "Wednesday, Sep 30 at 4:00 PM UTC", "2026 Bowman Football", "Notify me"),
        ("topps-inception-football", "Thursday, Oct 1", "2026 Topps Inception Football", "Pre-order"),
        ("topps-flagship-basketball", "Tuesday, Oct 6", "2026-27 Topps Flagship Basketball", "Pre-order"),
        ("topps-museum-collection-baseball", "Wednesday, Nov 11", "2026 Topps Museum Collection Baseball",
         "Notify me")]
AVAIL = [("topps-chrome-black-basketball", "Thursday, Aug 27", "2025-26 Topps Chrome® Black Basketball", "Buy now")]


def page(soon=SOON, avail=AVAIL):
    nav = '<nav><a href="/pages/about">About</a><a href="/pages/topps-now">Topps NOW</a></nav>'
    return ("<html><body>" + nav + "<h2>Dropping soon</h2>" + "".join(card(*c) for c in soon)
            + "<h2>Available now</h2>" + "".join(card(*c) for c in avail) + "x" * 20000 + "</body></html>")


def test_topps_calendar_parse():
    ps = {p["slug"]: p for p in parse_topps_calendar(page(), now=NOW)}
    assert "about" not in ps and "topps-now" not in ps            # nav links skipped
    bf = ps["bowman-football"]
    assert bf["sport"] == "Football" and bf["has_time"] and bf["status"] == "Upcoming"
    assert bf["name"] == "2026 Bowman Football" and bf["section"] == "Dropping soon"
    assert ps["topps-inception-football"]["status"] == "Pre-order"
    assert ps["topps-chrome-tennis"]["sport"] == ""
    black = ps["topps-chrome-black-basketball"]
    assert black["section"] == "Available now" and black["status"] == "On sale"


def test_topps_calendar_without_img_alt():
    html = ('<h2>Dropping soon</h2><a href="/pages/topps-midnight-football">Monday, Oct 12 '
            '2026 Topps Midnight Football Pre-order</a>' + "x" * 21000)
    (p,) = parse_topps_calendar(html, now=NOW)
    assert p["name"] == "2026 Topps Midnight Football" and p["sport"] == "Football"


# ---------------------------------------------------------------- 1.0.3: sturdier calendar, formats, cards-only
from rip_radar.parsing import is_tcg_product, parse_topps_product_page


def test_calendar_keeps_card_when_date_becomes_countdown():
    html = ('<h2>Dropping soon</h2><div><a href="/pages/bowman-football"><img alt="2026 Bowman Football"></a>'
            '<a href="/pages/bowman-football">Dropping in 00:34:12 2026 Bowman Football</a><button>Notify me</button></div>'
            + card(*SOON[2]) + "x" * 21000)
    ps = {p["slug"]: p for p in parse_topps_calendar(html, now=NOW)}
    assert ps["bowman-football"]["when"] is None and ps["bowman-football"]["sport"] == "Football"
    assert ps["topps-inception-football"]["when"]


def test_calendar_today_and_local_times():
    d, has_time, rest = parse_card_date("Today at 11:00 AM 2026 Bowman Football", NOW, default_tz=CT)
    assert has_time and fmt_when(d) == "Tue Sep 29, 11:00 AM" and rest.strip() == "2026 Bowman Football"
    d, _, _ = parse_card_date("Wednesday, Sep 30 at 11:00 AM 2026 Bowman Football", NOW, default_tz=CT)
    assert fmt_when(d) == "Wed Sep 30, 11:00 AM"          # browser-localised time stays Central
    d, _, _ = parse_card_date("Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football", NOW, default_tz=CT)
    assert fmt_when(d) == "Wed Sep 30, 11:00 AM"          # explicit zone always wins


def test_calendar_reads_machine_timestamp():
    html = ('<h2>Dropping soon</h2><div><a href="/pages/bowman-football">2026 Bowman Football</a>'
            '<time datetime="2026-09-30T16:00:00Z">Live soon</time></div>' + "x" * 21000)
    (p,) = parse_topps_calendar(html, now=NOW)
    assert p["has_time"] and p["when"].startswith("2026-09-30T11:00")


def test_product_page_formats():
    html = ('<div class="grid">'
            '<div class="card"><a href="/products/2026-bowman-football-hobby-box"><img alt="2026 Bowman Football Hobby Box"></a>'
            '<span>$129.99</span><button>Add to cart</button></div>'
            '<div class="card"><a href="/products/2026-bowman-football-mega-box">2026 Bowman Football Mega Box</a>'
            '<span>$59.99</span><button>Sold out</button></div>'
            '<div class="card"><a href="/products/2026-bowman-football-blaster-box?variant=1"></a>'
            '<span>$29.99</span><button>Notify me</button></div></div>'
            '<p>Available September 30 at 12pm ET</p>')
    formats, when = parse_topps_product_page(html, "https://www.topps.com/pages/bowman-football")
    f = {x["handle"]: x for x in formats}
    assert f["2026-bowman-football-hobby-box"] == {"handle": "2026-bowman-football-hobby-box",
        "name": "2026 Bowman Football Hobby Box", "url": "https://www.topps.com/products/2026-bowman-football-hobby-box",
        "price": "$129.99", "status": "On sale"}
    assert f["2026-bowman-football-mega-box"]["status"] == "Sold out"
    assert f["2026-bowman-football-blaster-box"]["name"] == "2026 Bowman Football Blaster Box"   # from the link
    assert when and fmt_when(when[0]) == "Wed Sep 30, 11:00 AM"


def test_tcg_filter():
    yes = ["Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box",
           "Pokémon TCG: 30th Celebration Booster Bundle (6 Packs)",
           "Pokémon TCG: Charizard ex Ultra-Premium Collection",
           "Pokémon TCG: 30th Celebration Pin Collection",
           "Scarlet & Violet—Surging Sparks Booster Display Box (36 Packs)",
           "Pokémon TCG: Mega Lucario ex Battle Deck",
           "/product/10-10372-109/pokemon-tcg-mega-evolution-pitch-black-3-booster-blister",
           "Pokémon TCG: 30th Celebration Poster Collection"]
    no = ["Pikachu Pokémon Center 30th Celebration Hat", "Pokémon Center 30th Celebration Lanyard",
          "Pikachu Sitting Cuties Plush", "Eevee Pin", "Charizard Card Sleeves (65 Sleeves)",
          "Pokémon TCG: Pikachu Playmat", "Umbreon Ceramic Mug", "Pokémon Center 30th Celebration Hoodie",
          "Gengar Figure", "Pokémon TCG: Eevee Binder"]
    assert [x for x in yes if not is_tcg_product(x)] == []
    assert [x for x in no if is_tcg_product(x)] == []
