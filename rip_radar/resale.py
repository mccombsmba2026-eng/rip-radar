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
    FAIL_TTL = 1800          # a failed lookup is retried after 30 min (not cached for a day)

    def __init__(self, state, get_settings, pace=15, ttl_hours=24, on_result=None, browser_fetch=None):
        self.state, self.get_settings, self.pace, self.ttl = state, get_settings, pace, ttl_hours * 3600
        # eBay turns plain Python requests away (HTTP 403): ask through a hidden real-browser window when we have one
        self.browser_fetch = browser_fetch
        self.stats = {"ok": 0, "priced": 0, "fail": 0, "fail_streak": 0, "last_error": "", "last_ok": 0,
                      "via": ""}
        self.cache = state.setdefault("resale", {})
        self.q = queue.PriorityQueue()       # live drops (priority 0) jump ahead of background valuing (1)
        self.on_result = on_result or (lambda key, result: None)
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
        if hit and time.time() - hit.get("at", 0) < (self.ttl if hit.get("ok", "median" in hit) else self.FAIL_TTL):
            return hit if hit.get("median") else None
        with self._lock:
            if key not in self.queued or urgent:
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
            fresh = self.cache.get(key)
            if fresh and fresh.get("median") and time.time() - fresh.get("at", 0) < self.ttl:
                with self._lock:
                    self.queued.discard(key)
                self.on_result(key, fresh)          # already known (a duplicate urgent request): just use it
                continue
            try:
                self.cache[key] = self.check(q)
            except Exception as e:
                log.info("resale %s: %s", q, e)
                self._failed(f"{type(e).__name__}")
                self.cache[key] = {"at": time.time(), "q": q, "ok": False}
            finally:
                with self._lock:
                    self.queued.discard(key)
            try:
                self.on_result(key, self.cache[key])  # put the value on the post right away
            except Exception as e:
                log.info("resale callback: %s", e)
            if pri:                                   # live drops (priority 0) go one after another, no pause
                time.sleep(self.pace)

    @staticmethod
    def key(name):
        return clean_query(name).lower()

    def _failed(self, why):
        self.stats["fail"] += 1
        self.stats["fail_streak"] += 1
        self.stats["last_error"] = why

    def _get(self, url):
        """-> (status, html, how). Hidden browser first (eBay blocks plain requests); plain HTTP as the fallback."""
        if self.browser_fetch:
            try:
                status, _, html = self.browser_fetch(url)
                if html and any(k in html for k in ("s-item", "s-card", "srp-results")):
                    return status, html, "browser"
                if html and ("captcha" in html.lower() or "pardon our interruption" in html.lower()):
                    return 403, html, "browser"
            except Exception as e:
                log.info("resale browser: %s", e)
        r = self.s.get(url, timeout=20)
        return r.status_code, r.text, "http"

    def check(self, q):
        status, html, how = self._get(search_url(q))
        res = {"at": time.time(), "q": q, "ok": status < 400}
        if status < 400:
            self.stats.update({"ok": self.stats["ok"] + 1, "fail_streak": 0, "last_ok": time.time(), "via": how})
            got = summarize(parse_sold(html, q))
            if got:
                res.update(got)
                self.stats["priced"] += 1
        else:
            self._failed(f"eBay HTTP {status}")
            log.info("resale: eBay HTTP %s (%s)", status, how)
        return res

    def health(self):
        """One line for the startup message / status: is eBay answering?"""
        st = self.stats
        if not self.enabled():
            return "💸 Resale values: off (Settings)"
        if st["fail_streak"] >= 5 and not st["ok"]:
            return f"💸 Resale values: ❌ eBay is turning lookups away ({st['last_error']}) - retrying every 30 min"
        if st["ok"]:
            return (f"💸 Resale values: ✅ working ({st['priced']} priced of {st['ok']} looked up since launch"
                    f"{', ' + str(st['fail']) + ' failed' if st['fail'] else ''})")
        return "💸 Resale values: starting (first eBay lookups run in the background)"
