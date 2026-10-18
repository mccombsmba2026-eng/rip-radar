"""Readers for pages that announce Pokémon release dates and Topps / EQL raffles (1.0.64).

All pure functions (html in -> list out) so they're unit-tested with fixtures. They're written to survive layout
changes: they look for product names next to dates rather than for one site's exact CSS classes.
"""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .parsing import CT, MONTHS, iter_when

# a sealed Pokémon product name (cards only - no plush, sleeves, binders on their own)
RE_PRODUCT = re.compile(
    r"elite trainer box|\betb\b|booster (?:box|bundle|pack|display|case)|\bbooster\b|collection|\btins?\b|blister|"
    r"build\s*(?:&|and)\s*battle|premium|\bbox(?:es)?\b|\bbundle\b|\bchest\b|trainer'?s toolkit|battle deck|"
    r"\bdecks?\b|expansion|\bset\b", re.I)
RE_NOT_PRODUCT = re.compile(r"\bplush|sleeves?\b(?!.*(?:pack|booster))|playmat|video game|\bswitch\b|figure only|"
                            r"apparel|t-shirt|hoodie|\bmug\b|pokémon go|pokemon go|\banime\b|episode", re.I)
RE_MONTH_YEAR = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
                           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(20\d\d)\b", re.I)
RE_DATE_BIT = re.compile(r"\s*[-–—:|·,]*\s*(?:\(?\s*(?:available|releases?|releasing|release date|launch(?:es|ing)?|"
                         r"on sale|arrives?|coming|street date)\b[^A-Za-z0-9]*)?(?:on\s+)?"
                         r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
                         r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?"
                         r"(?:,?\s+20\d\d)?\)?.*$", re.I)
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "dt", "dd", "td", "th", "figcaption", "a",
              "strong", "b", "span", "div", "article", "section"]


def _blocks(html):
    """Visible text blocks in page order (smallest elements that hold text, no repeats)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for bad in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        bad.decompose()
    out = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "div", "article", "section", "td"]):
            continue                              # only the innermost blocks: a parent repeats its children
        text = " ".join(el.get_text(" ").split())
        if not (2 <= len(text) <= 400):
            continue
        if out and out[-1]["text"] == text:      # a link inside a cell with the same text: one block
            continue
        a = el if el.name == "a" else el.find("a", href=True)
        out.append({"text": text, "tag": el.name, "href": a.get("href") if a is not None and a.get("href") else ""})
    return out


def _clean_name(text):
    name = RE_DATE_BIT.sub("", text).strip(" -–—:|·,.()")
    name = re.sub(r"^(?:new|coming soon|pre-?order|available now)[:\s-]+", "", name, flags=re.I)
    return name[:140].strip()


def _is_product(text):
    return bool(RE_PRODUCT.search(text)) and not RE_NOT_PRODUCT.search(text) and len(text.split()) <= 22


def dated_products(html, base_url="", now=None):
    """-> [{"name", "when": datetime (CT, midnight), "url"}] for every product named next to a date.
    Handles "Product - Available Nov 6", a date heading followed by the products released that day, and a product
    heading followed by a "Release date: ..." line. `now` anchors dates written without a year."""
    now = now or datetime.now(CT)
    blocks = _blocks(html)
    dates = []
    for b in blocks:
        w = next(iter_when(b["text"], now, horizon_days=400, past_hours=24 * 31), None)
        dates.append(w[0] if w else None)
    found, used = [], set()

    def add(i, when):
        name = _clean_name(blocks[i]["text"])
        if not name or not _is_product(name) or (name.lower(), when.date()) in used:
            return
        used.add((name.lower(), when.date()))
        found.append({"name": name, "when": when,
                      "url": urljoin(base_url, blocks[i]["href"]) if blocks[i]["href"] else base_url})

    heads = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "th", "dt"}
    added = set()

    def add_i(i, when):
        n = len(found)
        add(i, when)
        if len(found) > n:
            added.add(i)

    last_date, last_date_at = None, -99
    for i, b in enumerate(blocks):
        text, d = b["text"], dates[i]
        product = _is_product(text)
        if d and product and len(_clean_name(text)) >= 6:
            add_i(i, d)                            # "Delta Reign Elite Trainer Box - Available November 6"
            continue
        if d:
            j = i - 1
            if b["tag"] not in heads and j >= 0 and j not in added and not dates[j] and _is_product(blocks[j]["text"]):
                add_i(j, d)                        # product, then its "Release date: ..." line / table cell
            else:
                last_date, last_date_at = d, i     # a date heading: the products listed under it
            continue
        if product and last_date is not None and i - last_date_at <= 8:
            nb = blocks[i + 1] if i + 1 < len(blocks) else None
            if nb and dates[i + 1] and nb["tag"] not in heads and not _is_product(nb["text"]):
                continue                           # its own date line follows - handled there
            add_i(i, last_date)
    return found


def article_anchor(url, html=""):
    """The month an article is about ('...-release-in-october-2026' or a 'October 2026' heading) -> datetime."""
    m = RE_MONTH_YEAR.search(url.replace("-", " ")) or RE_MONTH_YEAR.search(BeautifulSoup(html or "", "html.parser")
                                                                           .get_text(" ")[:3000])
    if not m:
        return None
    return datetime(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1, tzinfo=CT)


def month_article_urls(now=None, months=2):
    """pokemon.com's monthly round-up for this month and the next ones (two URL styles are in use)."""
    now = now or datetime.now(CT)
    out = []
    y, m = now.year, now.month
    for _ in range(months):
        name = datetime(y, m, 1).strftime("%B").lower()
        slug = f"check-out-every-pokemon-tcg-product-release-in-{name}-{y}"
        out.append([f"https://www.pokemon.com/us/news/{slug}", f"https://www.pokemon.com/us/pokemon-news/{slug}"])
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def calendar_grid(html, now=None):
    """A month calendar drawn as a grid (pokemonrestock.com): a 'October 2026' title, then day cells whose text
    starts with the day number followed by the drops that day ("16 Target 30th Celebration ETB").
    -> [{"name", "when", "store"}]"""
    now = now or datetime.now(CT)
    text = BeautifulSoup(html or "", "html.parser").get_text(" ")
    anchor = None
    m = RE_MONTH_YEAR.search(text)
    if m:
        anchor = datetime(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1, tzinfo=CT)
    if not anchor:
        return []
    stores = {"target": "target", "walmart draw": "walmart", "walmart": "walmart", "pokemon center": "pokemon",
              "pokémon center": "pokemon", "best buy": "bestbuy", "gamestop": "gamestop", "amazon": "amazon",
              "costco": "costco", "sam's club": "samsclub", "sams club": "samsclub"}
    out, used = [], set()
    soup = BeautifulSoup(html or "", "html.parser")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    for el in soup.find_all(["td", "div", "li", "button", "article"]):
        t = " ".join(el.get_text(" ").split())
        dm = re.match(r"^(\d{1,2})\s+(.{6,300})$", t)
        if not dm or el.find(["td", "li", "article"]):
            continue
        try:
            day = anchor.replace(day=int(dm.group(1)))
        except ValueError:
            continue
        rest = dm.group(2)
        low = rest.lower()
        store = next((v for k, v in stores.items() if k in low), "")
        if not store:
            continue
        # one cell can hold several drops: split on store names
        parts = re.split(r"(?i)(target|walmart draw|walmart|pok[eé]mon center|best buy|gamestop|amazon|costco|"
                         r"sam'?s club)", rest)
        cur = store
        for p in parts:
            p = p.strip(" ·-–—:|,")
            if not p:
                continue
            k = p.lower()
            if k in stores or k.replace("é", "e") in stores:
                cur = stores.get(k, stores.get(k.replace("é", "e"), cur))
                continue
            if not _is_product(p) and len(p.split()) < 2:
                continue
            key = (p.lower(), day.date(), cur)
            if key in used:
                continue
            used.add(key)
            out.append({"name": p[:140], "when": day, "store": cur, "walmart_draw": "walmart draw" in low})
    return out


# ------------------------------------------------------------------ EQL (Topps' raffle / launch site)
RE_LAUNCH = re.compile(r"https?://launches\.topps\.com/[a-z]{2}-[A-Z]{2}/launch/[a-z0-9][a-z0-9-]+", re.I)


def eql_launch_links(html, base_url="https://launches.topps.com/en-US/"):
    """Every launch page linked from a page (the launch list, a topps.com product page...). /login etc. dropped."""
    soup = BeautifulSoup(html or "", "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(base_url, a["href"].split("?")[0].split("#")[0]).rstrip("/")
        m = RE_LAUNCH.match(u)
        if m and m.group(0) == u:
            text = " ".join(a.get_text(" ").split())
            if u not in out or len(text) > len(out[u]):
                out[u] = text
    for m in RE_LAUNCH.finditer(html or ""):              # links inside page data / scripts
        out.setdefault(m.group(0).rstrip("/"), "")
    return out


def _iso(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(CT) if d.tzinfo else d.replace(tzinfo=CT)
    except ValueError:
        return None


def parse_eql_launch(html, url):
    """One EQL launch page -> {name, image, price, limit, opens, closes (datetime CT or None), status}."""
    soup = BeautifulSoup(html or "", "html.parser")

    def meta(*keys):
        for k in keys:
            tag = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""
    h1 = soup.find("h1")
    name = (" ".join(h1.get_text(" ").split()) if h1 else "") or meta("og:title", "twitter:title") or \
        (soup.title.get_text(" ").strip() if soup.title else "")
    name = re.sub(r"\s*[|·-]\s*(?:EQL|Topps|Launches?).*$", "", name, flags=re.I).strip()
    for bad in soup(["script", "style", "noscript"]):
        bad.extract()
    text = " ".join(soup.get_text(" ").split())
    price = ""
    pm = re.search(r"(?:US)?\$\s?\d[\d,]*(?:\.\d\d)?", text)
    if pm:
        price = pm.group(0).replace("US", "").replace(" ", "")
    lm = re.search(r"(?:maximum|max\.?|limit)\s+(?:of\s+)?(\d+)\s+(?:per|each)", text, re.I)
    opens = closes = None
    # page data first (exact times): "...closingTime":"2026-10-21T16:00:00Z"
    for key, val in re.findall(r'"([A-Za-z]*(?:[Oo]pen|[Ss]tart|[Cc]los|[Ee]nd)[A-Za-z]*)"\s*:\s*"(20\d\d-\d\d-\d\dT[^"]+)"',
                               html or ""):
        d = _iso(val)
        if not d:
            continue
        if re.search(r"clos|end", key, re.I):
            closes = closes or d
        else:
            opens = opens or d
    if not closes:   # visible text: "Entries close Oct 21 at 11:00 AM CT" / "Closing in 2 days"
        cm = re.search(r"(?:clos(?:es|ing)|ends?|deadline)\b[^.]{0,20}?((?:mon|tue|wed|thu|fri|sat|sun)?[a-z,]*\s*"
                       r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}[^.]{0,30})", text, re.I)
        if cm:
            w = next(iter_when(cm.group(1)), None)
            closes = w[0] if w else None
    if not opens:
        om = re.search(r"(?:opens?|starts?|begins?)\b[^.]{0,20}?((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                       r"[a-z]*\.?\s+\d{1,2}[^.]{0,30})", text, re.I)
        if om:
            w = next(iter_when(om.group(1)), None)
            opens = w[0] if w else None
    low = text.lower()
    if re.search(r"\bclosed\b|has ended|no longer accepting|launch (?:is )?over|sold out", low):
        status = "Closed"
    elif re.search(r"coming soon|opens in|not (?:yet )?open|starts in", low):
        status = "Coming soon"
    else:
        status = "Open"
    now = datetime.now(CT)
    if closes and closes < now:
        status = "Closed"
    elif opens and opens > now:
        status = "Coming soon"
    image = meta("og:image", "twitter:image")
    return {"name": name or url.rsplit("/", 1)[-1].replace("-", " ").title(), "image": image, "price": price,
            "limit": lm.group(1) if lm else "", "opens": opens, "closes": closes, "status": status, "url": url}


def similar(a, b):
    """Two product names for the same thing (press site vs article vs a store's wording)."""
    def words(s):
        s = re.sub(r"pok[eé]mon|tcg|trading card game|[:\-–—®™]", " ", s.lower())
        return {w for w in re.findall(r"[a-z0-9]+", s) if w not in ("the", "and", "of", "a", "with")}
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.7
