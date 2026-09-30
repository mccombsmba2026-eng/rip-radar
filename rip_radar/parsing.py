"""Pure parsing helpers: dates/times, Topps calendar cards, sport/category detection.
No network, no files - everything here is unit-tested in tests/test_parsing.py."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

CT = ZoneInfo("America/Chicago")

BLOCK_MARKERS = ["robot or human", "access denied", "incapsula", "px-captcha",
                 "verify you are human", "are you a robot", "request unsuccessful",
                 "captcha", "pardon our interruption", "just a moment", "attention required",
                 "checking your browser", "cf-chl"]
# pages a real browser gets past on its own if we wait a few seconds
CHALLENGE_MARKERS = ["just a moment", "checking your browser", "cf-chl", "please wait while we verify",
                     "pardon our interruption"]


def is_challenge(html):
    head = (html or "")[:8000].lower()
    return len(html or "") < 60000 and any(m in head for m in CHALLENGE_MARKERS)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
TZS = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
       "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
       "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
       "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles"}
RE_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
    r"(?:,?\s*(20\d\d))?", re.I)
RE_SLASH = re.compile(r"\b(1[0-2]|0?[1-9])/(3[01]|[12]\d|0?[1-9])(?:/(20\d\d|\d\d))?\b")
RE_TIME = re.compile(
    r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s?m\.?"
    r"(?:\s*\(?\s*(ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT)\b)?", re.I)
NOISE = re.compile(r"\b(30th|25th)\s+(celebration|anniversary)\b", re.I)


def looks_blocked(status, text):
    if status in (401, 403, 429, 503):
        return True
    head = (text or "")[:6000].lower()
    return len(text or "") < 20000 and any(m in head for m in BLOCK_MARKERS)


def fmt_when(d, has_time=True):
    """'Wed Sep 30, 11:00 AM' - cross-platform (Windows strftime has no %-d)."""
    base = f"{d:%a %b} {d.day}"
    if not has_time:
        return base + " (time TBA)"
    return f"{base}, {d.hour % 12 or 12}:{d:%M} {'PM' if d.hour >= 12 else 'AM'}"


def gcal_link(title, start, details, all_day=False):
    if all_day:
        s = start.strftime("%Y%m%d")
        e = (start + timedelta(days=1)).strftime("%Y%m%d")
    else:
        su = start.astimezone(timezone.utc)
        s = su.strftime("%Y%m%dT%H%M%SZ")
        e = (su + timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}&dates={s}/{e}&details={quote(details[:900])}")


def extract_when(text, now=None):
    """First plausible upcoming date (+time if stated) in free text.
    Returns (datetime in CT, has_time, tz_note) or None."""
    return next(iter_when(text, now), None)


def best_time_for(text, day=None, now=None):
    """The announced time on a page: first date that states a time (on `day` if given), else None."""
    for w in iter_when(text, now):
        if w[1] and (day is None or w[0].date() == day):
            return w
    return None


def iter_when(text, now=None):
    now = now or datetime.now(CT)
    clean = NOISE.sub(" ", text or "")
    for rx in (RE_MONTH_DAY, RE_SLASH):
        for m in rx.finditer(clean):
            try:
                if rx is RE_MONTH_DAY:
                    month = MONTHS[m.group(1)[:3].lower()]
                    day, yr = int(m.group(2)), m.group(3)
                else:
                    month, day, yr = int(m.group(1)), int(m.group(2)), m.group(3)
                year = int(yr) if yr else now.year
                if year < 100:
                    year += 2000
                d = datetime(year, month, day)
            except (ValueError, KeyError):
                continue
            if not yr and d.date() < (now - timedelta(days=7)).date():
                d = d.replace(year=year + 1)
            t = RE_TIME.search(clean[m.end(): m.end() + 45])
            has_time, tz_note = False, ""
            if t:
                h = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "p" else 0)
                tzname = (t.group(4) or "").upper()
                tz = ZoneInfo(TZS.get(tzname, "America/Chicago"))
                if not tzname:
                    tz_note = " (time zone not stated, assumed CT)"
                local = d.replace(hour=h, minute=int(t.group(2) or 0), tzinfo=tz).astimezone(CT)
                has_time = True
            else:
                local = d.replace(hour=9, tzinfo=CT)
            if now - timedelta(hours=12) <= local <= now + timedelta(days=60):
                yield local, has_time, tz_note


def categorize(text, default=""):
    t = " " + (text or "").lower()
    if "pokemon" in t or "pokémon" in t:
        return "Pokémon"
    if "basketball" in t or " nba" in t:
        return "Basketball"
    if "football" in t or " nfl" in t:
        return "Football"
    if "baseball" in t or " mlb" in t or "bowman" in t:
        return "Baseball"
    return default




# ---------------------------------------------------------------- Topps release calendar
SPORT_WORDS = {"Baseball": ["baseball", " mlb", "bowman chrome", "bowman draft", "bowman sapphire"],
               "Basketball": ["basketball", " nba", "hoops"],
               "Football": ["football", " nfl"]}
NOT_OUR_SPORTS = ("soccer", "uefa", " mls", "premier league", "formula", " f1", "tennis", "wwe", "ufc",
                  "star wars", "marvel", "disney", "golf", "hockey", " nhl", "mars attacks", "pokemon")
_DAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)"
_TIME = r"(?:\s+at\s+(\d{1,2})(?::(\d\d))?\s*([ap])\.?m\.?\s*(UTC|GMT|ET|EST|EDT|CT|CST|CDT|PT|PST|PDT)?)?"
RE_CARD_DATE = re.compile(
    r"^\s*" + _DAY + r"?"
    # a year only counts after a comma ("Oct 6, 2026"), so "Oct 6 2026-27 Topps..." keeps the product's year
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:,\s*(20\d\d)(?![-\d]))?"
    + _TIME, re.I)
RE_RELATIVE_DATE = re.compile(r"^\s*(today|tomorrow|tonight)" + _TIME, re.I)
RE_ISO = re.compile(r"^20\d\d-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?$")
STATUS_WORDS = [("sold out", "Sold out"), ("pre-order", "Pre-order"), ("preorder", "Pre-order"),
                ("enter drawing", "Drawing open"), ("enter the drawing", "Drawing open"),
                ("enter lottery", "Drawing open"), ("enter the lottery", "Drawing open"),
                ("enter raffle", "Drawing open"), ("enter now", "Drawing open"),
                ("buy now", "On sale"), ("shop now", "On sale"), ("add to cart", "On sale"),
                ("available now", "On sale"), ("live now", "On sale"), ("on sale now", "On sale"),
                ("notify me", "Upcoming"), ("get notified", "Upcoming"), ("coming soon", "Upcoming")]
LIVE_STATUSES = {"Pre-order", "Drawing open", "On sale"}
BUTTON_NOISE = re.compile(r"\b(notify me|get notified|pre-?order( now)?|buy now|shop now|add to cart|sold out|"
                          r"available now|live now|on sale now|coming soon|enter( the)? (drawing|lottery|raffle)|"
                          r"enter now)\b", re.I)
PRODUCT_LINK = re.compile(r"/(pages|products)/", re.I)
TOPPS_PRODUCT_LINK = re.compile(r"/products/([a-z0-9][a-z0-9-]*)", re.I)
RE_PRICE = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)(?![A-Za-z0-9])")
# Walmart tiles read "$7994current price $79.94": the labelled price wins
RE_CURRENT_PRICE = re.compile(r"(?:current price|now|sale price|price)\s*:?\s*\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)", re.I)


def sport_of(name):
    t = " " + (name or "").lower()
    if any(w in t for w in NOT_OUR_SPORTS):
        return ""
    for sport in ("Basketball", "Football", "Baseball"):  # order matters: "Bowman Football" is football
        if any(w in t for w in SPORT_WORDS[sport]):
            return sport
    return ""


def _time_part(base, m, first_group, default_tz):
    """Apply 'at 4:00 PM UTC' (groups first_group..+3) to a date. No zone -> default_tz."""
    if not m.group(first_group):
        return base.replace(hour=9, tzinfo=CT), False
    h = int(m.group(first_group)) % 12 + (12 if m.group(first_group + 2).lower() == "p" else 0)
    zone = (m.group(first_group + 3) or "").upper()
    if zone in ("UTC", "GMT"):
        tz = timezone.utc
    elif zone:
        tz = ZoneInfo(TZS[zone])
    else:
        tz = default_tz
    return base.replace(hour=h, minute=int(m.group(first_group + 1) or 0), tzinfo=tz).astimezone(CT), True


def parse_card_date(text, now=None, default_tz=timezone.utc):
    """'Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football' -> (datetime CT, has_time, rest).
    Also 'Today at 11:00 AM ...'. A time with no zone uses default_tz (Topps' server page says UTC;
    a browser may show local time instead). Returns (None, False, text) when there's no date."""
    now = now or datetime.now(CT)
    text = text or ""
    m = RE_CARD_DATE.search(text)
    if m:
        month, day = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            base = datetime(year, month, day)
        except ValueError:
            return None, False, text
        if not m.group(3) and base.date() < (now - timedelta(days=120)).date():
            base = base.replace(year=year + 1)  # calendar rolled into next year
        when, has_time = _time_part(base, m, 4, default_tz)
        return when, has_time, text[m.end():]
    m = RE_RELATIVE_DATE.search(text)
    if m:
        local_now = now.astimezone(CT)
        d = local_now.date() + timedelta(days=1 if m.group(1).lower() == "tomorrow" else 0)
        when, has_time = _time_part(datetime(d.year, d.month, d.day), m, 2, CT if default_tz is timezone.utc
                                    else default_tz)
        return when, has_time, text[m.end():]
    return None, False, text


def _iso_in(node):
    """A machine-readable timestamp on the card (e.g. <time datetime="2026-09-30T16:00:00Z">), if any."""
    for el in [node] + list(node.find_all(True)):
        for v in el.attrs.values():
            if isinstance(v, str) and RE_ISO.match(v.strip()):
                try:
                    d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
                except ValueError:
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                has_time = "T00:00" not in v or d.hour or d.minute
                return d.astimezone(CT), bool(has_time)
    return None


def _card_node(anchor, key_of):
    """Highest ancestor of `anchor` whose product links all point to the same product."""
    node = anchor
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        keys = {key_of(x["href"]) for x in node.parent.find_all("a", href=True)}
        keys.discard(None)
        if len(keys) > 1:
            break
        node = node.parent
    return node


def status_from_text(text, default="Listed"):
    low = (text or "").lower()
    return next((label for word, label in STATUS_WORDS if word in low), default)


def _slug(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_topps_calendar(html, base_url="https://www.topps.com/release-calendar", now=None, default_tz=timezone.utc):
    """Every product card on the Topps release calendar -> list of dicts sorted by date.
    Cards whose date can't be read (countdowns, 'Live' labels near drop time) are kept with when=None."""
    soup = BeautifulSoup(html or "", "html.parser")
    low = (html or "").lower()
    sec_avail, sec_soon = low.find("available now"), low.find("dropping soon")

    def key_of(href):
        h = href.split("?")[0].split("#")[0]
        return _slug(urljoin(base_url, h)) if PRODUCT_LINK.search(h) else None

    cards = {}
    for a in soup.find_all("a", href=True):
        slug = key_of(a["href"])
        if not slug:
            continue
        url = urljoin(base_url, a["href"].split("?")[0].split("#")[0])
        c = cards.setdefault(slug, {"url": url, "texts": [], "alts": [], "raw": a["href"], "anchors": []})
        c["anchors"].append(a)
        txt = " ".join(a.get_text(" ").split())
        if txt:
            c["texts"].append(txt)
        c["alts"] += [img["alt"].strip() for img in a.find_all("img") if img.get("alt")]

    out = []
    for slug, c in cards.items():
        node = _card_node(c["anchors"][0], key_of)
        card_text = " ".join(node.get_text(" ").split())
        when, has_time, rest = None, False, ""
        for txt in sorted(c["texts"], key=len, reverse=True) + [card_text]:
            when, has_time, rest = parse_card_date(txt, now, default_tz)
            if when:
                break
        iso = _iso_in(node)
        if iso:
            when, has_time = iso
        name = (c["alts"][0] if c["alts"] else BUTTON_NOISE.sub("", rest if when else max(c["texts"] or [""], key=len)))
        name = " ".join(name.split()).strip(" -|·")
        if not when and not (sport_of(name) or len(name) > 12 and re.search(r"\b20\d\d\b", name)):
            continue  # nav/footer link, not a calendar card
        pos = low.find(c["raw"].lower())
        in_avail = sec_avail != -1 and pos > sec_avail and (sec_soon == -1 or sec_avail > sec_soon)
        out.append({"slug": slug, "name": name, "sport": sport_of(name), "url": c["url"],
                    "when": when.isoformat() if when else None, "has_time": has_time,
                    "status": status_from_text(card_text), "section": "Available now" if in_avail else "Dropping soon"})
    out.sort(key=lambda p: p["when"] or "")
    return out


def _variant_in(node, anchors):
    """Shopify variant id for a format card: <input name="id">, data-variant-id, or ?variant= on its link."""
    inp = node.find("input", attrs={"name": "id"})
    if inp and str(inp.get("value", "")).isdigit():
        return inp["value"]
    for el in [node] + list(node.find_all(True)):
        for attr in ("data-variant-id", "data-variant", "data-product-variant-id"):
            v = str(el.get(attr, ""))
            if v.isdigit():
                return v
    for a in anchors:
        m = re.search(r"[?&]variant=(\d+)", a.get("href", ""))
        if m:
            return m.group(1)
    return ""


def humanize_handle(handle):
    words = handle.replace("-", " ").split()
    keep = {"nfl", "nba", "mlb", "ufc", "wwe"}
    return " ".join(w.upper() if w in keep else (w if w[:1].isdigit() else w.capitalize()) for w in words)


def parse_topps_product_page(html, base_url):
    """A Topps product landing page (/pages/<slug>) -> every buyable format on it:
    [{"handle","name","url","price","status"}], plus any drop time shown on the page."""
    soup = BeautifulSoup(html or "", "html.parser")

    def key_of(href):
        m = TOPPS_PRODUCT_LINK.search(href.split("?")[0])
        return m.group(1).lower() if m else None

    found = {}
    for a in soup.find_all("a", href=True):
        handle = key_of(a["href"])
        if not handle:
            continue
        f = found.setdefault(handle, {"anchors": [], "texts": [], "alts": []})
        f["anchors"].append(a)
        t = " ".join(a.get_text(" ").split())
        if t:
            f["texts"].append(t)
        f["alts"] += [i["alt"].strip() for i in a.find_all("img") if i.get("alt")]
    # buttons that post straight to cart carry the product in a form, not a link
    for form in soup.find_all("form", action=re.compile(r"/cart/add")):
        handle = form.get("data-product-handle") or form.get("data-handle")
        if handle and handle.lower() not in found:
            found[handle.lower()] = {"anchors": [form], "texts": [], "alts": []}

    formats = []
    for handle, f in found.items():
        node = _card_node(f["anchors"][0], key_of)
        text = " ".join(node.get_text(" ").split())
        name = f["alts"][0] if f["alts"] else BUTTON_NOISE.sub("", max(f["texts"] or [""], key=len))
        name = RE_PRICE.sub("", " ".join(name.split())).strip(" -|·") or humanize_handle(handle)
        if len(name) < 4:
            name = humanize_handle(handle)
        price = RE_PRICE.search(text)
        variant = _variant_in(node, f["anchors"])
        host = "{0.scheme}://{0.netloc}".format(urlparse(base_url))
        formats.append({"handle": handle, "name": name, "url": urljoin(base_url, f"/products/{handle}"),
                        "price": f"${price.group(1)}" if price else "", "status": status_from_text(text),
                        "image": image_in(node, base_url),
                        "stock": stock_hint(text)[0], "limit": stock_hint(text)[1],
                        "add_to_cart": f"{host}/cart/add?id={variant}&quantity=1" if variant else "",
                        "buy_now": f"{host}/cart/{variant}:1" if variant else ""})
    page_text = soup.get_text(" ")
    return formats, extract_when(page_text[:20000])


# ---------------------------------------------------------------- Pokémon Center: cards only
TCG_STRONG = ("pokémon tcg", "pokemon tcg", "pokemon-tcg", "trading card game", "elite trainer box", "booster",
              "ultra-premium collection", "ultra premium collection", "premium collection", "build & battle",
              "build and battle", "battle deck", "league battle deck", "blister", "tech sticker collection",
              "poster collection", "knock out collection", "binder collection", "surprise box")
TCG_ACCESSORY = ("card sleeves", "sleeves", "deck box", "playmat", "play mat", "portfolio", "binder",
                 "card case", "toploader", "dice", "coin", "damage counter")
MERCH = ("hat", "cap", "beanie", "lanyard", "plush", "pin", "shirt", "tee", "hoodie", "sweatshirt", "jacket",
         "mug", "cup", "tumbler", "bottle", "sticker", "keychain", "key chain", "backpack", "bag", "pouch",
         "figure", "figurine", "poster", "blanket", "pillow", "socks", "wallet", "towel", "ornament", "puzzle",
         "lamp", "watch", "costume", "slippers", "necklace", "earrings", "bracelet", "lego", "pajama")
TCG_SEALED = ("elite trainer box", "booster", "collection", "tin", "bundle", "deck", "pack", "box", "display")


def is_tcg_product(text):
    """True for sealed Pokémon card products (ETBs, booster bundles, packs, UPCs, tins, collections, decks);
    False for merch (hats, lanyards, plush...) and card accessories (sleeves, binders, playmats)."""
    t = " " + re.sub(r"[-_/]+", " ", (text or "").lower()) + " "
    t = t.replace("pokemon tcg", "pokémon tcg")
    has = lambda words: any(re.search(r"\b" + re.escape(w) + r"s?\b", t) for w in words)
    strong = has(TCG_STRONG) or "pokémon tcg" in t
    if has(TCG_ACCESSORY) and not has(("elite trainer box", "booster", "collection", "tin", "bundle", "deck")):
        return False                       # sleeves / binders / playmats on their own
    if strong:
        return True
    if has(MERCH):
        return False
    return has(TCG_SEALED) and ("pokémon" in t or "pokemon" in t)


# ---------------------------------------------------------------- sports cards (sealed)
CARD_BRANDS = ("topps", "bowman", "panini", "donruss", "prizm", "select", "mosaic", "optic", "upper deck", "leaf",
               "onyx", "hoops", "chronicles", "contenders", "score", "stadium club", "heritage", "allen & ginter",
               "allen and ginter", "finest", "chrome", "absolute", "phoenix", "certified", "prestige", "origins")
CARD_SEALED = ("hobby box", "hobby", "blaster", "mega box", "mega", "hanger", "value box", "value pack", "fat pack",
               "cello", "jumbo", "tin", "retail box", "booster", "pack", "box", "collector", "trading card",
               "trading cards", "sapphire", "breaker", "case")
CARD_ACCESSORY = ("sleeve", "top loader", "toploader", "card saver", "one-touch", "one touch", "magnetic",
                  "display case", "binder", "storage box", "card holder", "graded card", "psa ", "bgs ", "sgc ",
                  "screwdown", "penny", "album", "frame", "stand", "protector")


def is_sports_card_product(text):
    t = " " + re.sub(r"[-_/]+", " ", (text or "").lower()) + " "
    if any(w in t for w in CARD_ACCESSORY):
        return False
    brand = any(re.search(r"(?<![a-z])" + re.escape(b) + r"(?![a-z])", t) for b in CARD_BRANDS)
    sport = bool(re.search(r"\b(baseball|basketball|football|nfl|nba|mlb|wnba|soccer|hockey|nhl|ufc|wwe|f1|"
                           r"formula 1|racing)\b", t))
    sealed = any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in CARD_SEALED)
    return brand and sealed and (sport or "trading card" in t or "topps" in t or "bowman" in t or "panini" in t)


def is_pokemon_product(text):
    """Sealed Pokémon card product that actually says Pokémon (other stores also sell Magic, Lorcana...)."""
    return bool(re.search(r"pok[eé]mon", text or "", re.I)) and is_tcg_product(text)


RE_ETB_UPC = re.compile(r"elite\s+trainer\s+box|(?<![a-z])etb(?![a-z])|ultra[\s-]*premium\s+collection|(?<![a-z])upc(?![a-z])",
                        re.I)


def is_etb_or_upc(text):
    """Elite Trainer Box or Ultra-Premium Collection - the drops that get @everyone anywhere."""
    return bool(RE_ETB_UPC.search(text or ""))


def is_card_product(text):
    """Pokémon card product or sealed sports-card product - what the store scanners keep."""
    return is_pokemon_product(text) or is_sports_card_product(text)


# ---------------------------------------------------------------- store search pages
# product-id patterns per store, and the direct cart links each store supports
RETAIL_STORES = {
    "target": {"label": "Target", "id": r"/A-(\d{6,10})"},
    "walmart": {"label": "Walmart", "id": r"/ip/(?:[^/?#]+/)?(\d{5,})"},
    "bestbuy": {"label": "Best Buy", "id": r"/sku/(\d{6,8})|/(\d{7})\.p|[?&]skuId=(\d{6,8})"},
    "amazon": {"label": "Amazon", "id": r"/(?:dp|gp/product)/([A-Z0-9]{10})"},
    "dicks": {"label": "Dick's", "id": r"/p/([a-z0-9-]*[a-z0-9]+)(?:/|$)"},
    "pokemon": {"label": "Pokémon Center", "id": r"/product/([0-9a-z-]+(?:/[0-9a-z-]+)?)"},
}
LIVE_WORDS = ("add to cart", "add to bag", "add for shipping", "add for pickup", "add for delivery", "buy now",
              "ship it", "pick it up", "deliver it", "add to basket")
NOT_LIVE_WORDS = ("out of stock", "sold out", "currently unavailable", "unavailable", "coming soon", "notify me",
                  "get notified", "not available", "temporarily out", "check stores", "see similar")


RE_STOCK = [re.compile(r"\bonly\s+(\d{1,3})\s+left\b", re.I), re.compile(r"\b(\d{1,3})\s+left in stock\b", re.I),
            re.compile(r"\b(\d{1,3})\s+(?:items?\s+)?(?:remaining|available)\b", re.I)]
RE_LIMIT = re.compile(r"\blimit(?:ed to)?\s+(\d{1,2})\s*(?:per|/)\s*(order|customer|household|person|guest|cart)"
                      r"|\blimit per (order|customer|household|person|guest|cart)\s*:?\s*(\d{1,2})", re.I)
LOW_WORDS = ("low stock", "limited stock", "almost gone", "selling fast", "few left", "limited quantity")


def stock_hint(text):
    """What the page says about how many are left, if anything: ("Only 3 left" | "Low stock" | "", "Limit 2 per order" | "")."""
    t = text or ""
    stock = ""
    for rx in RE_STOCK:
        m = rx.search(t)
        if m:
            stock = f"Only {m.group(1)} left"
            break
    if not stock:
        low = t.lower()
        stock = next((w.capitalize() for w in LOW_WORDS if w in low), "")
    m = RE_LIMIT.search(t)
    limit = ""
    if m:
        n, per = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        limit = f"Limit {n} per {per.lower()}"
    return stock, limit


def cart_links(store, pid):
    """(add_to_cart, buy_now) direct links where the store supports them."""
    if store == "walmart":
        return (f"https://affil.walmart.com/cart/addToCart?items={pid}",
                f"https://affil.walmart.com/cart/buynow?items={pid}")
    if store == "amazon":
        return f"https://www.amazon.com/gp/aws/cart/add.html?ASIN.1={pid}&Quantity.1=1", ""
    if store == "bestbuy":
        return f"https://api.bestbuy.com/click/-/{pid}/cart", ""
    return "", ""


def image_in(node, base_url):
    for img in node.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "srcset", "data-srcset"):
            v = (img.get(attr) or "").strip()
            if not v:
                continue
            v = v.split(",")[0].strip().split(" ")[0]
            if v.startswith("data:") or v.endswith(".svg") or "sprite" in v or "placeholder" in v:
                continue
            return urljoin(base_url, v)
    return ""


DRAWING_WORDS = ("enter drawing", "enter the drawing", "join drawing", "join the drawing", "request invite",
                 "request an invite", "get invite", "enter for a chance")


def tile_status(text, live_if_price=False):
    low = (text or "").lower()
    if any(w in low for w in DRAWING_WORDS):
        return "Drawing / invite open", True
    if any(w in low for w in NOT_LIVE_WORDS) and not any(w in low for w in LIVE_WORDS[:3]):
        return "Out of stock", False
    if any(w in low for w in LIVE_WORDS):
        return "In stock", True
    if live_if_price and RE_PRICE.search(text or ""):
        return "In stock", True
    return "Listed", False


def parse_retail_tiles(html, base_url, store, live_if_price=False):
    """Product tiles on a store search/category page ->
    [{"id","name","url","image","price","status","live","add_to_cart","buy_now"}]."""
    rx = re.compile(RETAIL_STORES[store]["id"])
    soup = BeautifulSoup(html or "", "html.parser")

    def key_of(href):
        m = rx.search(href.split("#")[0])
        return next((g for g in m.groups() if g), None) if m else None

    found = {}
    for a in soup.find_all("a", href=True):
        pid = key_of(a["href"])
        if not pid:
            continue
        f = found.setdefault(pid, {"anchors": [], "texts": [], "href": a["href"]})
        f["anchors"].append(a)
        t = " ".join(a.get_text(" ").split()) or a.get("aria-label", "") or a.get("title", "")
        if t:
            f["texts"].append(t)
        for img in a.find_all("img"):
            if img.get("alt"):
                f["texts"].append(img["alt"].strip())
    tiles = []
    for pid, f in found.items():
        node = _card_node(f["anchors"][0], key_of)
        text = " ".join(node.get_text(" ").split())
        name = best_name(f["texts"])
        if not name:
            continue
        status, live = tile_status(text, live_if_price)
        add, buy = cart_links(store, pid)
        stock, limit = stock_hint(text)
        tiles.append({"id": pid, "name": name[:200], "url": urljoin(base_url, f["href"].split("?")[0].split("#")[0]),
                      "image": image_in(node, base_url), "price": price_in(text),
                      "status": status, "live": live, "add_to_cart": add, "buy_now": buy, "text": text[:800],
                      "stock": stock, "limit": limit})
    if store == "walmart":
        tiles = _merge_walmart_json(tiles, html, base_url, live_if_price)
    if not tiles:
        tiles = title_tiles(soup, base_url, store, live_if_price)
    return tiles


def price_in(text):
    m = RE_CURRENT_PRICE.search(text or "") or RE_PRICE.search(text or "")
    return f"${m.group(1)}" if m else ""


# ---------------------------------------------------------------- product names
# store tiles wrap the title in badges: "10k+ bought in last month New at Topps™ 2026 Topps ...",
# and the star-rating link points at the product too ("1.2 out of 5 stars with 24 ratings")
RE_RATING = re.compile(r"\d(?:\.\d)?\s*out of\s*5\s*stars?.*?(?:reviews?|ratings?)(?:\s*\d[\d,]*\s*reviews?)?|"
                       r"\(\d[\d,]*\)\s*$|\brated\s+\d(?:\.\d)?\s+out of\s+5\b.*$", re.I)
NAME_NOISE = [re.compile(x, re.I) for x in (
    r"\b\d[\d.,]*k?\+?\s*bought in (?:the )?(?:last|past) (?:month|week|day)\b",
    r"\bnew at [^™®]{1,40}[™®]",
    r"^(?:sponsored|new|new arrival|best ?seller|only at target|only at walmart|deal|clearance|popular pick|"
    r"top rated|highly rated|rollback|reduced price|limited time|exclusive|pre-?order)\b[\s:·-]*",
    r"\bcurrent price\b.*$", r"\bwas \$.*$", r"\boptions? available\b.*$", r"\b\d+ options?\b.*$")]


def clean_name(text):
    t = " ".join((text or "").split())
    t = RE_RATING.sub("", t)
    for _ in range(3):                  # badges can stack: "Sponsored New at Topps™ ..."
        before = t
        for rx in NAME_NOISE:
            t = rx.sub("", t).strip(" -|·:")
        if t == before:
            break
    return BUTTON_NOISE.sub("", RE_PRICE.sub("", t)).strip(" -|·:")


def best_name(texts):
    """The real product title among a tile's link texts / image alts."""
    cands = []
    for x in texts:
        if not x or RE_PRICE.fullmatch(x.strip()) or re.search(r"out of\s*5\s*stars", x, re.I) and \
                len(RE_RATING.sub("", x).strip()) < 12:
            continue
        c = clean_name(x)
        if len(c) >= 6:
            cands.append(c)
    return max(cands, key=len, default="")


# ---------------------------------------------------------------- Walmart page data
def next_data(html):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _walk(o, depth=0):
    if depth > 40:
        return
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, depth + 1)


def _strings(o, depth=0, out=None):
    out = [] if out is None else out
    if depth > 6 or len(out) > 200:
        return out
    if isinstance(o, str):
        if 2 < len(o) < 200 and not o.startswith("http") and "{" not in o:
            out.append(o)
    elif isinstance(o, dict):
        for k, v in o.items():
            if k not in ("image", "imageInfo", "canonicalUrl", "seeSimilar", "__typename"):
                _strings(v, depth + 1, out)
    elif isinstance(o, list):
        for v in o:
            _strings(v, depth + 1, out)
    return out


def walmart_json_items(html, base_url="https://www.walmart.com/"):
    """Products in Walmart's embedded page data (search, browse and drawing pages all carry it).
    The drawing page's tiles have no product links, so this is how its items are found."""
    data = next_data(html)
    if not data:
        return []
    items = {}
    for d in _walk(data):
        pid, name = d.get("usItemId") or d.get("itemId"), d.get("name")
        if not pid or not isinstance(name, str) or not isinstance(pid, (str, int)) or len(name) < 4:
            continue
        pid = str(pid)
        if not pid.isdigit():
            continue
        price = ""
        pi = d.get("priceInfo") or {}
        cur = pi.get("currentPrice") if isinstance(pi, dict) else None
        if isinstance(cur, dict) and cur.get("price"):
            price = f"${float(cur['price']):,.2f}"
        elif isinstance(d.get("price"), (int, float)) and d["price"]:
            price = f"${float(d['price']):,.2f}"
        elif isinstance(pi, dict) and isinstance(pi.get("linePrice"), str):
            price = price_in(pi["linePrice"])
        img = d.get("image") if isinstance(d.get("image"), str) else ""
        if not img and isinstance(d.get("imageInfo"), dict):
            img = d["imageInfo"].get("thumbnailUrl") or ""
        avail = d.get("availabilityStatusV2") if isinstance(d.get("availabilityStatusV2"), dict) else {}
        avail = str(avail.get("value") or d.get("availabilityStatus") or d.get("availabilityStatusDisplayValue") or "")
        url = urljoin("https://www.walmart.com/", d.get("canonicalUrl") or f"/ip/{pid}").split("?")[0]
        text = " ".join(_strings(d))
        prev = items.get(pid)
        if prev and len(prev["text"]) >= len(text):
            continue
        start, end = _event_times(d)
        items[pid] = {"id": pid, "name": clean_name(name), "url": url, "image": img.split("?")[0] if img else "",
                      "price": price, "avail": avail.upper(), "text": text[:800],
                      "max_qty": d.get("maxOrderQuantity") or d.get("orderLimit") or "",
                      "start": start, "end": end}
    return list(items.values())


RE_ISO_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d")


def _as_time(v):
    """ISO string or epoch (s / ms) -> aware datetime in CT, else None."""
    try:
        if isinstance(v, str) and RE_ISO_TS.match(v):
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(CT)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 1.6e9 < v < 2.2e12:
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).astimezone(CT)
    except (ValueError, OSError, OverflowError):
        return None
    return None


def _event_times(item):
    """Drawing / event open + close times from an item's page data (keys like eventStartTime, drawStartDate...)."""
    start = end = None
    for d in _walk(item):
        for k, v in d.items():
            kl = k.lower()
            t = _as_time(v)
            if not t or not any(w in kl for w in ("start", "open", "begin", "end", "close", "expir")):
                continue
            if any(w in kl for w in ("start", "open", "begin")) and start is None:
                start = t
            elif any(w in kl for w in ("end", "close", "expir")) and end is None:
                end = t
    return start, end


def _merge_walmart_json(tiles, html, base_url, live_if_price=False):
    by_id = {x["id"]: x for x in tiles}
    for j in walmart_json_items(html, base_url):
        x = by_id.get(j["id"])
        text = ((x or {}).get("text", "") + " " + j["text"]).strip()
        status, live = tile_status(text, live_if_price)
        if j["avail"] == "IN_STOCK" and status == "Listed":
            status, live = "In stock", True
        elif j["avail"] in ("OUT_OF_STOCK", "NOT_AVAILABLE") and not status.startswith("Drawing"):
            status, live = "Out of stock", False
        stock, limit = stock_hint(text)
        if not limit and str(j["max_qty"]).isdigit() and 0 < int(j["max_qty"]) < 20:
            limit = f"Limit {j['max_qty']} per order"
        add, buy = cart_links("walmart", j["id"])
        merged = {"id": j["id"], "name": j["name"] or (x or {}).get("name", ""), "url": j["url"],
                  "image": j["image"] or (x or {}).get("image", ""), "price": j["price"] or (x or {}).get("price", ""),
                  "status": status, "live": live, "add_to_cart": add, "buy_now": buy, "text": text[:1200],
                  "stock": stock or (x or {}).get("stock", ""), "limit": limit or (x or {}).get("limit", ""),
                  "start": j["start"], "end": j["end"]}
        by_id[j["id"]] = merged
    return list(by_id.values())


def title_tiles(soup, base_url, store, live_if_price=False):
    """Last resort when tiles have no product links: product titles (h2/h3 or product-title) and their card."""
    tiles, seen = [], set()
    heads = soup.select('[data-automation-id="product-title"], [data-testid="product-title"]') or soup.find_all(["h2", "h3"])
    for h in heads:
        name = clean_name(h.get_text(" "))
        if len(name) < 6 or name.lower() in seen:
            continue
        node = h
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            if len(node.parent.find_all(["h2", "h3"])) > 1 or \
                    len(node.parent.select('[data-automation-id="product-title"]')) > 1:
                break
            node = node.parent
        text = " ".join(node.get_text(" ").split())
        if not RE_PRICE.search(text) and "drawing" not in text.lower():
            continue
        seen.add(name.lower())
        status, live = tile_status(text, live_if_price)
        stock, limit = stock_hint(text)
        pid = "t" + hashlib.sha1(name.lower().encode()).hexdigest()[:10]
        tiles.append({"id": pid, "name": name[:200], "url": base_url.split("?")[0], "image": image_in(node, base_url),
                      "price": price_in(text), "status": status, "live": live, "add_to_cart": "", "buy_now": "",
                      "text": text[:800], "stock": stock, "limit": limit})
    return tiles


RE_DRAW_START = re.compile(r"(?:drawing\s+)?(?:starts|opens|begins|opening)\s*:?\s*", re.I)
RE_DRAW_END = re.compile(r"(?:drawing\s+)?(?:ends|closes|closing|entries close|enter by)\s*:?\s*", re.I)
DRAW_CLOSED = ("drawing closed", "drawing ended", "drawing has ended", "entries closed", "entry closed",
               "drawing is closed", "winners selected")


def drawing_window(text, now=None):
    """'Drawing starts Sep 30, 2:00pm PDT' / 'Ends Oct 1, 11:59pm PT' -> (start, end, closed) in CT (or None)."""
    def after(rx):
        m = rx.search(text or "")
        if not m:
            return None
        w = extract_when(text[m.end(): m.end() + 60], now)
        return w[0] if w else None
    low = (text or "").lower()
    return after(RE_DRAW_START), after(RE_DRAW_END), any(w in low for w in DRAW_CLOSED)


# ---------------------------------------------------------------- topps.com: every product as it loads
# topps.com/products/sitemap.xml indexes numbered product sitemaps; the highest numbers hold the newest products
RE_SITEMAP_N = re.compile(r"topps\.com/products/sitemap/(\d+)\.xml")
RE_TOPPS_PRODUCT_URL = re.compile(r"https?://www\.topps\.com/products/(?!sitemap)[^\s<>\"']+")
TOPPS_SINGLE = re.compile(r"topps-now|living-set|-card-\d+$|card-\d+$|/?[a-z-]+-\d{2,4}-(?:mlb|nba|nfl)-topps-now|"
                          r"autograph(?:ed)?-card|relic-card|graded|digital|gift-card|art-print|poster|apparel|"
                          r"t-shirt|hoodie|jersey|hat$|-cap$", re.I)
TOPPS_SEALED = re.compile(r"box|pack|blaster|mega|hobby|jumbo|value|hanger|case|tin|bundle|collector|fat|cello|"
                          r"breaker|sapphire|display|starter|kit|factory-set|team-set|complete-set|set$|super", re.I)


def topps_sitemap_numbers(text):
    return sorted({int(n) for n in RE_SITEMAP_N.findall(text or "")})


def topps_product_urls(text):
    out = []
    for u in RE_TOPPS_PRODUCT_URL.findall(text or ""):
        u = u.split("?")[0].split("#")[0].rstrip("/.,")
        if u not in out:
            out.append(u)
    return out


def topps_handle(url):
    from urllib.parse import unquote
    return unquote(url.rstrip("/").rsplit("/products/", 1)[-1]).lower()


def is_topps_sealed(url):
    """Sealed formats (Hobby, Jumbo, Mega, Blaster, Value, hanger, cases, sets...) of any line - sports, Disney,
    F1, Star Wars... - but not single cards (Topps NOW, Living Set, autographs) or merch."""
    h = topps_handle(url)
    return bool(TOPPS_SEALED.search(h)) and not TOPPS_SINGLE.search(h)


RE_AVAILABLE_FROM = re.compile(r"available\s+(?:from|on)\s+(\d{1,2})\.?\s*([a-z]{3,9})\.?\s*(\d{4})", re.I)


def parse_topps_item_page(html, url, now=None):
    """One topps.com/products/<handle> page -> {name, url, image, price, status, live, limit, stock,
    add_to_cart, buy_now, when (iso or ""), has_time}."""
    soup = BeautifulSoup(html or "", "html.parser")

    def meta(*names):
        for n in names:
            m = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if m and m.get("content"):
                return m["content"].strip()
        return ""
    name = meta("og:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "") or humanize_handle(topps_handle(url))
    name = re.sub(r"\s*[|–-]\s*Topps\s*$", "", name).strip()
    image = meta("og:image", "twitter:image")
    price = meta("product:price:amount", "og:price:amount")
    availability = ""
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except ValueError:
            continue
        for d in _walk(data):
            offers = d.get("offers")
            for o in (offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []):
                price = price or str(o.get("price") or "")
                availability = availability or str(o.get("availability") or "")
    text = " ".join(soup.get_text(" ").split())
    head = text[:6000]                               # the product's own block, before "you may also like"
    low = head.lower()
    av = availability.lower()
    if any(w in low for w in ("enter drawing", "enter the drawing", "enter now")):
        status = "Drawing open"
    elif "preorder" in av or "pre-order" in low and "sold out" not in low:
        status = "Pre-order"
    elif "instock" in av or ("add to cart" in low and "sold out" not in low):
        status = "On sale"
    elif "outofstock" in av or "soldout" in av or "sold out" in low:
        status = "Sold out"
    elif any(w in low for w in ("notify me", "get notified", "coming soon")):
        status = "Upcoming"
    else:
        status = "Listed"
    if price and not price.startswith("$"):
        try:
            price = f"${float(price):,.2f}"
        except ValueError:
            price = ""
    price = price or price_in(head)
    m = re.search(r"ProductVariant/(\d+)", html or "") or re.search(r'"variantId"\s*:\s*"?(\d{6,})', html or "")
    host = "https://www.topps.com"
    variant = m.group(1) if m else ""
    stock, limit = stock_hint(head)
    when, has_time = "", False
    w = best_time_for(head, now=now) or extract_when(head, now)
    if w:
        when, has_time = w[0].isoformat(), w[1]
    else:
        m2 = RE_AVAILABLE_FROM.search(head)
        if m2 and m2.group(2)[:3].lower() in MONTHS:
            try:
                when = datetime(int(m2.group(3)), MONTHS[m2.group(2)[:3].lower()], int(m2.group(1)), 9,
                                tzinfo=CT).isoformat()
            except ValueError:
                pass
    return {"name": name[:200], "url": url, "image": image, "price": price, "status": status,
            "live": status in ("On sale", "Pre-order", "Drawing open"), "stock": stock, "limit": limit,
            "add_to_cart": f"{host}/cart/add?id={variant}&quantity=1" if variant else "",
            "buy_now": f"{host}/cart/{variant}:1" if variant else "", "when": when, "has_time": has_time}
