from datetime import datetime

from rip_radar.parsing import CT
from rip_radar.releases import (article_anchor, calendar_grid, dated_products, eql_launch_links, month_article_urls,
                                parse_eql_launch, similar)

NOW = datetime(2026, 10, 1, 9, tzinfo=CT)

ARTICLE_A = """<html><body><nav>Home News</nav><article>
<h1>Check Out Every Pokémon TCG Product Release in October 2026</h1>
<p>Here's everything coming to stores this month.</p>
<h2>October 2</h2>
<ul><li>Pokémon TCG: 30th Celebration Mini Tin</li><li>Pokémon TCG: 30th Celebration Booster Bundle</li>
<li>Pokémon TCG: 30th Celebration Binder Collection</li></ul>
<h2>October 16</h2>
<h3>Pokémon TCG: Mega Charizard X ex Premium Collection</h3>
<p>Includes 6 booster packs and a foil promo card.</p>
</article><footer>October 30 Pokémon Plush</footer></body></html>"""

ARTICLE_B = """<div><h2>Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box</h2>
<p>Release date: November 6, 2026</p><p>Contains 9 booster packs.</p>
<h2>Pokémon TCG: Mega Evolution—Delta Reign Booster Display Box - Available November 6</h2>
<h2>Pokémon TCG: Delta Reign Plush Pikachu - November 6</h2></div>"""

PRESS = """<table><tr><th>Product</th><th>Release Date</th></tr>
<tr><td><a href="/en/Items/ViewItem/Delta-Reign-ETB">Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box</a></td>
<td>11/6/2026</td></tr>
<tr><td><a href="/en/Items/ViewItem/30th-UPC">Pokémon TCG: 30th Celebration Ultra-Premium Collection</a></td>
<td>Nov 20, 2026</td></tr></table>"""


def names(items):
    return {(i["name"], i["when"].day) for i in items}


def test_article_date_headings_then_products():
    got = names(dated_products(ARTICLE_A, "https://pokemon.com/a", NOW))
    assert ("Pokémon TCG: 30th Celebration Mini Tin", 2) in got
    assert ("Pokémon TCG: 30th Celebration Booster Bundle", 2) in got
    assert ("Pokémon TCG: Mega Charizard X ex Premium Collection", 16) in got
    assert not any("Plush" in n for n, _ in got)


def test_article_product_then_date_line():
    got = names(dated_products(ARTICLE_B, "https://pokemon.com/b", NOW))
    assert ("Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box", 6) in got
    assert ("Pokémon TCG: Mega Evolution—Delta Reign Booster Display Box", 6) in got
    assert not any("Plush" in n for n, _ in got)


def test_press_table():
    got = dated_products(PRESS, "https://press.pokemon.com/en/Items/Schedule/x", NOW)
    assert {(g["name"], g["when"].month, g["when"].day) for g in got} == {
        ("Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box", 11, 6),
        ("Pokémon TCG: 30th Celebration Ultra-Premium Collection", 11, 20)}
    etb = next(g for g in got if "Elite" in g["name"])
    assert etb["url"] == "https://press.pokemon.com/en/Items/ViewItem/Delta-Reign-ETB"


def test_article_helpers():
    assert article_anchor("https://www.pokemon.com/us/news/check-out-every-pokemon-tcg-product-release-in-october-2026") \
        == datetime(2026, 10, 1, tzinfo=CT)
    urls = month_article_urls(datetime(2026, 12, 5, tzinfo=CT))
    assert urls[0][0].endswith("release-in-december-2026") and urls[1][0].endswith("release-in-january-2027")


def test_calendar_grid():
    html = """<h2>October 2026</h2><div class="grid"><div>1</div><div>14 Target 30th Celebration Elite Trainer Box</div>
    <div>16 Walmart Draw 30th Celebration Booster Bundle Pokemon Center Delta Reign ETB</div><div>17</div></div>"""
    got = {(g["name"], g["when"].day, g["store"]) for g in calendar_grid(html, NOW)}
    assert ("30th Celebration Elite Trainer Box", 14, "target") in got
    assert ("30th Celebration Booster Bundle", 16, "walmart") in got
    assert ("Delta Reign ETB", 16, "pokemon") in got


def test_eql_links_and_page():
    lst = ('<a href="/en-US/launch/2026-topps-chrome-spongebob-hobby-box">SpongeBob</a>'
           '<a href="/en-US/launch/2026-topps-chrome-spongebob-hobby-box/login">x</a><a href="/en-US/">all</a>'
           '<script>{"u":"https://launches.topps.com/en-US/launch/2026-topps-tribute-baseball-hobby-box"}</script>')
    links = eql_launch_links(lst)
    assert set(links) == {"https://launches.topps.com/en-US/launch/2026-topps-chrome-spongebob-hobby-box",
                          "https://launches.topps.com/en-US/launch/2026-topps-tribute-baseball-hobby-box"}
    page = ('<html><head><meta property="og:image" content="https://img/x.jpg"><title>2026 Topps Chrome SpongeBob - '
            'Hobby Box | Topps</title></head><body><h1>2026 Topps Chrome SpongeBob - Hobby Box</h1>'
            '<p>US$249.99</p><p>Maximum of 2 per customer</p><p>Free to enter</p>'
            '<script>{"openingTime":"2030-10-20T16:00:00Z","closingTime":"2030-10-22T16:00:00Z"}</script></body></html>')
    p = parse_eql_launch(page, "https://launches.topps.com/en-US/launch/x")
    assert p["name"] == "2026 Topps Chrome SpongeBob - Hobby Box" and p["price"] == "$249.99" and p["limit"] == "2"
    assert p["closes"].day == 22 and p["opens"].day == 20 and p["status"] == "Coming soon"
    assert p["image"] == "https://img/x.jpg"


def test_similar():
    assert similar("Pokémon TCG: Mega Evolution—Delta Reign Elite Trainer Box", "Mega Evolution Delta Reign Elite Trainer Box")
    assert not similar("Delta Reign Elite Trainer Box", "Delta Reign Booster Bundle")


PRESS_REAL = """<table class="tableSchedule"><tbody>
<tr class=""><td class="td-prod"><a class="prod-name" href="/en/products/Pokemon-TCG-Mega-EvolutionDelta-Reign">Pokémon TCG:
 Mega Evolution—Delta Reign</a></td><td class="td-plat"> </td><td class="td-date">
November 6, 2026
</td></tr>
<tr class=""><td class="td-prod"><a class="prod-name" href="/en/products/Pokemon-TCG-30th-Celebration">Pokémon TCG: 30th
 Celebration</a></td><td class="td-plat"> </td><td class="td-date">September 16, 2026</td></tr></tbody></table>"""


def test_press_schedule_sets():
    from rip_radar.releases import press_schedule
    got = press_schedule(PRESS_REAL, "https://press.pokemon.com/en/Items/Schedule/Pokemon-Trading-Card-Game")
    assert [(g["name"], g["when"].month, g["when"].day, g["set"]) for g in got] == [
        ("Pokémon TCG: Mega Evolution—Delta Reign", 11, 6, True), ("Pokémon TCG: 30th Celebration", 9, 16, True)]
    assert got[0]["url"] == "https://press.pokemon.com/en/products/Pokemon-TCG-Mega-EvolutionDelta-Reign"


def test_article_skips_related_story_links():
    html = ("<h2>October 2</h2><ul><li>Pokémon TCG: 30th Celebration Mini Tin</li></ul>"
            "<a href='/x'>Highlighting 10 Cards from the Pokémon TCG: 30th Celebration Classic Collection</a><p>October 14</p>"
            "<a href='/y'>Sneak a Peek at Cards from the Mega Evolution—Delta Reign Expansion</a><p>October 5</p>")
    names = {i["name"] for i in dated_products(html, "https://pokemon.com/a", NOW)}
    assert names == {"Pokémon TCG: 30th Celebration Mini Tin"}
