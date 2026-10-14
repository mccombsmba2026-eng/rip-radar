"""What a product actually sells for on eBay (sold listings), shown on each product post next to the store price.
Runs in the background: one search every ~20 s at most, each product cached for 24 h. Approximate by nature -
it's the median of recent sold listings whose titles match the product."""
import logging
import queue
import re
import statistics
import threading
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("rip_radar")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36 Edg/129.0")
EXCLUDE = ("lot", "graded", "psa", "bgs", "cgc", "sgc", "empty", "code", "digital", "opened", "damaged", "read",
           "custom", "proxy", "replica", "repack", "mystery", "break", "spot", "random")
STOP = {"pokemon", "pokémon", "tcg", "the", "and", "of", "a", "trading", "card", "cards", "game", "for", "with",
        "new", "sealed", "exclusive", "edition", "packs", "pack", "count", "ct", "pc", "-", "&", "x"}
RE_MONEY = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)")


def clean_query(name):
    """'Pokémon TCG: Prismatic Evolutions Booster Bundle (6 Packs)' -> 'Pokémon Prismatic Evolutions Booster Bundle'"""
    t = re.sub(r"\(.*?\)|\[.*?\]|[®™]", " ", name or "")
    t = re.sub(r"\b(?:tcg|trading card game|target exclusive|walmart exclusive|new|sealed)\b|:", " ", t, flags=re.I)
    words = t.split()[:9]
    return " ".join(words)


def key_words(q):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]*", q.lower()) if w not in STOP and len(w) > 1]


def search_url(q):
    nkw = q + " " + " ".join("-" + w for w in ("lot", "graded", "psa", "empty", "code", "digital"))
    return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(nkw)}&LH_Sold=1&LH_Complete=1&_ipg=60&rt=nc"


def parse_sold(html, query):
    """Sold listings whose title has (nearly) all the product's words -> prices."""
    want = key_words(query)
    soup = BeautifulSoup(html or "", "html.parser")
    prices = []
    for li in soup.find_all("li"):
        cls = " ".join(li.get("class") or [])
        if "s-item" not in cls and "s-card" not in cls:
            continue
        title_el = next((e for e in li.find_all(True) if "title" in " ".join(e.get("class") or [])), None)
        price_el = next((e for e in li.find_all(True) if "price" in " ".join(e.get("class") or [])), None)
        if not title_el or not price_el:
            continue
        title = " ".join(title_el.get_text(" ").split()).lower()
        if not title or "shop on ebay" in title:
            continue
        if any(re.search(r"\b" + w + r"\b", title) for w in EXCLUDE):
            continue
        have = sum(1 for w in want if w in title)
        if want and have < max(1, round(len(want) * 0.75)):
            continue
        ptxt = price_el.get_text(" ")
        if " to " in ptxt:                       # a price range = multiple variations: skip
            continue
        m = RE_MONEY.search(ptxt)
        if m:
            prices.append(float(m.group(1).replace(",", "")))
    return prices


def summarize(prices):
    if len(prices) < 3:
        return None
    prices = sorted(prices)
    med = statistics.median(prices)
    keep = [p for p in prices if med / 3 <= p <= med * 3]      # drop obvious mismatches (singles, cases)
    if len(keep) < 3:
        return None
    return {"median": round(statistics.median(keep), 2), "n": len(keep), "low": keep[0], "high": keep[-1]}


class ResaleChecker:
    def __init__(self, state, get_settings, pace=20, ttl_hours=24):
        self.state, self.get_settings, self.pace, self.ttl = state, get_settings, pace, ttl_hours * 3600
        self.cache = state.setdefault("resale", {})
        self.q = queue.Queue()
        self.queued = set()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self._thread = None
        self._lock = threading.Lock()

    def enabled(self):
        return self.get_settings().get("resale_check", True)

    def lookup(self, name, urgent=False):
        """Cached result (or None) - and queue a fresh search when there isn't one."""
        if not self.enabled() or not name:
            return None
        q = clean_query(name)
        key = q.lower()
        hit = self.cache.get(key)
        if hit and time.time() - hit.get("at", 0) < self.ttl:
            return hit if hit.get("median") else None
        with self._lock:
            if key not in self.queued:
                self.queued.add(key)
                self.q.put((0 if urgent else 1, time.time(), q))
        self._start()
        return hit if hit and hit.get("median") else None

    def text(self, r, price_text=""):
        url = search_url(r.get("q", ""))
        line = f"[${r['median']:,.0f} median of {r['n']} sold]({url}) · range ${r['low']:,.0f}–${r['high']:,.0f}"
        try:
            price = float(str(price_text).replace("$", "").replace(",", ""))
        except ValueError:
            return line
        diff = r["median"] - price
        pct = int(round(diff / price * 100)) if price else 0
        return line + f"\n{'🟢 +' if diff >= 0 else '🔴 −'}${abs(diff):,.0f} ({'+' if pct >= 0 else ''}{pct}%) vs this price"

    def _start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="resale")
        self._thread.start()

    def _run(self):
        while True:
            pri, _, q = self.q.get()
            key = q.lower()
            try:
                self.cache[key] = self.check(q)
            except Exception as e:
                log.info("resale %s: %s", q, e)
                self.cache[key] = {"at": time.time(), "q": q}
            finally:
                with self._lock:
                    self.queued.discard(key)
            time.sleep(self.pace)

    def check(self, q):
        r = self.s.get(search_url(q), timeout=20)
        res = {"at": time.time(), "q": q}
        if r.status_code < 400:
            got = summarize(parse_sold(r.text, q))
            if got:
                res.update(got)
        else:
            log.info("resale: eBay HTTP %s", r.status_code)
        return res
