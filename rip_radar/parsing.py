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
                 "captcha", "pardon our interruption"]

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
RE_CARD_DATE = re.compile(
    r"^\s*(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
    # a year only counts after a comma ("Oct 6, 2026"), so "Oct 6 2026-27 Topps..." keeps the product's year
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:,\s*(20\d\d)(?![-\d]))?"
    r"(?:\s+at\s+(\d{1,2})(?::(\d\d))?\s*([ap])\.?m\.?\s*(UTC|GMT|ET|EST|EDT|CT|CST|CDT|PT|PST|PDT)?)?", re.I)
STATUS_WORDS = [("sold out", "Sold out"), ("pre-order", "Pre-order"), ("preorder", "Pre-order"),
                ("enter drawing", "Drawing open"), ("enter the drawing", "Drawing open"),
                ("buy now", "On sale"), ("shop now", "On sale"), ("add to cart", "On sale"),
                ("available now", "On sale"), ("notify me", "Upcoming"), ("coming soon", "Upcoming")]
LIVE_STATUSES = {"Pre-order", "Drawing open", "On sale"}
BUTTON_NOISE = re.compile(r"\b(notify me|pre-?order( now)?|buy now|shop now|add to cart|sold out|"
                          r"available now|coming soon|enter( the)? drawing)\b", re.I)
PRODUCT_LINK = re.compile(r"/(pages|products)/", re.I)


def sport_of(name):
    t = " " + (name or "").lower()
    if any(w in t for w in NOT_OUR_SPORTS):
        return ""
    for sport in ("Basketball", "Football", "Baseball"):  # order matters: "Bowman Football" is football
        if any(w in t for w in SPORT_WORDS[sport]):
            return sport
    return ""


def parse_card_date(text, now=None):
    """'Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football' -> (datetime CT, has_time, rest)."""
    now = now or datetime.now(CT)
    m = RE_CARD_DATE.search(text or "")
    if not m:
        return None, False, text
    month, day = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
    year = int(m.group(3)) if m.group(3) else now.year
    try:
        base = datetime(year, month, day)
    except ValueError:
        return None, False, text
    if not m.group(3) and base.date() < (now - timedelta(days=120)).date():
        base = base.replace(year=year + 1)  # calendar rolled into next year
    if m.group(4):
        h = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
        zone = (m.group(7) or "").upper()
        tz = timezone.utc if zone in ("", "UTC", "GMT") else ZoneInfo(TZS[zone])
        when = base.replace(hour=h, minute=int(m.group(5) or 0), tzinfo=tz).astimezone(CT)
        return when, True, text[m.end():]
    return base.replace(hour=9, tzinfo=CT), False, text[m.end():]


def _slug(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_topps_calendar(html, base_url="https://www.topps.com/release-calendar", now=None):
    """Every product card on the Topps release calendar -> list of dicts, sorted by date."""
    soup = BeautifulSoup(html or "", "html.parser")
    low = (html or "").lower()
    sec_avail, sec_soon = low.find("available now"), low.find("dropping soon")
    cards = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        if not PRODUCT_LINK.search(href):
            continue
        url = urljoin(base_url, href)
        c = cards.setdefault(_slug(url), {"url": url, "texts": [], "alts": [], "raw": a["href"], "anchors": []})
        c["anchors"].append(a)
        txt = " ".join(a.get_text(" ").split())
        if txt:
            c["texts"].append(txt)
        c["alts"] += [img["alt"].strip() for img in a.find_all("img") if img.get("alt")]

    out = []
    for slug, c in cards.items():
        when, has_time, rest = None, False, ""
        for txt in sorted(c["texts"], key=len, reverse=True):
            when, has_time, rest = parse_card_date(txt, now)
            if when:
                break
        if not when:
            continue  # nav/footer link, not a calendar card
        name = " ".join((c["alts"][0] if c["alts"] else BUTTON_NOISE.sub("", rest)).split()).strip(" -|·")
        node = c["anchors"][0]  # the card = highest ancestor holding links to this product only
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            others = {_slug(urljoin(base_url, x["href"].split("?")[0]))
                      for x in node.parent.find_all("a", href=True) if PRODUCT_LINK.search(x["href"])}
            if len(others) > 1:
                break
            node = node.parent
        card_text = " ".join(node.get_text(" ").split()).lower()
        status = next((label for word, label in STATUS_WORDS if word in card_text), "Listed")
        pos = low.find(c["raw"].lower())
        in_avail = sec_avail != -1 and pos > sec_avail and (sec_soon == -1 or sec_avail > sec_soon)
        out.append({"slug": slug, "name": name, "sport": sport_of(name), "url": c["url"],
                    "when": when.isoformat(), "has_time": has_time, "status": status,
                    "section": "Available now" if in_avail else "Dropping soon"})
    out.sort(key=lambda p: p["when"])
    return out
