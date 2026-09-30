"""Pure parsing helpers: dates/times, Topps calendar cards, sport/category detection.
No network, no files - everything here is unit-tested in tests/test_parsing.py."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin
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
                return local, has_time, tz_note
    return None


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
RE_PRICE = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)")


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
        formats.append({"handle": handle, "name": name, "url": urljoin(base_url, f"/products/{handle}"),
                        "price": f"${price.group(1)}" if price else "", "status": status_from_text(text)})
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
