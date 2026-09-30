"""The scanner. Runs in a background thread; the UI reads engine.snapshot()."""
import csv
import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

from . import __version__, paths, settings as settings_mod
from .notify import CHANNELS, ChatBot, Notifier, StatusBoard, store_in
from .parsing import (CT, LIVE_STATUSES, RETAIL_STORES, categorize, extract_when, fmt_when, gcal_link, is_card_product,
                      is_sports_card_product, is_tcg_product, looks_blocked, parse_retail_tiles, parse_topps_calendar,
                      parse_topps_product_page, tile_status)

log = logging.getLogger("rip_radar")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 Edg/129.0")
MAX_ALERTS = 150


def load_builtin():
    return yaml.safe_load(paths.resource("targets.yaml").read_text(encoding="utf-8"))


class Fetcher:
    """Plain HTTP by default. Pages marked browser: true go through the app's hidden browser window
    (real Edge engine) when the app provides one; otherwise plain HTTP."""

    def __init__(self, browser_fetch=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.browser_fetch = browser_fetch

    def get(self, url, browser=False):
        if browser and self.browser_fetch:
            try:
                return self.browser_fetch(url)
            except Exception as e:
                log.warning("browser fetch failed for %s: %s - trying plain HTTP", url, e)
        headers = None
        if "reddit.com" in url:   # Reddit rate-limits generic browser agents; it asks apps to name themselves
            headers = {"User-Agent": f"windows:rip-radar:{__version__} (personal drop alerts)"}
        r = self.s.get(url, timeout=30, allow_redirects=True, headers=headers)
        return r.status_code, r.url, r.text


class Engine:
    def __init__(self, browser_fetch=None):
        paths.ensure_dirs()
        self.builtin = load_builtin()
        self.state = self._load_state()
        self.state.setdefault("alerts", [])
        self.fetcher = Fetcher(browser_fetch)
        self.notify = Notifier(settings_mod.load, on_alert=self._record_alert)
        self.board = StatusBoard(settings_mod.load, self.status_text, self.state)
        self.bot = ChatBot(self.status_text)
        self.started = datetime.now(CT)
        self.last_scan = None
        self.health = {}           # name -> {"health","code","at","type","url"}
        self.topps = self.state.get("topps_products", [])
        self.topps_formats = {k: list(v.values()) for k, v in
                              self.state.get("targets", {}).get("Topps product pages", {}).get("formats", {}).items()}
        self.next_due = {}
        self.first_pass_done = False
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="engine")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._save_state()

    def scan_now(self):
        self.next_due = {}
        self._wake.set()

    def reload(self):
        """Settings changed: re-read targets, restart bot if token changed, rescan."""
        s = settings_mod.load()
        if s.get("bot_token"):
            self.bot.start(s["bot_token"])
        self.scan_now()

    # ------------------------------------------------------------ targets
    def targets(self):
        s = settings_mod.load()
        disabled = set(s.get("disabled_sources", []))
        out = [dict(t) for t in self.builtin["targets"] if t["name"] not in disabled]
        presets = self.builtin.get("watch_presets", {})
        for p in s.get("watch_pages", []):
            if not p.get("enabled", True) or not p.get("url", "").startswith("http"):
                continue
            pre = presets.get(p.get("preset", "custom"), presets.get("custom", {}))
            words = [w.strip() for w in (p.get("keywords") or pre.get("keywords", [])) if w.strip()]
            if not words:
                continue
            out.append({"name": p["name"], "type": "keywords", "url": p["url"], "browser": True,
                        "interval_seconds": 90, "keywords": words, "category": categorize(p["name"]),
                        "alert_title": f"{pre.get('alert', '🚨 PAGE LIVE')}: {p['name']}", "user": True,
                        "channel": pre.get("channel") or store_in(p["url"])})
        for t in out:
            if t["type"] == "topps_calendar":
                t["sports"] = s.get("sports") or ["Baseball", "Basketball", "Football"]
        return out

    # ------------------------------------------------------------ main loop
    def _loop(self):
        s = settings_mod.load()
        if s.get("bot_token"):
            self.bot.start(s["bot_token"])
        default_iv = self.builtin.get("default_interval_seconds", 90)
        while not self._stop.is_set():
            if settings_mod.load().get("paused"):
                self._wake.wait(5)
                self._wake.clear()
                continue
            report = {}
            targets = self.targets()
            pending = {t["name"] for t in targets}
            while pending and not self._stop.is_set():
                # one source at a time, most overdue first (relative to its interval), so fast sources like the
                # Pokémon Center queue stay on time even when slow store pages pile up
                now = time.time()
                due = [t for t in targets if t["name"] in pending and now >= self.next_due.get(t["name"], 0)]
                if not due:
                    break
                t = max(due, key=lambda t: (now - self.next_due.get(t["name"], 0))
                        / t.get("interval_seconds", default_iv))
                pending.discard(t["name"])
                self._run_target(t, report)
                iv = t.get("interval_seconds", default_iv)
                self.next_due[t["name"]] = time.time() + iv * random.uniform(0.85, 1.15)
                self._save_state()
                time.sleep(random.uniform(1, 2.5))
            if self._stop.is_set():
                return
            if report and not self.first_pass_done and len(report) >= len(targets):
                self.first_pass_done = True
                lines = [f"{'✅' if v.startswith('ok') else '❌'} **{k}**: {v}" for k, v in report.items()]
                self.notify.send("system", f"Rip Radar {__version__} started · source check",
                                 desc="\n".join(lines)[:4000])
            try:
                self.board.tick()
            except Exception as e:
                log.warning("status board: %s", e)
            self._wake.wait(5)
            self._wake.clear()

    def _run_target(self, t, report):
        st = self.state.setdefault("targets", {}).setdefault(t["name"], {})
        first = not st.get("initialized")
        prev = st.get("health", "")
        watcher = getattr(self, f"check_{t['type']}", None)
        self.notify.set_channel(t.get("channel"))      # e.g. Pokémon Center -> its own Discord channel
        try:
            health, code = watcher(t, st, first) if watcher else (f"error: unknown type {t['type']}", 0)
        except Exception as e:
            health, code = f"error: {type(e).__name__}: {e}"[:160], 0
            log.exception("%s failed", t["name"])
        finally:
            self.notify.set_channel(None)
        st["initialized"] = True
        st["health"] = health
        now = datetime.now(CT)
        with self._lock:
            self.health[t["name"]] = {"health": health, "code": code, "at": now.isoformat(),
                                      "type": t["type"], "url": t.get("url", ""), "user": t.get("user", False)}
            self.last_scan = now
        report[t["name"]] = f"{health} [HTTP {code}]"
        bad = health.split(" ")[0].rstrip(":") in ("blocked", "error", "empty")
        if self.first_pass_done and bad and prev.split(" ")[:1] != health.split(" ")[:1]:
            self.notify.send("system", f"⚠️ {t['name']} stopped working: {health}", t.get("url", ""))

    # ------------------------------------------------------------ fetching
    def _fetch(self, t, st):
        """Plain HTTP first. If a site turns that away, switch this source to the app's built-in
        browser (real Edge engine) from then on - it gets through most bot walls from a home connection."""
        use_browser = bool(t.get("browser") or st.get("auto_browser"))
        status, final, html = self.fetcher.get(t["url"], use_browser)
        if not use_browser and getattr(self.fetcher, "browser_fetch", None) and looks_blocked(status, html):
            log.info("%s blocked plain HTTP (%s) - switching to the built-in browser", t["name"], status)
            st["auto_browser"] = True
            status, final, html = self.fetcher.get(t["url"], True)
        return status, final, html

    # ------------------------------------------------------------ watchers
    def check_listing(self, t, st, first):
        """Alert on NEW product links appearing on a page (product loaded).
        tcg_only: keep only sealed Pokémon card products. include/exclude: word filters."""
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        soup = BeautifulSoup(html, "html.parser")
        pat = re.compile(t.get("link_pattern", r"/products?/"), re.I)
        want = [k.lower() for k in t.get("include", [])]
        avoid = [k.lower() for k in t.get("exclude", [])]
        found, skipped = {}, 0
        for a in soup.find_all("a", href=True):
            href = urljoin(final, a["href"].split("?")[0].split("#")[0])
            if not pat.search(href):
                continue
            text = " ".join(a.get_text(" ").split()) or a.get("aria-label", "") or href.rsplit("/", 1)[-1]
            blob = (text + " " + href).lower()
            if (want and not any(k in blob for k in want)) or any(k in blob for k in avoid) \
                    or (t.get("tcg_only") and not is_tcg_product(blob)):
                skipped += 1
                continue
            if href not in found or len(text) > len(found[href]):
                found[href] = text
        if not found:
            return ("empty" if not skipped else f"ok (0 card products, {skipped} other items skipped)"), status
        seen = set(st.get("links", []))
        if not first:
            for h in [h for h in found if h not in seen][:10]:
                name = found[h] or h
                fields = {"Source": t["name"], "Category": t.get("category") or categorize(name + " " + h)}
                self._when_fields(name[:120], h, extract_when(name), fields)
                label = "New card product" if t.get("tcg_only") else "New product"
                self.notify.send(t.get("level", "urgent"), f"{label} on {t['name']}: {name[:120]}", h, fields)
        st["links"] = list(seen | set(found))[-3000:]
        return f"ok ({len(found)} {'card ' if t.get('tcg_only') else ''}products)", status

    def check_keywords(self, t, st, first):
        """Alert when a page flips live: queue page, 'Enter drawing', 'Request invite', 'Add to cart'."""
        status, final, html = self._fetch(t, st)
        url_hit = any(k.lower() in (final or "").lower() for k in t.get("url_contains", []))
        if looks_blocked(status, html) and not url_hit:
            return "blocked", status
        if status >= 400 and not url_hit:
            return "error", status
        text = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        hits = [k for k in t.get("keywords", []) if k.lower() in text]
        active = bool(hits) or url_hit
        was = st.get("active")
        if active and was is not True and not (first and t.get("quiet_on_first", False)):
            title = t.get("alert_title", f"{t['name']}: LIVE")
            fields = {"Source": t["name"], "Detected": "queue / waiting room" if url_hit else ", ".join(hits[:4]),
                      "Category": t.get("category", "")}
            self.notify.send(t.get("level", "urgent"), title, t["url"], fields)
        elif was is True and not active and t.get("alert_on_end"):
            self.notify.send("normal", f"{t['name']}: ended", t["url"], {"Source": t["name"]})
        st["active"] = active
        return ("ok · LIVE now" if active else "ok · not live"), status

    def check_feed(self, t, st, first):
        """News / Reddit RSS: new posts about raffles, drawings, invites, queues or drop times."""
        status, final, body = self.fetcher.get(t["url"])
        if status >= 400:
            return ("blocked" if status in (403, 429) else "error"), status
        feed = feedparser.parse(body)
        must = [k.lower() for k in t.get("must_have", [])]
        exclude = [k.lower() for k in t.get("exclude", [])]
        seen = set(st.get("seen", []))
        sent = 0
        for e in feed.entries:
            key = e.get("id") or e.get("link") or e.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            if first:
                continue
            title = e.get("title", "").strip()
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
            blob = f"{title} {summary}".lower()
            if (must and not any(k in blob for k in must)) or any(k in blob for k in exclude):
                continue
            pub = e.get("published_parsed") or e.get("updated_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) < datetime.now(timezone.utc) - timedelta(days=3):
                continue
            link = e.get("link", "")
            fields = {"Source": t["name"], "Category": categorize(blob, t.get("category", ""))}
            self._when_fields(title[:120], link, extract_when(f"{title}. {summary}"), fields)
            level = "urgent" if any(k in blob for k in t.get("urgent_if", [])) else t.get("level", "normal")
            store = store_in(title + " " + summary) if t.get("route_by_store") else None
            if store:
                fields["Store"] = CHANNELS[store]
            self.notify.send(level, title[:240], link, fields, desc=summary[:300], channel=store)
            sent += 1
            if sent >= t.get("max_per_run", 8):
                break
        st["seen"] = list(seen)[-2000:]
        return f"ok ({len(feed.entries)} posts)", status

    def check_retail_search(self, t, st, first):
        """A store's search/category page: every Pokémon / sports-card product on it.
        Pings when a card product is IN STOCK (new and already in stock, or back in stock), with picture,
        price, product link and Add to cart / Buy now links where the store allows them.
        First run only records what's there, so you aren't flooded with everything already in stock."""
        status, final, html = self._fetch(t, st)
        self._save_debug(t["name"], html)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        store = t["store"]
        label = RETAIL_STORES[store]["label"]
        tiles = parse_retail_tiles(html, final, store, t.get("live_if_price", False))
        if not tiles:
            return "empty", status
        keep = {"pokemon": is_tcg_product, "sports": is_sports_card_product}.get(t.get("products", "cards"),
                                                                                 is_card_product)
        cap = float(t.get("max_price", 0) or 0)
        cards = [x for x in tiles if keep(f"{x['name']} {x['url']}")
                 and not (cap and x["price"] and float(x["price"].strip("$").replace(",", "")) > cap)]
        known = st.setdefault("items", {})

        # stores whose search tiles don't show stock: open a few product pages each run to find out
        if t.get("verify_pages"):
            unknown = [x for x in cards if x["status"] == "Listed"]
            unknown.sort(key=lambda x: known.get(x["id"], {}).get("checked", 0))
            for x in unknown[: t.get("verify_pages", 2)]:
                try:
                    ps, pf, ph = self._fetch({"name": t["name"], "url": x["url"], "browser": t.get("browser")}, st)
                    if not looks_blocked(ps, ph) and ps < 400:
                        text = BeautifulSoup(ph, "html.parser").get_text(" ")
                        x["status"], x["live"] = tile_status(text, t.get("live_if_price", False))
                except Exception as e:
                    log.info("verify %s: %s", x["url"], e)
                known.setdefault(x["id"], {})["checked"] = time.time()

        now = time.time()
        for x in cards:
            prev = known.get(x["id"])
            if not first and x["live"] and (prev is None or not prev.get("live")):
                if x["status"].startswith("Drawing"):
                    self._product_alert("urgent", f"🎟️ DRAWING / INVITE OPEN at {label}: {x['name']}", x, label)
                else:
                    what = "NEW & IN STOCK" if prev is None else "BACK IN STOCK"
                    self._product_alert("urgent", f"🟢 {what} at {label}: {x['name']}", x, label)
            elif not first and prev is None and t.get("alert_new_listed"):
                self._product_alert("normal", f"🆕 New at {label} (not in stock yet): {x['name']}", x, label)
            rec = known.setdefault(x["id"], {})
            if x["status"] != "Listed" or "live" not in rec:
                rec["live"] = x["live"]
            rec.update({"name": x["name"], "seen": now})
        if len(known) > 3000:
            for k in sorted(known, key=lambda k: known[k].get("seen", 0))[: len(known) - 3000]:
                known.pop(k)
        live = sum(1 for x in cards if x["live"])
        return f"ok ({len(cards)} card products · {live} in stock · {len(tiles) - len(cards)} other items skipped)", status

    def _product_alert(self, level, title, x, store_label, extra=None):
        fields = {"Price": x.get("price"), "Store": store_label, "Status": x.get("status")}
        fields.update(extra or {})
        self.notify.send(level, title, x["url"], fields, image=x.get("image", ""),
                         links=[("🛒 Add to cart", x.get("add_to_cart")), ("⚡ Buy now", x.get("buy_now")),
                                ("Product page", x["url"])])

    def _save_debug(self, name, html):
        """Last page each source saw - bundled by Settings > Save diagnostics so parsers can be tuned."""
        try:
            d = paths.DATA_DIR / "debug"
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
            (d / f"{safe}.html").write_text((html or "")[:3_000_000], encoding="utf-8", errors="ignore")
        except OSError:
            pass

    def check_topps_calendar(self, t, st, first):
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        # Topps' own page says UTC. If a browser rewrote a time without a zone, it's local (Central).
        via_browser = bool(t.get("browser") or st.get("auto_browser"))
        allp = parse_topps_calendar(html, final, default_tz=CT if via_browser else timezone.utc)
        wanted = set(t.get("sports", ["Baseball", "Basketball", "Football"]))
        products = [p for p in allp if p["sport"] in wanted]
        if not products:
            return ("empty" if not allp else f"ok (0 of {len(allp)} match your sports)"), status
        known, now = st.get("products", {}), datetime.now(CT)
        for p in products:   # near a drop Topps swaps the date for a countdown - keep the last known time
            old = known.get(p["slug"]) or {}
            if not p["when"] and old.get("when"):
                p["when"], p["has_time"] = old["when"], old.get("has_time", False)
        products.sort(key=lambda p: p["when"] or "")
        with self._lock:
            self.topps = products
        self.state["topps_products"] = products
        self._write_topps_csv(products)
        self._save_debug(t["name"], html)

        if first:
            lines = [f"`{p['sport'][:4]}` **{p['name']}** · {self._when_text(p)} · {p['status']}" for p in products]
            self.notify.send("normal", f"Topps calendar: {len(products)} products", t["url"], desc="\n".join(lines))

        for p in products:
            d = datetime.fromisoformat(p["when"]) if p["when"] else None
            fields = {"Sport": p["sport"], "Drops (CT)": self._when_text(p), "Status": p["status"],
                      "Buy / enter": f"[Open on Topps]({p['url']})"}
            if d:
                fields["Calendar"] = f"[Add to Google Calendar]({gcal_link(p['name'], d, p['url'], not p['has_time'])})"
                if d > now - timedelta(days=1):
                    self._add_event(f"Topps: {p['name']}", p["url"], d, p["has_time"])
            old = known.get(p["slug"])
            if first:
                continue
            if old is None:
                self.notify.send("urgent", f"🆕 New on Topps calendar: {p['name']}", p["url"], fields)
                continue
            if p["status"] != old.get("status"):
                if p["status"] in LIVE_STATUSES:
                    self.notify.send("urgent", f"🚨 TOPPS LIVE: {p['name']} · {p['status']}", p["url"], fields)
                else:
                    self.notify.send("normal", f"Topps: {p['name']} ({old.get('status')} → {p['status']})",
                                     p["url"], fields)
            if p["when"] and old.get("when") and p["when"][:16] != old["when"][:16]:
                fields["Was"] = fmt_when(datetime.fromisoformat(old["when"]), old.get("has_time", False))
                self.notify.send("normal", f"📅 Topps date moved: {p['name']}", p["url"], fields)

        # heads-up before each timed drop, and again the minute it opens - both link straight to the page
        lead = int(t.get("remind_minutes_before", 15))
        reminded = set(st.get("reminded", []))
        for p in products:
            if not (p["when"] and p["has_time"]):
                continue
            d = datetime.fromisoformat(p["when"])
            formats = self._format_lines(p["slug"])
            fields = {"Sport": p["sport"], "Drops (CT)": fmt_when(d), "Status": p["status"],
                      "Buy / enter": f"[Open on Topps]({p['url']})"}
            soon_key, open_key = f"{p['slug']}|{p['when'][:16]}", f"{p['slug']}|{p['when'][:16]}|open"
            if soon_key not in reminded and now <= d <= now + timedelta(minutes=lead):
                mins = max(1, int((d - now).total_seconds() // 60))
                self.notify.send("urgent", f"⏰ Topps drop in {mins} min: {p['name']}", p["url"], fields,
                                 desc=formats, links=[("Open on Topps", p["url"])])
                reminded.add(soon_key)
            if open_key not in reminded and d <= now <= d + timedelta(minutes=10):
                self.notify.send("urgent", f"🟢 OPEN NOW on Topps: {p['name']}", p["url"], fields, desc=formats,
                                 links=[("Open on Topps", p["url"])])
                reminded.add(open_key)
        st["reminded"] = list(reminded)[-300:]
        st["products"] = {p["slug"]: {"status": p["status"], "when": p["when"], "has_time": p["has_time"],
                                      "name": p["name"]} for p in products}
        return f"ok ({len(products)} products)", status

    def check_topps_products(self, t, st, first):
        """Open each calendar product's Topps page and track every format on it (Hobby, Mega, Blaster...).
        Alerts when a format is listed and when it goes on sale / pre-order / drawing, with a direct link.
        Pages near their drop time are checked every run; the rest every `idle_minutes`."""
        products = list(self.topps)
        if not products:
            return "ok · waiting for the Topps calendar", 0
        now = datetime.now(CT)
        hot_before, hot_after = timedelta(hours=t.get("hot_hours_before", 2)), timedelta(hours=t.get("hot_hours_after", 6))
        idle = timedelta(minutes=t.get("idle_minutes", 15))
        due_at = st.setdefault("next", {})
        pages = st.setdefault("formats", {})
        seen_slugs = st.setdefault("baselined", [])

        def is_hot(p):
            if p["status"] in LIVE_STATUSES or not p["when"]:
                return True
            d = datetime.fromisoformat(p["when"])
            if p["has_time"]:
                return d - hot_before <= now <= d + hot_after
            return d.date() == now.date()

        queue = [p for p in products if is_hot(p) or now.timestamp() >= due_at.get(p["slug"], 0)]
        queue.sort(key=lambda p: (not is_hot(p), due_at.get(p["slug"], 0)))
        checked, last_status, blocked = 0, 200, 0
        for p in queue[: t.get("max_pages_per_run", 4)]:
            page_t = {"name": t["name"], "url": p["url"], "browser": t.get("browser", False)}
            status, final, html = self._fetch(page_t, st)
            last_status = status
            due_at[p["slug"]] = (now + idle).timestamp()
            if looks_blocked(status, html) or status >= 400:
                blocked += 1
                continue
            checked += 1
            self._save_debug(f"topps-page-{p['slug']}", html)
            formats, page_when = parse_topps_product_page(html, final)
            old = pages.get(p["slug"], {})
            new_map = {f["handle"]: f for f in formats}
            baseline = first or p["slug"] not in seen_slugs
            for f in formats:
                prev = old.get(f["handle"])
                fields = {"Product": p["name"], "Format": f["name"], "Price": f["price"], "Status": f["status"],
                          "Drops (CT)": self._when_text(p), "Buy / enter": f"[Open this format]({f['url']})"}
                if baseline:
                    continue
                links = [("🛒 Add to cart", f.get("add_to_cart")), ("⚡ Buy now", f.get("buy_now")),
                         ("Product page", f["url"])]
                fields.pop("Buy / enter", None)
                if prev is None:
                    level = "urgent" if f["status"] in LIVE_STATUSES or is_hot(p) else "normal"
                    self.notify.send(level, f"🆕 Topps {f['name']} listed · {f['status']}", f["url"], fields,
                                     image=f.get("image", ""), links=links)
                elif f["status"] != prev.get("status"):
                    if f["status"] in LIVE_STATUSES:
                        self.notify.send("urgent", f"🚨 LIVE: {f['name']} · {f['status']}", f["url"], fields,
                                         image=f.get("image", ""), links=links)
                    elif f["status"] == "Sold out":
                        self.notify.send("normal", f"Sold out: {f['name']}", f["url"], fields,
                                         image=f.get("image", ""))
            pages[p["slug"]] = new_map
            if p["slug"] not in seen_slugs:
                seen_slugs.append(p["slug"])
        with self._lock:
            self.topps_formats = {k: list(v.values()) for k, v in pages.items()}
        n_formats = sum(len(v) for v in pages.values())
        if checked == 0 and blocked:
            return "blocked", last_status
        return f"ok ({n_formats} formats across {len(pages)} pages)", last_status

    def _when_text(self, p):
        if not p.get("when"):
            return "Now / date not shown"
        return fmt_when(datetime.fromisoformat(p["when"]), p["has_time"])

    def _format_lines(self, slug):
        fmts = getattr(self, "topps_formats", {}).get(slug, [])
        return "\n".join(f"• [{f['name']}]({f['url']}) {f['price']} · {f['status']}" for f in fmts[:10])

    # ------------------------------------------------------------ calendar + csv
    def _when_fields(self, title, url, when, fields):
        if not when:
            return
        start, has_time, tz_note = when
        fields["When (CT)"] = fmt_when(start, has_time) + tz_note
        fields["Calendar"] = f"[Add to Google Calendar]({gcal_link(title, start, url, all_day=not has_time)})"
        self._add_event(title, url, start, has_time)

    def _add_event(self, title, url, start, has_time):
        events = self.state.setdefault("events", {})
        uid = hashlib.sha1(f"{url}|{start.date()}".encode()).hexdigest()[:16]
        if uid in events and events[uid]["start"] == start.isoformat():
            return
        events[uid] = {"title": title, "url": url, "start": start.isoformat(), "has_time": has_time}
        cutoff = datetime.now(CT) - timedelta(days=14)
        self.state["events"] = {k: v for k, v in events.items() if datetime.fromisoformat(v["start"]) > cutoff}
        self._write_ics()

    def _write_ics(self):
        def esc(s):
            return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rip Radar//EN", "X-WR-CALNAME:Rip Radar Drops"]
        for uid, ev in sorted(self.state["events"].items(), key=lambda kv: kv[1]["start"]):
            start = datetime.fromisoformat(ev["start"])
            lines += ["BEGIN:VEVENT", f"UID:{uid}@ripradar",
                      f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"]
            if ev["has_time"]:
                su = start.astimezone(timezone.utc)
                lines += [f"DTSTART:{su:%Y%m%dT%H%M%SZ}", f"DTEND:{su + timedelta(minutes=30):%Y%m%dT%H%M%SZ}",
                          "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "DESCRIPTION:Drop in 15 minutes",
                          "END:VALARM"]
            else:
                lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
            lines += [f"SUMMARY:{esc(ev['title'])}", f"DESCRIPTION:{esc(ev['url'])}", f"URL:{ev['url']}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        try:
            paths.ICS_FILE.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        except OSError as e:
            log.warning("couldn't write drops.ics: %s", e)

    def _write_topps_csv(self, products):
        try:
            with open(paths.TOPPS_CSV, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Sport", "Product", "Drops (CT)", "Status", "Section", "Link"])
                for p in products:
                    w.writerow([p["sport"], p["name"], self._when_text(p), p["status"], p["section"], p["url"]])
        except OSError as e:
            log.warning("couldn't write topps_calendar.csv (open in Excel?): %s", e)

    # ------------------------------------------------------------ state + status
    def _record_alert(self, alert):
        with self._lock:
            alerts = self.state.setdefault("alerts", [])
            alerts.insert(0, alert)
            del alerts[MAX_ALERTS:]

    def alerts_today(self):
        today = datetime.now(CT).date()
        return sum(1 for a in self.state.get("alerts", [])
                   if a["level"] != "system" and datetime.fromisoformat(a["at"]).astimezone(CT).date() == today)

    def status_text(self, heading="🟢 **Yes, running.**"):
        now = datetime.now(CT)
        mins = int((now - self.started).total_seconds()) // 60
        with self._lock:
            health = dict(self.health)
        ok = [k for k, v in health.items() if v["health"].startswith("ok")]
        bad = [f"❌ {k}: {v['health']}" for k, v in health.items() if not v["health"].startswith("ok")]
        paused = settings_mod.load().get("paused")
        lines = [heading if not paused else "⏸️ **Running, but paused.**",
                 f"Up {mins // 60}h {mins % 60}m · v{__version__}",
                 f"Last scan: {fmt_when(self.last_scan) + ' CT' if self.last_scan else 'starting up'}",
                 f"Sources OK: {len(ok)}/{len(health)} · Alerts today: {self.alerts_today()}"]
        return "\n".join(lines + bad[:8])

    def snapshot(self):
        with self._lock:
            return {
                "version": __version__,
                "started": self.started.isoformat(),
                "last_scan": self.last_scan.isoformat() if self.last_scan else None,
                "paused": settings_mod.load().get("paused", False),
                "health": [{"name": k, **v} for k, v in self.health.items()],
                "alerts": self.state.get("alerts", [])[:100],
                "alerts_today": self.alerts_today(),
                "topps": self.topps,
                "topps_formats": self.topps_formats,
                "bot": self.bot.state,
            }

    def _load_state(self):
        try:
            return json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"targets": {}, "events": {}, "alerts": []}

    def _save_state(self):
        with self._lock:
            data = json.dumps(self.state, indent=1)
        tmp = paths.STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(paths.STATE_FILE)
        except OSError as e:
            log.warning("couldn't save state yet: %s", e)
