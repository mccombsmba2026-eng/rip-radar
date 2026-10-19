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
from .msrp import msrp_text, price_check
from .notify import CHANNELS, STORE_NAMES, ChatBot, LiveBoard, Notifier, StatusBoard, channel_of, store_in
from .parsing import (CT, is_presale, is_raffle, blocked_by, shopify_product_js, name_from_title, focus_match, is_accessory_only, is_card_only, og_image, parse_watch, watch_match, wall_kind, best_time_for, is_topps_sealed, parse_topps_item_page, topps_handle, topps_product_urls,
                      topps_sitemap_numbers, LIVE_STATUSES, RETAIL_STORES, categorize, extract_when, fmt_when, gcal_link, is_card_product,
                      is_etb_or_upc, is_pokemon_product, is_sports_card_product, is_tcg_product, looks_blocked, parse_retail_tiles, parse_topps_calendar,
                      parse_topps_product_page, humanize_handle, tile_status, drawing_window, next_data, sport_of, stock_hint,
                      title_tiles)
from .cards import ProductCards
from . import releases as rel
from .stock import page_data_stock, parse_target_pdp, target_key_in, target_pdp, target_stock, target_stock_text

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
            # right after launch the hidden browser window may still be starting: wait for it rather than falling
            # back to plain HTTP, which gets a store's empty page shell (products are drawn by the browser)
            for _ in range(45):
                try:
                    return self.browser_fetch(url)
                except RuntimeError as e:
                    if "not ready" not in str(e):
                        log.warning("browser fetch failed for %s: %s - trying plain HTTP", url, e)
                        break
                    time.sleep(2)
                except Exception as e:
                    log.warning("browser fetch failed for %s: %s - trying plain HTTP", url, e)
                    break
        headers = None
        if "reddit.com" in url:   # Reddit rate-limits generic browser agents; it asks apps to name themselves
            headers = {"User-Agent": f"windows:rip-radar:{__version__} (personal drop alerts)"}
        r = self.s.get(url, timeout=30, allow_redirects=True, headers=headers)
        return r.status_code, r.url, r.text


class Engine:
    def __init__(self, browser_fetch=None, queue_fetch=None, stock_json=None, extra_browsers=None, resale_fetch=None):
        paths.ensure_dirs()
        self.builtin = load_builtin()
        self.state = self._load_state()
        self.state.setdefault("alerts", [])
        # several hidden browsers scan different sites at the same time (1 + extra_browsers)
        self._fetchers = [Fetcher(browser_fetch)] + [Fetcher(b) for b in (extra_browsers or [])]
        self._tl = threading.local()
        self.queue_fetcher = Fetcher(queue_fetch) if queue_fetch else None   # own browser for the PC queue
        self.stock_json = stock_json          # Target stock asked from inside a browser on target.com
        from .resale import ResaleChecker
        self._resale_posts = {}               # resale key -> {(store, pid): rec} waiting for a value
        self.resale = ResaleChecker(self.state, settings_mod.load, on_result=self._resale_ready,
                                    browser_fetch=resale_fetch)
        self.resale.debug_save = lambda name, html: self._save_debug(name, html)
        self._queue_thread = None
        self.notify = Notifier(settings_mod.load, on_alert=self._record_alert)
        self.board = StatusBoard(settings_mod.load, self.status_text, self.state)
        self.cal_board = LiveBoard(settings_mod.load, "calendar", self.state, "calendar_board_msg", every_minutes=5)
        self.topps_board = LiveBoard(settings_mod.load, "topps_calendar", self.state, "topps_board_msg",
                                     every_minutes=5)
        from .notify import STORE_COLORS
        # store channels: one self-editing message per product (cards.py) instead of a list board
        self._lock = threading.RLock()
        self.cards = ProductCards(settings_mod.load, self.state, lock=self._lock)
        self.old_store_boards = [f"board_{k}" for k in ("target", "walmart", "dicks", "amazon", "bestbuy", "pokemon",
                                                         "topps")]
        # drop calendar = Topps calendar, drawings and products with an announced date (no news / Reddit posts)
        # (and no Topps format pages - Hobby / Mega / cases are shown under their product, not as extra entries)
        self.state["events"] = {k: v for k, v in (self.state.get("events") or {}).items()
                                if (v.get("kind") in ("topps", "drawing") or v.get("product"))
                                and not (v.get("kind") == "topps" and "/products/" in v.get("url", ""))}
        self.drawings_board = LiveBoard(settings_mod.load, "drawings", self.state, "board_drawings", every_minutes=5)
        self.restock_board = LiveBoard(settings_mod.load, "instore", self.state, "board_instore", every_minutes=15)
        self._restock_board_dirty = False
        # 💸 daily eBay price list: own channel if set, else #watch
        self.prices_board = LiveBoard(settings_mod.load, ("prices", "watch"), self.state, "board_prices",
                                      every_minutes=30, color=0x2ECC71)
        self.formats_board = LiveBoard(settings_mod.load, "topps", self.state, "board_topps", every_minutes=5,
                                       color=STORE_COLORS.get("topps"))
        self.sync_at = 0
        self.startup_sync = False
        self.bot = ChatBot(self.status_text, settings_mod.load, self.bot_reply, zip_lookup=self.zip_lookup,
                           drop_mode=lambda hours: self.set_drop_mode(hours), watch=self.handle_watch_command,
                           block=self.block_product, keep=self.keep_product, prices=self.handle_prices_command,
                           restart=self.request_restart)
        self.started = datetime.now(CT)
        self.TOPPS_FAST = {"state": "starting", "fails": 0}
        if not self.state.get("resale_cleanup_1072") and not settings_mod.load().get("resale_check", True):
            # posts carded before 1.0.61 still said "⏳ checking eBay sold listings…" - those lookups never run now
            self.state["resale_cleanup_1072"] = True
            for st in (self.state.get("targets") or {}).values():
                for bucket in ("items", "products"):
                    for rec in (st.get(bucket) or {}).values():
                        if isinstance(rec, dict) and rec.get("resale") in (self.RESALE_WAIT, self.RESALE_FAIL):
                            rec.pop("resale", None)
                            rec["resale_cleared"] = True        # its card is re-edited on the next change
        if not self.state.get("alarm_1068"):
            # texts / ntfy used to fire for every urgent alert: now only the queue going live (M, 10/18)
            self.state["alarm_1068"] = True
            try:
                cur = settings_mod.load()
                if cur.get("sms_for") == ["urgent"]:
                    settings_mod.update({"sms_for": ["queue"]})
            except Exception as e:
                log.warning("sms default: %s", e)
        if not self.state.get("resale_off_1061"):
            # M (10/18): per-product eBay lookups = hundreds a day = eBay blocks. Only the daily price list + `value`.
            self.state["resale_off_1061"] = True
            try:
                settings_mod.update({"resale_check": False})
            except Exception as e:
                log.warning("resale default: %s", e)
        from .stock import GUARD
        GUARD.update({k: v for k, v in (self.state.get("target_guard") or {}).items() if k in ("pause_until", "pauses")})
        self.restart_hook = None              # set by the app: restart Rip Radar (Discord `restart`, queue self-heal)
        self.last_scan = None
        self.health = {}           # name -> {"health","code","at","type","url"}
        self.topps = self.state.get("topps_products", [])
        self.topps_formats = {k: list(v.values()) for k, v in
                              self.state.get("targets", {}).get("Topps product pages", {}).get("formats", {}).items()}
        self.next_due = {}
        self.host_backoff = {}      # site -> (resume_at, times_blocked): give a site that blocked us a rest
        self.first_pass_done = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # every launch (including after an update) syncs by itself: first full scan, then every board is posted
        # fresh - no need to press "Sync all channels"
        self.sync_at, self.startup_sync = time.time(), True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="engine")
        self._thread.start()
        if self.queue_fetcher:
            self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True, name="queue-watch")
            self._queue_thread.start()
        if self.stock_json:
            threading.Thread(target=self._target_tripwire_loop, daemon=True, name="target-tripwire").start()
        threading.Thread(target=self._topps_fast_loop, daemon=True, name="topps-fast").start()

    # ------------------------------------------------------------ Topps fast check: Shopify JSON every 60 s
    TOPPS_FAST = {"state": "starting", "fails": 0}

    def _topps_source(self):
        return next((t for t in self.targets() if t.get("type") == "topps_sitemap"), None)

    def _hot_topps_items(self, now=None):
        """Topps formats worth a 60-second check: not on sale yet AND (dropping within 48 h / just announced /
        🔥 focus or @everyone list / a #watch match)."""
        t = self._topps_source()
        if not t:
            return []
        now = now or time.time()
        terms = [parse_watch(q) for q in settings_mod.load().get("watch_terms") or []]
        known = self.state.get("targets", {}).get(t["name"], {}).get("products") or {}
        out = []
        for h, r in list(known.items()):
            if r.get("live") or r.get("status") in ("Sold out", None, "") and not r.get("loaded_before_stock"):
                continue
            n = r.get("name", "")
            soon = False
            if r.get("when"):
                try:
                    soon = -3600 <= datetime.fromisoformat(r["when"]).timestamp() - now <= 48 * 3600
                except ValueError:
                    pass
            fresh = now - r.get("first_seen", 0) < 3 * 86400 and not r.get("baseline")
            if soon or fresh or r.get("focus") or self._focus(n)["ping"] or any(watch_match(w, n, "topps")
                                                                                for w in terms):
                out.append((h, r))
        return out

    def topps_fast_tick(self, every=60, per_hour=240, http=None):
        """One tiny request per hot Topps format (Shopify's /products/<handle>.js, ~2 KB - no page load).
        The moment one says available, that product page is opened by the Topps scanner right away and the
        normal #topps card + @everyone go out."""
        if settings_mod.load().get("paused") or not self.TOPPS_FAST.get("on", True):
            return 0
        t = self._topps_source()
        if not t:
            return 0
        now = time.time()
        recent = self.__dict__.setdefault("_tfast_log", [])
        recent[:] = [x for x in recent if now - x < 3600]
        checked = self.__dict__.setdefault("_tfast_checked", {})
        http = http or self.__dict__.setdefault("_tfast_http", requests.Session())
        tripped = 0
        for h, r in sorted(self._hot_topps_items(now), key=lambda hr: checked.get(hr[0], 0)):
            if len(recent) >= per_hour or now - checked.get(h, 0) < every:
                continue
            checked[h] = now
            recent.append(now)
            try:
                resp = http.get(f"https://www.topps.com/products/{h}.js", timeout=12,
                                headers={"User-Agent": UA, "Accept": "application/json"})
                code, body = resp.status_code, resp.text
            except requests.RequestException as e:
                code, body = 0, str(e)
            info = shopify_product_js(body) if code and code < 400 else None
            if info is None:
                self.TOPPS_FAST["fails"] += 1
                self.TOPPS_FAST["state"] = f"❌ topps.com answered {code or 'nothing'}"
                if code in (403, 429) or self.TOPPS_FAST["fails"] >= 10:
                    # turned away: stop for 30 min so we never hammer it; the normal page checks carry on
                    self.TOPPS_FAST["resume"] = now + 1800
                    self.TOPPS_FAST["on"] = False
                    log.info("Topps fast check paused 30 min (HTTP %s)", code)
                    break
                continue
            self.TOPPS_FAST.update({"fails": 0, "state": "✅ working", "at": now})
            if info["available"] and not r.get("live"):
                tripped += 1
                log.info("Topps fast check: %s is available - opening it now", r.get("name", h)[:60])
                r["force"] = True
                self.next_due[t["name"]] = 0
                self._wake.set()
        return tripped

    def _topps_fast_loop(self):
        while not self._stop.is_set():
            try:
                if not self.TOPPS_FAST.get("on", True) and time.time() >= self.TOPPS_FAST.get("resume", 0):
                    self.TOPPS_FAST["on"], self.TOPPS_FAST["fails"] = True, 0
                self.topps_fast_tick()
            except Exception as e:
                log.warning("topps fast check: %s", e)
            self._stop.wait(15)

    def topps_fast_health(self):
        hot = len(self._hot_topps_items())
        st = self.TOPPS_FAST.get("state", "starting")
        if not self.TOPPS_FAST.get("on", True):
            st += " · paused 30 min, normal Topps checks still running"
        return f"⚡ Topps fast check: {st} · {hot} upcoming format{'s' if hot != 1 else ''} checked every 60 s"

    # ------------------------------------------------------------ Target tripwire: hot items checked every 90 s
    def _hot_target_items(self):
        """Target products worth watching closely: ETB / UPC, 🔥 focus / @everyone list, or a #watch match - that
        aren't in stock right now."""
        terms = [parse_watch(q) for q in settings_mod.load().get("watch_terms") or []]
        out, now = [], time.time()
        for t in self.targets():
            if t.get("type") != "retail_search" or t.get("store") != "target":
                continue
            for pid, r in (self.state.get("targets", {}).get(t["name"], {}).get("items") or {}).items():
                if r.get("live") or not r.get("name") or now - r.get("seen", 0) > 86400:
                    continue
                n = r["name"]
                if is_etb_or_upc(n) or self._focus(n)["ping"] or r.get("focus") or any(watch_match(w, n, "target")
                                                                                       for w in terms):
                    out.append((pid, r))
        return out

    def target_tripwire_tick(self, every=300, per_hour=24):
        """A tiny stock check (one small request, no page load) on each hot Target item every ~90 s. When one flips
        to in stock, Target's search pages are re-scanned right away - the normal pipeline does the ping."""
        if settings_mod.load().get("paused"):
            return 0
        now = time.time()
        recent = self.__dict__.setdefault("_trip_log", [])
        recent[:] = [x for x in recent if now - x < 3600]
        checked = self.__dict__.setdefault("_trip_checked", {})
        tripped = 0
        for pid, r in sorted(self._hot_target_items(), key=lambda pr: checked.get(pr[0], 0)):
            if len(recent) >= per_hour or now - checked.get(pid, 0) < every:
                continue
            checked[pid] = now
            recent.append(now)
            info = target_stock(getattr(self.fetcher, "s", None) or requests.Session(), pid, "",
                                self.state.get("target_key", ""), browser_json=self.stock_json)
            if not info:
                continue
            status = (info.get("online_status") or "").upper()
            if (info.get("online") or 0) > 0 or status in ("IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE"):
                tripped += 1
                log.info("tripwire: Target %s looks in stock - rescanning Target now", r["name"][:60])
                for t in self.targets():
                    if t.get("store") == "target":
                        self.next_due[t["name"]] = 0
                self._wake.set()
                break
        return tripped

    # ------------------------------------------------------------ Target ID watch: `watch tcin 12345678` in #watch
    TCIN_PHASES = ["not loaded", "loaded", "drawing", "in stock"]
    MAX_TCINS = 15

    @staticmethod
    def tcin_in(text):
        """'watch tcin 94681234' / a target.com link (…/A-94681234) / a bare 8-digit number -> '94681234' or ''."""
        m = re.search(r"(?:\btcin\s*#?\s*|/A-)(\d{6,10})\b", text or "", re.I) or \
            re.fullmatch(r"\s*(\d{8,9})\s*", text or "")
        return m.group(1) if m else ""

    def _tcin_check(self, tcin):
        """One look at a Target product ID -> (phase, info). Two small requests from the hidden Target window."""
        status, data = target_pdp(tcin, browser_json=self.stock_json,
                                  session=getattr(self.fetcher, "s", None) or requests.Session(),
                                  key=self.state.get("target_key", ""))
        info = parse_target_pdp(data, status)
        info["status_code"] = status
        if not info["loaded"]:
            known = status in (404, 410) or (status == 200 and data is not None)
            return ("not loaded" if known else "unknown"), info
        stock = target_stock(getattr(self.fetcher, "s", None) or requests.Session(), tcin, "",
                             self.state.get("target_key", ""), browser_json=self.stock_json)
        st_txt = ((stock or {}).get("online_status") or "").upper()
        live = bool(stock) and ((stock.get("online") or 0) > 0 or st_txt in ("IN_STOCK", "LIMITED_STOCK",
                                                                             "PRE_ORDER_SELLABLE"))
        info["online"] = (stock or {}).get("online")
        info["online_status"] = st_txt.replace("_", " ").title()
        return ("in stock" if live else "drawing" if info["drawing"] else "loaded"), info

    def tcin_watch_tick(self, now=None):
        """Every watched Target ID, about every 90 s (slower when many are watched: ≤ ~240 requests/h in total).
        @everyone in #watch when one moves up: not loaded -> LOADED -> DRAWING -> IN STOCK."""
        tcins = list(settings_mod.load().get("watch_tcins") or [])[: self.MAX_TCINS]
        if not tcins or settings_mod.load().get("paused"):
            return 0
        now = now or time.time()
        every = max(90, 30 * len(tcins))
        watch = self.state.setdefault("tcin_watch", {})
        posted = 0
        for tcin in tcins:
            rec = watch.setdefault(tcin, {})
            if now - rec.get("checked", 0) < every:
                continue
            rec["checked"] = now
            phase, info = self._tcin_check(tcin)
            if phase == "unknown":                    # Target didn't answer: try again next round, no change
                continue
            if info.get("loaded"):
                rec.update({k: info[k] for k in ("name", "price", "image", "limit", "street_date") if info.get(k)})
            prev = rec.get("phase")
            rec["phase"] = phase
            if prev is None:
                continue                              # first look = baseline (the command already said where it is)
            order = self.TCIN_PHASES
            if order.index(phase) > order.index(prev) or (phase == "in stock" and prev != "in stock"):
                if blocked_by(rec.get("name", ""), self._blocklist()):
                    continue
                self._tcin_alert(tcin, rec, phase, info)
                posted += 1
        return posted

    def _tcin_alert(self, tcin, rec, phase, info):
        url = f"https://www.target.com/p/-/A-{tcin}"
        name = rec.get("name") or f"Target item {tcin}"
        head = {"loaded": "🆕 LOADED ON TARGET (watched ID)", "drawing": "🎟️ TARGET DRAWING / INVITE SET UP",
                "in stock": "🟢 IN STOCK AT TARGET (watched ID)"}[phase]
        fields = {"TCIN": tcin, "Price": rec.get("price"), "Limit": rec.get("limit"),
                  "Release date": rec.get("street_date"),
                  "Online": (f"{info['online']} available" if info.get("online") else info.get("online_status")),
                  "Drawing text": info.get("drawing_text") if phase == "drawing" else ""}
        self.notify.send("urgent", f"{head}: {name}"[:250], url, fields, image=rec.get("image", ""),
                         links=[("Open on Target", url)], store="target", product=True, ping=True, channel="watch",
                         copy_to=["drawings"] if phase == "drawing" else None)

    def _tcin_line(self, tcin, phase, info):
        icon = {"not loaded": "⚪", "loaded": "🆕", "drawing": "🎟️", "in stock": "🟢", "unknown": "❔"}[phase]
        name = info.get("name") or "(nothing at this ID yet)"
        price = f" · {info['price']}" if info.get("price") else ""
        return f"{icon} `{tcin}` **{phase}** · {name[:80]}{price}"

    def _target_tripwire_loop(self):
        while not self._stop.is_set():
            try:
                self.target_tripwire_tick()
            except Exception as e:
                log.warning("target tripwire: %s", e)
            try:
                self.tcin_watch_tick()
            except Exception as e:
                log.warning("target id watch: %s", e)
            self._stop.wait(20)

    @property
    def fetcher(self):
        """The browser this thread scans with (each parallel scanner thread gets its own)."""
        return getattr(self._tl, "fetcher", None) or self._fetchers[0]

    @fetcher.setter
    def fetcher(self, value):          # (tests swap in a fake; all scanner slots then use it)
        self._fetchers = [value]

    def _run_batch(self, batch, report):
        """Run up to len(browsers) sources at once - each on its own hidden browser, never two on the same site."""
        if len(batch) == 1:
            self._run_target(batch[0], report)
            return

        def work(t, f):
            self._tl.fetcher = f
            try:
                self._run_target(t, report)
            except Exception as e:
                log.warning("%s: %s", t["name"], e)
            finally:
                self._tl.fetcher = None
        threads = [threading.Thread(target=work, args=(t, self._fetchers[i]), daemon=True, name=f"scan-{i}")
                   for i, t in enumerate(batch)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(240)

    # ------------------------------------------------------------ Pokémon Center queue: its own loop + browser
    def drop_mode(self):
        """-> seconds left in drop mode (0 = off). Drop mode: queue checked every 30 s, Pokémon Center product
        scanners paused so the queue check is the only Pokémon Center traffic (fewer bot walls)."""
        return max(0, int(float(settings_mod.load().get("drop_mode_until") or 0) - time.time()))

    QUEUE_WINDOW = {"days": [0, 1, 2, 3, 4], "start": 9, "end": 15}

    def queue_window(self, now=None):
        """Pokémon Center's usual drop window (tcgdropradar's log: weekdays ~10 AM-4 PM ET, never weekends, 2026
        median 11:12 AM CT) = weekdays 9 AM-3 PM CT by default (Settings `queue_window`, "days": [] = off). Inside it
        Pokémon Center product scans rest, so the once-a-minute queue check is the only Pokémon Center traffic."""
        w = settings_mod.load().get("queue_window", self.QUEUE_WINDOW)
        if not w:
            return False
        now = now or datetime.now(CT)
        return now.weekday() in w.get("days", []) and int(w.get("start", 9)) <= now.hour < int(w.get("end", 15))

    def _queue_count(self, blocked):
        """How many Pokémon Center queue checks got through, per day (and inside the drop window)."""
        day = datetime.now(CT).date().isoformat()
        qc = self.state.setdefault("queue_checks", {})
        d = qc.setdefault(day, {"n": 0, "blocked": 0, "win_n": 0, "win_blocked": 0})
        d["n"] += 1
        d["blocked"] += bool(blocked)
        if self.queue_window():
            d["win_n"] += 1
            d["win_blocked"] += bool(blocked)
        for k in sorted(qc)[:-14]:
            qc.pop(k, None)

    def _queue_day_line(self, days_ago):
        day = (datetime.now(CT) - timedelta(days=days_ago)).date().isoformat()
        d = self.state.get("queue_checks", {}).get(day)
        if not d or not d["n"]:
            return ""
        ok = d["n"] - d["blocked"]
        out = f"{ok} of {d['n']} checks got through ({100 * ok // d['n']}%)"
        if d.get("win_n"):
            wok = d["win_n"] - d["win_blocked"]
            out += f" · in the drop window {wok} of {d['win_n']} ({100 * wok // d['win_n']}%)"
        return out

    def queue_reliability(self):
        d = self.state.get("queue_checks", {}).get(datetime.now(CT).date().isoformat())
        if not d or not d["n"]:
            return ""
        ok = d["n"] - d["blocked"]
        line = f"today {ok} of {d['n']} checks got through ({100 * ok // d['n']}%)"
        if d.get("win_n"):
            wok = d["win_n"] - d["win_blocked"]
            line += f" · drop window {wok} of {d['win_n']} ({100 * wok // d['win_n']}%)"
        return line

    def set_drop_mode(self, hours):
        until = time.time() + float(hours) * 3600 if hours else 0
        settings_mod.update({"drop_mode_until": until})
        self._wake.set()
        return self.drop_mode()

    def _queue_loop(self):
        """Checks the queue on its own timer in its own browser window, every 60 s (30 s in drop mode)."""
        while not self._stop.is_set():
            started = time.time()
            if not settings_mod.load().get("paused"):
                for t in [t for t in self.targets() if t.get("track_duration")]:
                    try:
                        self._run_target(t, {})
                    except Exception as e:
                        log.warning("queue watch: %s", e)
            fast = bool(self.drop_mode())
            # 1.0.66 (M: "it's gonna get the IP banned on PKC"): never faster than once a minute unless M turns on
            # drop mode himself; overnight quiet hours (2-6 AM) every 2 min
            every = 30 if fast else 60 * self._slow_factor()
            try:
                self._queue_self_heal()
            except Exception as e:
                log.warning("queue self-heal: %s", e)
            streak = self._queue_blocked_streak()
            if streak >= 3:
                # Pokémon Center keeps turning the check away: check less often so its bot check cools off
                # (the product scans there are already paused) - hammering it keeps us blocked longer
                # 1.0.71: a long block (Imperva "Request unsuccessful" on 10/18 for 1.5 h+) -> every 5 min outside
                # the weekday drop window, so we stop feeding the block; TCG Drop Radar is the backup meanwhile
                every = 60 if fast else (120 if streak < 8 else (180 if self.queue_window() else 300))
            self._stop.wait(max(5, every - (time.time() - started)))

    SELF_HEAL_STREAK = 5          # ~10 min of blocked checks (they slow to 2 min apart once blocked)
    SELF_HEAL_EVERY = 45 * 60     # at most one automatic restart per 45 min

    def _queue_self_heal(self):
        """Pokémon Center keeps turning the queue check away -> restart Rip Radar (a fresh start cleared it every
        time on 10/14). Never while a queue is live, at most once per 45 min, and the restart is announced."""
        if not self.restart_hook or self._queue_blocked_streak() < self.SELF_HEAL_STREAK:
            return False
        t = next((t for t in self.targets() if t.get("track_duration") and "pokemoncenter" in t.get("url", "")), None)
        if not t or self.state.get("targets", {}).get(t["name"], {}).get("active"):
            return False
        if time.time() - self.state.get("last_self_heal", 0) < self.SELF_HEAL_EVERY:
            return False
        self.state["last_self_heal"] = time.time()
        self.state.setdefault("targets", {}).setdefault(t["name"], {})["blocked_streak"] = 0
        self._save_state()
        self.notify.send("system", "♻️ Restarting Rip Radar to clear Pokémon Center's block on the queue check",
                         "https://www.pokemoncenter.com/",
                         {"Why": f"{self.SELF_HEAL_STREAK} queue checks in a row were turned away",
                          "Back in": "about 1-2 minutes (a fresh start cleared it every time yesterday)"})
        threading.Thread(target=self._restart_soon, daemon=True).start()
        return True

    def _restart_soon(self, delay=3):
        time.sleep(delay)
        try:
            self.restart_hook()
        except Exception as e:
            log.warning("restart: %s", e)

    def request_restart(self):
        """Discord `restart` (urgent channel only)."""
        if not self.restart_hook:
            return "Restart only works in the installed app."
        self._save_state()
        threading.Thread(target=self._restart_soon, daemon=True).start()
        return "♻️ Restarting Rip Radar now · back in about 1-2 minutes (#app-status will say when it's running)."

    def _queue_blocked_streak(self):
        t = next((t for t in self.targets() if t.get("track_duration") and "pokemoncenter" in t.get("url", "")), None)
        return (self.state.get("targets", {}).get(t["name"], {}).get("blocked_streak", 0)) if t else 0

    def _apply_keep_awake(self):
        """Settings → 'Keep this PC awake while Rip Radar runs' (OFF unless M turns it on). Applied from the scanner
        thread, which lives as long as the app; switching it off releases the request right away."""
        want = bool(settings_mod.load().get("keep_awake", False))
        if want != getattr(self, "_awake_on", False):
            from .winsys import keep_awake
            keep_awake(want)
            self._awake_on = want

    def _queue_has_own_loop(self):
        return bool(self._queue_thread and self._queue_thread.is_alive())

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
        out = [dict(t) for t in self.builtin["targets"] if t["name"] not in disabled and not t.get("disabled")]
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
            if t["type"] == "retail_search" and len(getattr(self, "_fetchers", [])) > 1 and t.get("store") != "pokemon":
                # 3 browsers in parallel: store pages come round about twice as often (never under 2 min)
                t["interval_seconds"] = max(120, int(t.get("interval_seconds", 300) * 0.5))
            if t["type"] == "topps_calendar":
                t["sports"] = s.get("sports") or ["Baseball", "Basketball", "Football"]
                t["all_products"] = s.get("topps_all_products", True)
        return out

    # ------------------------------------------------------------ main loop
    def _loop(self):
        s = settings_mod.load()
        if s.get("bot_token"):
            self.bot.start(s["bot_token"])
        default_iv = self.builtin.get("default_interval_seconds", 90)
        while not self._stop.is_set():
            self._apply_keep_awake()
            self._resale_watchdog()
            if time.time() - getattr(self, "_last_purge", 0) > 6 * 3600:
                try:
                    self.purge_unwanted_cards()
                    threading.Thread(target=self.cards.react_existing, daemon=True, name="react-old").start()
                except Exception as e:
                    log.warning("cleanup: %s", e)
            if settings_mod.load().get("paused"):
                self._wake.wait(5)
                self._wake.clear()
                continue
            report = {}
            pass_started = time.time()
            # Pokémon Center product scans rest during drop mode, the weekday drop window (the queue check is then our
            # ONLY Pokémon Center traffic: 60 requests an hour) and while the queue check is being turned away
            pc_rest = (self.drop_mode() or self.queue_window() or self._queue_blocked_streak() >= 2
                       or time.time() < self.state.get("pc_cooldown_until", 0))
            targets = [t for t in self.targets()
                       if not (t.get("track_duration") and self._queue_has_own_loop())
                       and not (pc_rest and t.get("store") == "pokemon")]
            pending = {t["name"] for t in targets}
            while not self._stop.is_set():
                # one source at a time, most overdue first (relative to its interval), so fast sources like the
                # Pokémon Center queue stay on time even when slow store pages pile up
                now = time.time()
                # the queue watcher may run again mid-pass: a full pass takes minutes, the queue can't wait that long
                due = [t for t in targets if (t["name"] in pending or t.get("track_duration"))
                       and now >= self.next_due.get(t["name"], 0)
                       and (t["type"] == "walmart_drawings" or t.get("track_duration")   # never paused
                            or (now >= self.host_backoff.get(self._host(t), (0, 0))[0]
                                and now >= self.host_backoff.get(self._site(t.get("url", "")), (0, 0))[0]
                                and not self._over_budget(t)
                                and not self._site_banned(t)))]
                if not due:
                    break
                due.sort(key=lambda t: -(now - self.next_due.get(t["name"], 0)) / t.get("interval_seconds", default_iv))
                batch, hosts = [], set()
                for t in due:                      # most overdue first, one per site, as many as there are browsers
                    if len(batch) >= len(self._fetchers):
                        break
                    if self._host(t) not in hosts:
                        batch.append(t)
                        hosts.add(self._host(t))
                for t in batch:
                    pending.discard(t["name"])
                self._run_batch(batch, report)
                self._clock_ticks()
                for t in batch:
                    iv = t.get("interval_seconds", default_iv)
                    if t["type"] not in ("walmart_drawings",) and not t.get("track_duration"):
                        iv *= self._slow_factor()
                    self.next_due[t["name"]] = time.time() + iv * random.uniform(0.85, 1.15)
                self._save_state()
                time.sleep(random.uniform(1, 2.5))
            if self._stop.is_set():
                return
            if self.sync_at and report and pass_started >= self.sync_at:
                self.sync_at = 0
                self.first_pass_done = True
                self._boards_tick(repost=True)       # fresh board at the bottom of every channel
                if getattr(self, "startup_sync", False):
                    self.startup_sync = False
                    ok = sum(1 for v in report.values() if v.startswith("ok"))
                    bad = [f"❌ **{k}**: {v}" for k, v in report.items() if not v.startswith("ok")]
                    self.notify.send("system", f"🟢 Rip Radar {__version__} is running · all channels synced",
                                     desc=f"{ok}/{len(report)} sources working.\n{self.stock_health()}\n{self.topps_fast_health()}\n"
                                          f"{self.resale.health()}\n"
                                          + "\n".join(bad)[:3400])
                else:
                    self.notify.send("normal", "🔄 All channels synced", desc="Each channel has a fresh board with "
                                     "its current state. They keep themselves up to date.", channel="status")
            if report and not self.first_pass_done and len(report) >= len(targets):
                self.first_pass_done = True
                lines = [f"{'✅' if v.startswith('ok') else '❌'} **{k}**: {v}" for k, v in report.items()]
                self.notify.send("system", f"Rip Radar {__version__} started · source check",
                                 desc="\n".join(lines)[:4000])
            self._clock_ticks()
            try:
                self.recap_tick()
                self.health_tick()
            except Exception as e:
                log.warning("recap / health: %s", e)
            try:
                self.price_list_tick()
            except Exception as e:
                log.warning("price list: %s", e)
            try:
                self.board.tick()
                self.calendar_tick()
            except Exception as e:
                log.warning("boards: %s", e)
            self._wake.wait(5)
            self._wake.clear()

    @staticmethod
    def _host(t):
        from urllib.parse import urlparse
        return urlparse(t.get("url", "")).netloc.lower().removeprefix("www.")

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
        st.setdefault("since", time.time())
        if health.startswith("ok"):           # for the daily health check: working vs actually finding something
            st["last_ok_at"] = time.time()
            m = re.match(r"ok \((\d+)", health)
            if not m or int(m.group(1)) > 0:
                st["last_found_at"] = time.time()
        now = datetime.now(CT)
        with self._lock:
            self.health[t["name"]] = {"health": health, "code": code, "at": now.isoformat(),
                                      "type": t["type"], "url": t.get("url", ""), "user": t.get("user", False)}
            self.last_scan = now
        report[t["name"]] = f"{health} [HTTP {code}]"
        host = self._host(t)
        if health.startswith("blocked") and t["type"] != "feed" and not t.get("track_duration"):
            n = self.host_backoff.get(host, (0, 0))[1] + 1        # 5, 10, 20, then 30 min max
            self.host_backoff[host] = (time.time() + min(1800, 300 * 2 ** (n - 1)), n)
            log.info("%s blocked us - pausing it for %d min", host, min(30, 5 * 2 ** (n - 1)))
        elif health.startswith("ok"):
            self.host_backoff.pop(host, None)
        bad = health.split(" ")[0].rstrip(":") in ("blocked", "error", "empty")
        # one bad check is normal (sites hiccup): only say something once it has failed for 30+ minutes straight
        if bad:
            st.setdefault("bad_since", time.time())
            if self.first_pass_done and not st.get("bad_alerted") and time.time() - st["bad_since"] >= 1800:
                st["bad_alerted"] = True
                self.notify.send("system", f"⚠️ {t['name']} hasn't worked for 30+ min: {health}", t.get("url", ""))
        else:
            if st.pop("bad_alerted", None):
                self.notify.send("system", f"✅ {t['name']} is working again", t.get("url", ""))
            st.pop("bad_since", None)

    # ------------------------------------------------------------ fetching
    def _fetch(self, t, st):
        """Plain HTTP first. If a site turns that away, switch this source to the app's built-in
        browser (real Edge engine) from then on - it gets through most bot walls from a home connection."""
        use_browser = bool(t.get("browser") or st.get("auto_browser"))
        self._note_request(t["url"])
        status, final, html = self.fetcher.get(t["url"], use_browser)
        if not use_browser and getattr(self.fetcher, "browser_fetch", None) and looks_blocked(status, html):
            log.info("%s blocked plain HTTP (%s) - switching to the built-in browser", t["name"], status)
            st["auto_browser"] = True
            self._note_request(t["url"])
            status, final, html = self.fetcher.get(t["url"], True)
        if getattr(getattr(self.fetcher, "browser_fetch", None), "last_challenge", False):
            self._ease_off(t["url"], "made the scanner wait on a bot check")
        if self._note_ban(t, html):
            return 403, final, html          # counts as blocked, not "empty"
        if "pokemoncenter.com" in t.get("url", "") and looks_blocked(status, html):
            self.state["pc_cooldown_until"] = time.time() + 3600   # PC product scans rest an hour (even over restarts)
        return status, final, html

    # ------------------------------------------------------------ staying under the radar (one home IP, no proxies)
    SITE_BUDGET = {"default": 150, "topps.com": 240, "target.com": 150, "walmart.com": 150,
                   "pokemoncenter.com": 160, "amazon.com": 120}

    @staticmethod
    def _site(url):
        from urllib.parse import urlparse
        host = urlparse(url or "").netloc.lower().removeprefix("www.")
        return ".".join(host.split(".")[-2:]) if host else ""

    def _note_request(self, url):
        from collections import deque
        log_ = self.__dict__.setdefault("_req_log", {})
        q = log_.setdefault(self._site(url), deque())
        now = time.time()
        q.append(now)
        while q and now - q[0] > 3600:
            q.popleft()

    def _requests_last_hour(self, url_or_site):
        site = url_or_site if "/" not in url_or_site else self._site(url_or_site)
        q = self.__dict__.get("_req_log", {}).get(site)
        now = time.time()
        return sum(1 for x in (q or []) if now - x <= 3600)

    def _budget(self, site):
        b = dict(self.SITE_BUDGET)
        b.update(settings_mod.load().get("site_budgets") or {})
        return int(b.get(site, b["default"]))

    RE_BAN = re.compile(r"try again (?:after|in) (\d{1,2}) hours?", re.I)

    def _note_ban(self, t, html):
        """A site's own 'come back in N hours' page (Dick's, 10/18: 'Site Unavailable ... Please try again after 12
        hours ... IP: <M's IP>') = it has blocked this home connection. Stop asking that site for those hours - every
        request while banned can extend it - and say so once."""
        m = self.RE_BAN.search(BeautifulSoup(html or "", "html.parser").get_text(" ")[:5000]) if len(html or "") < 20000 else None
        if not m:
            return False
        site = self._site(t.get("url", ""))
        hours = min(24, max(1, int(m.group(1))))
        bans = self.state.setdefault("site_bans", {})
        if bans.get(site, 0) > time.time():
            return True
        bans[site] = time.time() + hours * 3600
        until = datetime.now(CT) + timedelta(hours=hours)
        self.notify.send("system", f"⛔ {STORE_NAMES.get(t.get('store'), site)} has blocked your home internet for "
                                   f"{hours} h", t.get("url", ""),
                         {"Rip Radar stops checking it until": f"{fmt_when(until)} CT",
                          "Why": "Its own page says to try again in a few hours. Asking again while blocked can make "
                                 "the block longer, so the app leaves it alone."})
        return True

    def _site_banned(self, t):
        return self.state.get("site_bans", {}).get(self._site(t.get("url", "")), 0) > time.time()

    def _over_budget(self, t):
        """A site's hourly page budget is spent: hold its sources until the oldest page ages out (drawings exempt)."""
        if t["type"] == "walmart_drawings" or t.get("track_duration"):
            return False
        site = self._site(t.get("url", ""))
        if self._requests_last_hour(site) < self._budget(site):
            return False
        said = self.state.setdefault("budget_notice", {})
        if time.time() - said.get(site, 0) > 6 * 3600:
            said[site] = time.time()
            self.notify.send("system", f"⏳ Easing off {site}: hit its hourly limit ({self._budget(site)} pages)",
                             desc="To keep your home connection from getting flagged, Rip Radar slows that one site "
                                  "down until the hour rolls over. Everything else keeps going.")
        return True

    def _ease_off(self, url, why):
        """First sign of trouble (a bot check we had to wait through): give that site a 3-minute breather."""
        site = self._site(url)
        if not site or site == "pokemoncenter.com" and settings_mod.load().get("drop_mode_until", 0) > time.time():
            return
        resume, n = self.host_backoff.get(site, (0, 0))
        self.host_backoff[site] = (max(resume, time.time() + 180), n)
        log.info("%s %s - easing off for 3 min", site, why)

    def _slow_factor(self):
        """Quiet hours (Settings, default 2-6 AM CT): store pages come round half as often overnight."""
        q = settings_mod.load().get("quiet_hours", [2, 6])
        if not q or len(q) != 2:
            return 1
        h = datetime.now(CT).hour
        a, b = int(q[0]), int(q[1])
        return 2 if (a <= h < b if a <= b else (h >= a or h < b)) else 1

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
                    or (t.get("tcg_only") and (not is_tcg_product(blob) or is_accessory_only(blob))):
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
                label = t.get("label") or ("New card product" if t.get("tcg_only") else "New product")
                if blocked_by(name, self._blocklist()):
                    continue
                self.notify.send(t.get("level", "urgent"), f"{label} on {t['name']}: {name[:120]}", h, fields,
                                 ping=bool(t.get("ping")), copy_to=t.get("copy_to"),
                                 links=[(t.get("link_label", "Open"), h)] if t.get("link_label") else None)
        if t.get("debug"):
            self._save_debug(t["name"], html)
        st["links"] = list(seen | set(found))[-3000:]
        return f"ok ({len(found)} {'card ' if t.get('tcg_only') else ''}products)", status

    def _queue_signals(self, t, final, html):
        """-> (active, what was seen). A queue page = the address moved to a queue / waiting room, or the page is
        a short waiting-room page with queue wording (a normal homepage that merely mentions the virtual queue
        in a banner or footer doesn't count)."""
        url_hit = any(k.lower() in (final or "").lower() for k in t.get("url_contains", []))
        text = " ".join(BeautifulSoup(html or "", "html.parser").get_text(" ").split()).lower()
        hits = [k for k in t.get("keywords", []) if k.lower() in text]
        active = url_hit or (bool(hits) and (len(text) < 8000 or len(hits) >= 2))
        return active, ("queue page address: " + (final or "")[:80]) if url_hit else ", ".join(hits[:4])

    def check_queue(self, t, st, first):
        """Pokémon Center queue: checked every minute, never paused for backoff. If the built-in browser gets a
        bot wall, a plain request gets a second look (a queue redirect shows up in the address either way).
        A blocked check is 'unknown', never 'no queue'."""
        now = datetime.now(CT)
        if self.queue_fetcher:
            status, final, html = self.queue_fetcher.get(t["url"], True)
        else:
            status, final, html = self._fetch(t, st)
        blocked = (looks_blocked(status, html) or status >= 400)
        active, detected = self._queue_signals(t, final, html)
        if blocked and not active:
            try:
                s2, f2, h2 = self.fetcher.get(t["url"], False)
                a2, d2 = self._queue_signals(t, f2, h2)
                if a2 or not (looks_blocked(s2, h2) or s2 >= 400):
                    blocked, active, detected, status, html = False, a2, d2, s2, h2
            except Exception as e:
                log.info("queue second look: %s", e)
        was = st.get("active")
        site, slug = self._queue_site(t)
        self._save_debug(f"{slug}-queue-last-check", f"<!-- {final} -->\n" + (html or ""))
        if slug == "pokemon-center":                 # bot-check posts: Pokémon Center only
            self._watch_bot_check(t, st, wall_kind(status, html) if (blocked and not active) else "", now)
            self._queue_count(blocked and not active)
        if active:
            self._save_debug(f"{slug}-queue-page", f"<!-- {final} -->\n" + (html or ""))
        if blocked and not active:
            st["blocked_streak"] = st.get("blocked_streak", 0) + 1
            if slug == "pokemon-center":
                self.state["pc_cooldown_until"] = time.time() + 3600
            if was is not True:
                self._queue_watch(t, st, f"⚠️ **Couldn't check the {site} queue** at {self._clock(now)} CT · the "
                                         f"site turned the check away. Retrying every minute.")
            return ("blocked" if looks_blocked(status, html) else "error"), status
        st["blocked_streak"] = 0
        self._track_queue(t, st, active, was, detected)
        st["active"] = active
        since = st.get("live_since")
        if active and since:
            return f"ok · LIVE for {self._dur(now - datetime.fromisoformat(since))}", status
        return "ok · not live", status

    def _watch_bot_check(self, t, st, kind, now):
        """Posts (no @everyone) when Pokémon Center switches its bot check / captcha on or off for the checker:
        sites often tighten it right before a drop. Needs 3 checks in a row to count, and 20 min between posts."""
        prev, streak = st.get("wall", ""), st.get("wall_streak", 0)
        st["wall_streak"] = streak + 1 if bool(kind) != bool(prev) else 0
        if bool(kind) == bool(prev) or st["wall_streak"] < 3:
            return
        st["wall"], st["wall_streak"] = kind, 0
        if time.time() - st.get("wall_posted", 0) < 1200:
            return
        st["wall_posted"] = time.time()
        if kind:
            st["wall_since"] = now.isoformat()
            self.notify.send("normal", f"🛡️ Pokémon Center turned on its bot check · {kind}", t["url"],
                             {"Since (CT)": self._clock(now)},
                             desc="Sites often tighten bot checks right before a drop or queue. It may also just be "
                                  "aimed at this checker. The queue is still watched every minute (30 s in drop mode).",
                             channel=t.get("channel"), record=True)
        else:
            since = st.get("wall_since")
            dur = f" · was on {self._dur(now - datetime.fromisoformat(since))}" if since else ""
            self.notify.send("normal", f"🛡️ Pokémon Center bot check is off again{dur}", t["url"],
                             channel=t.get("channel"), record=True)

    def test_queue_alert(self):
        t = next((x for x in self.targets() if x.get("track_duration") and "pokemoncenter" in x.get("url", "")), {"url": "https://www.pokemoncenter.com/",
                                                                            "channel": ["pokemon_queue", "pokemon"]})
        now = datetime.now(CT)
        return self.notify.send("urgent", "🧪 TEST · 🚨 POKÉMON CENTER QUEUE IS LIVE", t["url"], alarm=True, fields=
                                {"Went up (CT)": self._clock(now), "Detected": "test message"},
                                desc="This is what you'll get the moment a real queue goes up (with @everyone). "
                                     "Then one message keeps counting how long it's been up, and a last one says when "
                                     "it closed and how long it lasted.",
                                links=[("Join the queue", t["url"])], channel=t.get("channel"), record=False)

    def check_keywords(self, t, st, first):
        """Alert when a page flips live: queue page, 'Enter drawing', 'Request invite', 'Add to cart'."""
        if t.get("track_duration"):
            return self.check_queue(t, st, first)
        status, final, html = self._fetch(t, st)
        url_hit = any(k.lower() in (final or "").lower() for k in t.get("url_contains", []))
        if (looks_blocked(status, html) or status >= 400) and not url_hit:
            return ("blocked" if looks_blocked(status, html) else "error"), status
        text = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        hits = [k for k in t.get("keywords", []) if k.lower() in text]
        active = bool(hits) or url_hit
        was = st.get("active")
        if active and was is not True and not (first and t.get("quiet_on_first", False)):
            title = t.get("alert_title", f"{t['name']}: LIVE")
            fields = {"Source": t["name"], "Detected": "queue / waiting room" if url_hit else ", ".join(hits[:4]),
                      "Category": t.get("category", "")}
            raffle = any(w in h.lower() for h in hits for w in ("drawing", "invite", "raffle", "lottery", "chance"))
            self.notify.send(t.get("level", "urgent"), title, t["url"], fields,
                             copy_to=["drawings"] if raffle else None, ping=is_etb_or_upc(t["name"] + " " + title))
        elif was is True and not active and t.get("alert_on_end"):
            self.notify.send("normal", f"{t['name']}: ended", t["url"], {"Source": t["name"]})
        st["active"] = active
        return ("ok · LIVE now" if active else "ok · not live"), status

    # ------------------------------------------------------------ Pokémon Center queue timing
    @staticmethod
    def _dur(td):
        m = max(0, int(td.total_seconds() // 60))
        if m >= 1440:
            return f"{m // 1440}d {(m % 1440) // 60}h"
        return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m} min"

    @staticmethod
    def _clock(d):
        return f"{d.hour % 12 or 12}:{d:%M} {'PM' if d.hour >= 12 else 'AM'}"

    def _queue_hook(self, t):
        hooks = settings_mod.load().get("webhooks") or {}
        chans = t.get("channel") if isinstance(t.get("channel"), list) else [t.get("channel")]
        return next((hooks.get(c) for c in chans if c and (hooks.get(c) or "").startswith("http")), "")

    def _queue_live_message(self, t, st, text):
        """One message in the queue channel that keeps saying how long the queue has been up."""
        hook = self._queue_hook(t)
        if not hook:
            return
        body = {"content": text}
        try:
            mid = st.get("session_msg")
            if mid and st.get("session_hook") == hook:
                if requests.patch(f"{hook}/messages/{mid}", json=body, timeout=15).status_code < 400:
                    return
            r = requests.post(hook + "?wait=true", json=body, timeout=15)
            if r.status_code < 400:
                st["session_msg"], st["session_hook"] = r.json()["id"], hook
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("queue timer message: %s", e)

    def _queue_watch(self, t, st, text, repost=False):
        """The queue channel's status line. repost: move it back to the bottom (after a queue closes)."""
        hook = self._queue_hook(t)
        if not hook or st.get("watch_text") == text and not repost:
            return
        body = {"content": text}
        try:
            mid = st.get("watch_msg") if st.get("watch_hook") == hook else None
            if mid and repost:
                requests.delete(f"{hook}/messages/{mid}", timeout=15)
            elif mid and requests.patch(f"{hook}/messages/{mid}", json=body, timeout=15).status_code < 400:
                st["watch_text"] = text
                return
            r = requests.post(hook + "?wait=true", json=body, timeout=15)
            if r.status_code < 400:
                st["watch_msg"], st["watch_hook"], st["watch_text"] = r.json()["id"], hook, text
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("queue status line: %s", e)

    @staticmethod
    def _queue_site(t):
        """-> (display name, file slug) of the site a queue watcher watches (Pokémon Center, Crunchyroll...)."""
        site = t.get("site") or "Pokémon Center"
        return site, re.sub(r"[^a-z0-9]+", "-", site.lower().replace("é", "e")).strip("-")

    def _track_queue(self, t, st, active, was, detected):
        now = datetime.now(CT)
        site, slug = self._queue_site(t)
        history = self.state.setdefault("queue_history" if slug == "pokemon-center" else f"queue_history_{slug}", [])
        if active and was is not True:
            st["live_since"] = now.isoformat()
            st.pop("session_msg", None)
            self.notify.send("urgent", t.get("alert_title", "🚨 POKÉMON CENTER QUEUE IS LIVE"), t["url"],
                             {"Went up (CT)": self._clock(now), "Detected": detected},
                             links=[("Join the queue", t["url"])], ping=True, everyone_ok=True,
                             alarm=slug == "pokemon-center")
            if settings_mod.load().get("open_browser_on_queue", True):
                # open the site in M's own Chrome right away, so the line is already up on screen when he
                # gets to the PC - he joins it himself (the app never enters queues)
                try:
                    from .winsys import open_in_chrome
                    open_in_chrome(t["url"])
                except Exception as e:
                    log.warning("open browser on queue: %s", e)
        if not active:
            # one self-editing line so you can see it's watching; nothing new is posted until a queue goes up
            left = self.drop_mode()
            if left:
                mode = f"\n🚨 **DROP MODE** until {self._clock(now + timedelta(seconds=left))} CT · checking every 30 s"
            elif self.queue_window():
                w = settings_mod.load().get("queue_window", self.QUEUE_WINDOW)
                end = int(w.get("end", 15))
                mode = (f"\n🔴 **Drop window** (weekdays, when Pokémon Center usually drops) until "
                        f"{end % 12 or 12} {'PM' if end >= 12 else 'AM'} CT · checked every minute, and it's the only "
                        f"thing Rip Radar asks Pokémon Center for (product scans rest)")
            else:
                mode = "\n-# Checked every minute (every 2 min 2-6 AM)."
            rel_line = self.queue_reliability() if slug == "pokemon-center" else ""
            if rel_line:
                mode += f"\n-# Reliability: {rel_line}"
            self._queue_watch(t, st, f"⚪ **No {site} queue right now** · last checked {self._clock(now)} CT"
                                     f"{mode} You'll get an @everyone the moment a queue goes up.",
                              repost=was is True)
        else:
            self._queue_watch(t, st, f"🟢 **Queue is UP** since {self._clock(datetime.fromisoformat(st['live_since']))} CT "
                                     f"· see the alert below")
        if active:
            since = datetime.fromisoformat(st.get("live_since") or now.isoformat())
            self._queue_live_message(t, st, f"🟢 **{site} queue is LIVE** · up for **{self._dur(now - since)}** "
                                            f"(since {self._clock(since)} CT)\n-# Last checked {self._clock(now)} CT")
        elif was is True:
            since = datetime.fromisoformat(st.pop("live_since", now.isoformat()))
            dur = self._dur(now - since)
            history.insert(0, {"start": since.isoformat(), "end": now.isoformat(), "minutes": int((now - since).total_seconds() // 60)})
            del history[20:]
            self._queue_live_message(t, st, f"⚪ {site} queue closed · was up **{dur}** "
                                            f"({self._clock(since)}–{self._clock(now)} CT)")
            st.pop("session_msg", None)
            recent = "\n".join(f"• {datetime.fromisoformat(h['start']):%a %b} {datetime.fromisoformat(h['start']).day}: "
                               f"{self._clock(datetime.fromisoformat(h['start']))}–{self._clock(datetime.fromisoformat(h['end']))}"
                               f" ({self._dur(timedelta(minutes=h['minutes']))})" for h in history[:5])
            self.notify.send("normal", f"⚪ {site} queue closed · was up {dur}", t["url"],
                             {"Went up (CT)": self._clock(since), "Closed (CT)": self._clock(now), "Up for": dur},
                             desc=f"Recent queues:\n{recent}")

    def check_feed(self, t, st, first):
        """News / Reddit RSS: new posts about raffles, drawings, invites, queues or drop times."""
        try:
            status, final, body = self.fetcher.get(t["url"])
        except Exception as e:
            # some sites cut Python's requests off ("Remote end closed connection") - one look via the browser
            log.info("%s: %s - trying the browser", t["name"], e)
            status, final, body = self.fetcher.get(t["url"], True)
            if not re.search(r"<(item|entry)\b", body or "", re.I):
                return f"error: {type(e).__name__} (site cut the connection)", 0
        if status >= 400:
            return ("blocked" if status in (403, 429) else "error"), status
        feed = feedparser.parse(body)
        must = [k.lower() for k in t.get("must_have", [])]
        exclude = [k.lower() for k in t.get("exclude", [])]
        seen = set(st.get("seen", []))
        shared = self.state.setdefault("news_seen", [])      # across all news searches: one post per article
        shared_set = set(shared)
        sent = 0
        for e in feed.entries:
            key = e.get("id") or e.get("link") or e.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            title = e.get("title", "").strip()
            story = re.sub(r"\W+", " ", title.lower().rsplit(" - ", 1)[0]).strip()[:120]
            if first:
                if story and story not in shared_set:
                    shared.append(story)
                    shared_set.add(story)
                continue
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
            blob = f"{title} {summary}".lower()
            if (must and not any(k in blob for k in must)) or any(k in blob for k in exclude):
                continue
            pub = e.get("published_parsed") or e.get("updated_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) < datetime.now(timezone.utc) - timedelta(days=3):
                continue
            link = e.get("link", "")
            fields = {"Source": t["name"], "Category": categorize(blob, t.get("category", ""))}
            when = extract_when(f"{title}. {summary}")
            if t.get("calendar_only"):         # release-date feeds: onto the drop calendar, no ping
                if when:
                    self._when_fields(title[:120], link, when, fields, t.get("kind", "release"),
                                      store_in(title + " " + summary))
                continue
            if story in shared_set:                             # another search already posted this story
                continue
            # only card raffles and card PRODUCT ANNOUNCEMENTS, judged on the HEADLINE (a "Smart bulbs drop to $13.99
            # on Amazon" story matched the search words but isn't ours); deals, reviews, guides are left out
            head = title.lower().rsplit(" - ", 1)[0]
            about_cards = bool(re.search(r"pok[eé]mon (?:tcg|cards?|center|trading)|pok[eé]mon\b.*\b(?:etb|elite trainer|"
                                         r"booster|collection|set|expansion|tin|bundle)|topps|bowman|trading card|\btcg\b|"
                                         r"elite trainer|booster (?:box|bundle|pack)", head))
            is_raffle = any(w in head for w in ("drawing", "raffle", "lottery", "invite", "sweepstakes", "early access", " eql"))
            is_announcement = bool(re.search(
                r"pre-?orders?|release date|releases?\b|releasing|launch(?:es|ing)?|revealed?|announce[sd]?|first look|"
                r"coming (?:soon|to|in|this|next)|arriv(?:es|ing)|restock(?:ed|s|ing)?|back in stock|goes on sale|on sale "
                r"(?:now|today|this|next|on)|available (?:now|today|on|this|next)|drops? (?:on|this|next|today|tomorrow)|"
                r"queue|new set|next set|expansion", head))
            is_noise = bool(re.search(r"\$\d|% off|\bdeal|\bsave\b|discount|coupon|price (?:drop|cut|guide)|cheapest|"
                                      r"\breview\b|how to|best .* to buy|worth it|\branked\b|\bvs\.?\b|\bgame\b.*\bswitch\b|"
                                      r"\banime\b|\bmovie\b|pokémon go|pokemon go|\bplush\b", head))
            if is_announcement and re.search(r"pok[eé]mon", head) and re.search(
                    r"pre-?order|release|set\b|expansion|restock|queue|etb|booster|collection", head):
                about_cards = True   # "Pokémon Delta Reign preorders open" (the set name is the card product)
            if is_raffle:        # store raffles are for card product: "Pokémon" / "Topps" in the headline is enough
                about_cards = about_cards or bool(re.search(r"pok[eé]mon|topps|bowman|\bcards?\b|\btcg\b", head))
            if not about_cards or is_noise or not (is_raffle or is_announcement):
                shared.append(story)
                shared_set.add(story)
                continue
            shared.append(story)
            shared_set.add(story)
            store = store_in(title + " " + summary)
            self._when_fields(title[:120], link, when, fields, "news", store)
            level = "urgent" if any(k in blob for k in t.get("urgent_if", [])) else t.get("level", "normal")
            if store:
                fields["Store"] = STORE_NAMES[store]
            if is_raffle:
                # a store's raffle: that store's channel AND #drawings (any store: Walmart, Dick's, Target...)
                self.notify.send(level, "🎟️ " + title[:240], link, fields, desc=summary[:300],
                                 channel=store or "drawings", copy_to=["drawings"],
                                 ping=is_etb_or_upc(title) or self._focus(title)["ping"])
            else:
                # a product announcement: main alerts channel, keeping store channels products-only
                self.notify.send(level, "📣 " + title[:240], link, fields, desc=summary[:300], channel="main",
                                 ping=(is_etb_or_upc(title) or self._focus(title)["ping"]) and any(
                                     w in blob for w in ("restock", "drop", "live", "in stock",
                                                                                         "pre-order", "preorder")))
            sent += 1
            if sent >= t.get("max_per_run", 8):
                break
        st["seen"] = list(seen)[-2000:]
        del shared[:-3000]
        return f"ok ({len(feed.entries)} posts)", status

    def _keep_filter(self, t):
        """Which listings a store source keeps -> fn(text). (Shared with tools/diag_filter.py.)"""
        keep = {"pokemon": is_pokemon_product, "sports": self._wanted_sports_card}.get(t.get("products", "cards"),
                                                                                       self._wanted_card)
        # M's rule (1.0.42): EVERY store channel is cards only - sealed Pokémon / sports product, no sleeves, binders,
        # playmats, deck boxes or plush ("cards_only: false" on a source would turn this off)
        if t["store"] == "pokemon":
            # everything at Pokémon Center is Pokémon, names don't always say so
            return lambda text: is_tcg_product(text) and not is_accessory_only(text)
        if t.get("cards_only", True):
            base_keep, kind = keep, t.get("products", "cards")
            return lambda text, b=base_keep, k=kind: b(text) and is_card_only(text, k)
        return keep

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
        keep = self._keep_filter(t)
        cap = float(t.get("max_price", 0) or 0)
        blocked = self._blocklist()
        cards = [x for x in tiles if keep(f"{x['name']} {x['url']}") and not blocked_by(x["name"], blocked)
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
        overpriced = 0
        use_cards = self.cards.enabled(store)
        changed = []                     # (x, rec, event headline, ping)
        for x in cards:
            prev = known.get(x["id"])
            kind = "pokemon" if is_tcg_product(x["name"] + " " + x["url"]) else "sports"
            verdict, info = price_check(x["name"], x["price"], kind)
            extra = {"Retail (MSRP)": msrp_text(info)}
            details = {"name": x["name"], "url": x["url"], "price": x["price"], "status": x["status"],
                       "add_to_cart": x.get("add_to_cart", ""), "buy_now": x.get("buy_now", ""),
                       "image": x.get("image", ""), "over": verdict, "seen": now, "limit": x.get("limit", ""),
                       "retail": msrp_text(info), "ratio": info.get("ratio"), "seller": x.get("seller", ""),
                       "price_seen": now, "stale": False}
            if (prev or {}).get("price") != x["price"] or not (prev or {}).get("price_since"):
                details["price_since"] = now          # when this price was first seen (shown on the post)
            if x.get("stock") or not (prev or {}).get("stock_at"):
                details["stock"] = x.get("stock", "")
            if x["live"] and verdict == "way_over":
                overpriced += 1          # reseller pricing: no ping, no card
                rec = known.setdefault(x["id"], {})
                rec.update({"live": x["live"], **details})
                if use_cards and rec.get("carded"):
                    self.cards.retire(store, x["id"])
                    rec["carded"] = False
                continue
            warn = f" · ⚠️ {int((info['ratio'] - 1) * 100)}% above retail" if verdict == "over" else ""
            focus = self._focus(x["name"])
            big = is_etb_or_upc(x["name"]) or focus["ping"]     # ETB / UPC / M's @everyone list: @everyone
            if store == "amazon" and x["status"].startswith("Drawing"):
                big = True                                      # Amazon invite requests: always @everyone
            listed_at = (prev or {}).get("first_seen")
            up_before = ""
            if listed_at and not (prev or {}).get("live"):
                atleast = "at least " if (prev or {}).get("baseline") else ""
                up_before = f"{atleast}{self._dur(timedelta(seconds=now - listed_at))} before stock"
                extra["Link was up"] = up_before
            event = None
            # a listing that flickers out and back within 10 min (Hot Topic does this) is not a restock: no new ping
            flicker = (prev is not None and not prev.get("live") and prev.get("oos_since")
                       and now - prev["oos_since"] < 600 and not x["status"].startswith("Drawing"))
            if not first and x["live"] and (prev is None or not prev.get("live")) and not flicker:
                if x["status"].startswith("Drawing"):
                    event = (f"🎟️ INVITE REQUEST OPEN at {label}" if store == "amazon"
                             else f"🎟️ DRAWING / INVITE OPEN at {label}")
                else:
                    event = f"🟢 {'NEW & IN STOCK' if prev is None else 'BACK IN STOCK'} at {label}"
            elif not first and prev is None and t.get("alert_new_listed", True):
                event = f"🆕 Loaded at {label}, not in stock yet"   # page is up before stock: track it
            rec = known.setdefault(x["id"], {})
            if "first_seen" not in rec:
                rec["first_seen"], rec["baseline"] = now, bool(first)
                rec["loaded_before_stock"] = not first and not x["live"]
            if x["status"] != "Listed" or "live" not in rec:
                if rec.get("live") and not x["live"]:
                    rec["oos_since"] = now
                if rec.get("live") and not x["live"] and rec.get("live_since"):
                    # sold out: remember how long it lasted, for the daily recap
                    self.state.setdefault("sellouts", []).append({
                        "store": store, "name": x["name"], "url": x["url"], "at": now,
                        "minutes": int((now - rec["live_since"]) / 60), "focus": rec.get("focus", ""),
                        "price": x.get("price", "")})
                    del self.state["sellouts"][:-2000]
                    rec.pop("live_since", None)
                if x["live"] and not rec.get("live"):
                    rec["live_since"] = now
                rec["live"] = x["live"]
            if rec["live"]:
                rec["loaded_before_stock"] = False
            rec.update(details)
            rec["focus"] = focus["focus"]
            if event and x["live"] and up_before:
                rec["up_before"] = up_before
            elif not rec["live"]:
                rec.pop("up_before", None)
            # an announced date on a product that isn't buyable yet -> drop calendar, linked to the product
            if not x["live"]:
                self._product_date(x, store, label)
            # copies: anything that's a raffle / drawing -> #drawings, anything PRESALE / pre-order -> #presales
            raffle = x["status"].startswith("Drawing") or is_raffle(x["name"])
            presale = (bool(x.get("presale")) or is_presale(x["name"], x["status"])) and channel_of(store) != "presale"
            if event and not use_cards:
                title = f"{event}: {x['name']}{warn}"
                self._product_alert("urgent" if x["live"] else "normal", title, x, label, extra,
                                    copy_to=(["drawings"] if raffle else []) + (["presale"] if presale else []) or None,
                                    store=store, ping=big)
            elif event:
                self._record_alert({"level": "urgent" if x["live"] else "normal", "title": f"{event}: {x['name']}{warn}",
                                    "url": x["url"], "fields": {k: v for k, v in {"Price": x["price"], **extra}.items() if v},
                                    "desc": "", "image": x.get("image", ""), "links": self._buy_links(x),
                                    "channel": store, "at": datetime.now(timezone.utc).isoformat()})
                if raffle:                                    # the store's card bumps AND #drawings gets it
                    self._product_alert("urgent", f"{event}: {x['name']}{warn}", x, label, extra,
                                        channel="drawings", store=store, ping=big, strict=True, record=False)
                if presale:                                   # ... and #presales gets PRESALE / pre-order items
                    self._product_alert("urgent" if x["live"] else "normal", f"🛍️ PRESALE · {event}: {x['name']}{warn}",
                                        x, label, extra, channel="presale", store=store, ping=big, strict=True,
                                        record=False)
            if event:
                self._watch_alerts(store, x, rec, event)
            changed.append((x, rec, event, big and bool(event)))
        if store == "target":
            self._refresh_target_stock(html, [c for c in changed], st)
        elif t.get("stock_pages", 2):
            self._refresh_page_stock(t, st, changed)
        self._fill_images(t, st, changed)
        self._attach_resale(changed, store)
        if use_cards:
            # listings the filters no longer want (graded slabs, lots...): take their posts down now
            kept = {x["id"] for x in cards}
            for x in tiles:
                rec = known.get(x["id"])
                if x["id"] not in kept and rec and rec.get("carded"):
                    self.cards.retire(store, x["id"])
                    rec["carded"] = False
            for x, rec, event, ping in changed:
                if ping and self.notify.everyone_only_in_watch():
                    self.notify.everyone_copy(f"{event}: {x['name']}"[:250], x["url"],
                                              {"Store": label, "Price": x.get("price"), "Stock": rec.get("stock") or
                                               x.get("status"), "Focus": rec.get("focus")},
                                              image=x.get("image", ""), links=self._buy_links(x), store=store)
                    ping = False
                self.cards.request(store, x["id"], rec, bump=bool(event), ping=ping, headline=event or "")
                rec["carded"] = True
            # a post whose product hasn't been on the store page for 30+ min: say its price may be out of date
            for pid, rec in known.items():
                if rec.get("carded") and not rec.get("stale") and now - rec.get("seen", now) > 1800:
                    rec["stale"] = True
                    self.cards.request(store, pid, rec)
            # products that dropped off the store's listings for a day: remove their message
            for pid, rec in known.items():
                if rec.get("carded") and now - rec.get("seen", 0) > 86400:
                    self.cards.retire(store, pid)
                    rec["carded"] = False
        if len(known) > 3000:
            for k in sorted(known, key=lambda k: known[k].get("seen", 0))[: len(known) - 3000]:
                known.pop(k)
        live = sum(1 for x in cards if x["live"])
        over = f" · {overpriced} over retail ignored" if overpriced else ""
        return (f"ok ({len(cards)} card products · {live} in stock{over} · {len(tiles) - len(cards)} other items skipped)",
                status)

    # ------------------------------------------------------------ stock counts
    def _refresh_target_stock(self, html, changed, st, per_run=4, every=3600, hot_every=900):
        """Exact counts from Target's stock service: online + stores near your ZIP. Just-changed items first."""
        key = target_key_in(html)
        if key:
            self.state["target_key"] = key
        s = settings_mod.load()
        zip_code = str(s.get("zip") or "").strip()
        miles = int(s.get("restock_miles") or 30)
        session = getattr(self.fetcher, "s", None)
        if session is None:
            return
        now = time.time()
        # every card product, including ones sold out online: stores can have them when the website doesn't
        from .stock import target_paused
        if target_paused():
            return                                  # Target pushed back: wait the pause out (stock.GUARD)
        hot = lambda r: bool(is_etb_or_upc(r.get("name", "")) or r.get("focus") or self._focus(r.get("name", ""))["ping"])
        todo = sorted(changed, key=lambda c: (not c[2], not hot(c[1]), c[1].get("stock_at", 0)))
        n = 0
        for x, rec, event, _ in todo:
            wait = hot_every if hot(rec) else every
            if n >= per_run or (not event and now - rec.get("stock_at", 0) < wait):
                continue
            n += 1
            info = target_stock(session, x["id"], zip_code, self.state.get("target_key", ""), miles=miles,
                                browser_json=self.stock_json)
            rec["stock_at"] = now
            if info:
                rec["stock"], rec["stores"] = target_stock_text(info, zip_code)
                if info["online"] == 0 and not info["stores"] and rec.get("live") and info["sold_out"]:
                    rec["stock"] = "Sold out everywhere (page still shows it)"
                self._store_restocks("target", x, rec, info["stores"])

    def _store_restocks(self, store, x, rec, stores):
        """In-store restock tracker: a nearby store going from 0 to some (or jumping by 5+) is a restock.
        Pings #in-store (else the store's channel) and logs it so each store's restock rhythm can be learned."""
        counts = {n: q for n, q in stores}
        prev = rec.get("store_counts")
        rec["store_counts"] = counts
        if prev is None:                        # first look at this product's stores: baseline, no ping
            return
        got = [(n, q) for n, q in counts.items() if q > 0 and (prev.get(n, 0) == 0 or q - prev.get(n, 0) >= 5)]
        if not got or blocked_by(x.get("name", ""), self._blocklist()):
            return
        got.sort(key=lambda g: -g[1])
        label = STORE_NAMES.get(store, store)
        now = datetime.now(CT)
        log_ = self.state.setdefault("restock_log", [])
        for n, q in got:
            log_.append({"store": store, "location": n, "qty": q, "was": prev.get(n, 0), "product": x["name"],
                         "at": now.isoformat()})
        del log_[:-3000]
        others = [f"{n} {q}" for n, q in sorted(counts.items(), key=lambda kv: -kv[1]) if (n, q) not in got][:6]
        first_n, first_q = got[0]
        title = (f"🏬 {label} {first_n} just got {first_q}" + (f" (+{len(got) - 1} more stores)" if len(got) > 1 else "")
                 + f": {x['name']}")
        fields = {"Restocked": " · ".join(f"**{n}** {prev.get(n, 0)} → **{q}**" for n, q in got[:8]),
                  "Other stores": " · ".join(others), "Price": x.get("price"), "Online": rec.get("stock")}
        self.notify.send("urgent", title[:250], x["url"], fields, image=x.get("image", ""), links=self._buy_links(x),
                         channel=["instore", store], store=store, product=True,
                         ping=is_etb_or_upc(x["name"]) or self._focus(x["name"])["ping"])
        self._restock_board_dirty = True

    def stock_health(self):
        """One real Target store-count request, so the startup message says whether Mat local / look-ups work."""
        from .stock import LAST_ERROR, target_paused
        if target_paused():
            return f"🏬 Target store counts: ⏸️ {LAST_ERROR.get('text')}"[:300]
        pid = next((pid for t in self.targets() if t.get("store") == "target"
                    for pid in (self.state.get("targets", {}).get(t["name"], {}).get("items") or {})), None)
        if not pid:
            return "🏬 Target store counts: no Target products yet to test with"
        zip_code = str(settings_mod.load().get("zip") or "77007")
        info = target_stock(getattr(self.fetcher, "s", None) or requests.Session(), pid, zip_code,
                            self.state.get("target_key", ""), stores_only=True, browser_json=self.stock_json)
        if info and info["stores"]:
            return f"🏬 Target store counts: ✅ working ({len(info['stores'])} stores near {zip_code})"
        return f"🏬 Target store counts: ❌ not working · {LAST_ERROR.get('text') or 'no stores returned'}"[:300]

    def zip_lookup(self, zip_code, miles=30, max_products=20):
        """In store look up: every Target card product we know of, at every Target within `miles` of any ZIP.
        -> Discord messages (each under 2000 characters), stores nearest first, only products they have."""
        items = {}
        for t in self.targets():
            if t.get("type") == "retail_search" and t.get("store") == "target":
                for pid, rec in (self.state.get("targets", {}).get(t["name"], {}).get("items") or {}).items():
                    if rec.get("name") and time.time() - rec.get("seen", 0) < 3 * 86400 and rec.get("over") != "way_over":
                        items[pid] = rec
        if not items:
            return ["I don't have a Target product list yet - the Target scanner fills it in a few minutes after launch."]
        # ETBs / UPCs / Pokémon first, so the most wanted items are always checked
        order = sorted(items, key=lambda p: (not is_etb_or_upc(items[p]["name"]), not is_pokemon_product(items[p]["name"]),
                                            items[p]["name"]))[:max_products]
        session = getattr(self.fetcher, "s", None) or requests.Session()
        stores, dist, answered = {}, {}, 0
        for pid in order:
            info = target_stock(session, pid, zip_code, self.state.get("target_key", ""), miles=miles, stores_only=True,
                                browser_json=self.stock_json, urgent=True)
            time.sleep(1.5)
            if not info:
                continue
            answered += 1
            dist.update(info.get("distance") or {})
            for name, q in info["stores"]:
                stores.setdefault(name, [])
                if q > 0:
                    stores[name].append((items[pid]["name"], q, items[pid]["url"]))
        if not answered:
            from .stock import LAST_ERROR
            why = f"\n-# Reason: {LAST_ERROR['text'][:180]}" if LAST_ERROR.get("text") else ""
            return [f"Target didn't answer the stock look-up for {zip_code} right now. Try again in a few minutes.{why}"]
        with_stock = {n: v for n, v in stores.items() if v}
        head = (f"🏬 **Target stores within {miles} mi of {zip_code}** · {len(stores)} stores · "
                f"{len(with_stock)} have card product · {answered} products checked")
        if not with_stock:
            return [head + "\nNone of them show Pokémon or sports card stock right now."]
        chunks, cur = [], head
        for name in sorted(with_stock, key=lambda n: (dist.get(n, 999), n)):
            prods = sorted(with_stock[name], key=lambda p: -p[1])
            d = f" · {dist[name]:.1f} mi" if name in dist else ""
            block = f"\n\n**{name}**{d} · {sum(q for _, q, _ in prods)} items\n" + "\n".join(
                f"• {self._short(n)[:60]} **{q}**" for n, q, _ in prods[:12])
            if len(prods) > 12:
                block += f"\n• …and {len(prods) - 12} more"
            if len(cur) + len(block) > 1900:
                chunks.append(cur)
                cur = block.strip()
            else:
                cur += block
        chunks.append(cur)
        return chunks

    def _render_restocks(self):
        """#in-store board: when each nearby store usually restocks (learned from what the tracker has seen)."""
        from collections import Counter, defaultdict
        by_loc = defaultdict(list)
        for e in self.state.get("restock_log", []):
            by_loc[(e["store"], e["location"])].append(e)
        rows = []
        for (store, loc), evs in sorted(by_loc.items(), key=lambda kv: -len(kv[1])):
            days = Counter(datetime.fromisoformat(e["at"]).strftime("%a") for e in evs)
            hours = Counter(datetime.fromisoformat(e["at"]).hour for e in evs)
            top_days = ", ".join(d for d, _ in days.most_common(2))
            h = hours.most_common(1)[0][0]
            part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
            last = datetime.fromisoformat(evs[-1]["at"])
            rows.append(f"**{STORE_NAMES.get(store, store)} {loc}** · {len(evs)} restocks seen · usually "
                        f"**{top_days}** {part} · last {last:%a %b} {last.day} {self._clock(last)}")
        if not rows:
            rows = ["Nothing yet. Every time a store near you goes from 0 to having a card product, it shows up here, "
                    "and after a few weeks you'll see each store's usual restock days."]
        s = settings_mod.load()
        return self._section(f"🏬 In-store restocks · within {s.get('restock_miles', 30)} mi of {s.get('zip', '')}", rows)

    def _fill_images(self, t, st, changed, per_run=2, retry_hours=6):
        """Tiles without a picture: open the product page and take its main image (og:image), a couple per run,
        new / just-changed products first, so every post has a big product picture."""
        now = time.time()
        todo = [c for c in changed if not c[1].get("image") and now - c[1].get("img_tried", 0) > retry_hours * 3600]
        todo.sort(key=lambda c: (not c[2], -c[1].get("first_seen", 0)))
        for x, rec, event, _ in todo[:per_run]:
            rec["img_tried"] = now
            try:
                ps, pf, ph = self._fetch({"name": t["name"], "url": x["url"], "browser": t.get("browser")}, st)
            except Exception as e:
                log.info("image page %s: %s", x["url"], e)
                continue
            if not looks_blocked(ps, ph) and ps < 400:
                img = og_image(ph, pf)
                if img:
                    rec["image"] = x["image"] = img

    def _attach_resale(self, changed, store=None):
        """eBay sold price on each post. Known values go on straight away; unknown ones are looked up - live drops
        first and immediately - and edited into the post the moment the answer comes back (_resale_ready)."""
        if not getattr(self, "resale", None):
            return
        if not self.resale.enabled():
            # per-product eBay lookups are off (1.0.61): never leave a "checking eBay…" line that won't resolve
            for _, rec, _, _ in changed:
                if rec.get("resale") in (self.RESALE_WAIT, self.RESALE_FAIL):
                    rec.pop("resale", None)
            return
        for x, rec, event, _ in changed:
            name = rec.get("name", "")
            r = self.resale.lookup(name, urgent=bool(event) and bool(rec.get("live")))
            if r:
                rec["resale"] = self.resale.text(r, rec.get("price"))
                continue
            hit = self.resale.cache.get(self.resale.key(name)) or {}
            if hit.get("ok") and not hit.get("median"):
                rec["resale"] = self.RESALE_NONE
                continue
            if not rec.get("resale") or rec["resale"] == self.RESALE_FAIL:
                rec["resale"] = self.RESALE_WAIT          # shows on the post until the value is edited in
            if store and rec.get("carded", True):
                self._resale_posts.setdefault(self.resale.key(name), {})[(store, x["id"])] = rec

    def _resale_watchdog(self):
        """#app-status hears about it once when eBay keeps turning lookups away, and once when it works again."""
        st = self.resale.stats
        flagged = self.state.get("resale_flagged")
        if st["fail_streak"] >= 15 and not flagged:
            self.state["resale_flagged"] = True
            self.notify.send("system", "💸 Resale values aren't coming through", "https://www.ebay.com/",
                             {"Why": st["last_error"], "What happens": "Posts show \"eBay didn't answer\"; lookups retry "
                              "every 30 min by themselves"})
        elif flagged and st["fail_streak"] == 0 and st["ok"]:
            self.state["resale_flagged"] = False
            self.notify.send("system", "💸 Resale values are working again", "https://www.ebay.com/")

    RESALE_WAIT = "⏳ checking eBay sold listings…"
    RESALE_NONE = "No recent eBay sales match this product"
    RESALE_FAIL = "⚠️ eBay didn't answer · retrying in 30 min"

    def _resale_ready(self, key, result):
        """A lookup finished: write the value (or why there isn't one) on every waiting post and edit those posts
        now (no new ping)."""
        waiting = self._resale_posts.pop(key, {})
        if result and key in self._price_keys():
            self._record_price(key, result)
            run = self.state.get("price_refresh") or {}
            if key in run.get("pending", []):
                run["pending"].remove(key)
        if not result:
            return
        for (store, pid), rec in waiting.items():
            if result.get("median"):
                rec["resale"] = self.resale.text(result, rec.get("price"))
            elif result.get("ok"):
                rec["resale"] = self.RESALE_NONE
            else:
                rec["resale"] = self.RESALE_FAIL
            if self.cards.enabled(store) and rec.get("carded"):
                self.cards.request(store, pid, rec)

    def _refresh_page_stock(self, t, st, changed, every=1800):
        """Other stores: open a couple of in-stock product pages per run and read 'Only N left' / page data."""
        now = time.time()
        todo = [c for c in changed if c[1].get("live") and (c[2] or now - c[1].get("stock_at", 0) > every)]
        todo.sort(key=lambda c: (not c[2], c[1].get("stock_at", 0)))
        for x, rec, event, _ in todo[: t.get("stock_pages", 2)]:
            rec["stock_at"] = now
            try:
                ps, pf, ph = self._fetch({"name": t["name"], "url": x["url"], "browser": t.get("browser")}, st)
            except Exception as e:
                log.info("stock page %s: %s", x["url"], e)
                continue
            if looks_blocked(ps, ph) or ps >= 400:
                continue
            text = BeautifulSoup(ph, "html.parser").get_text(" ")
            hint, limit = stock_hint(text)
            count, max_qty = page_data_stock(next_data(ph)) if t.get("store") in ("walmart", "samsclub") else (None, None)
            if count is not None and count > 0:
                rec["stock"] = f"{count} available"
            elif hint:
                rec["stock"] = hint
            if limit or (max_qty and max_qty < 20):
                rec["limit"] = limit or f"Limit {max_qty} per order"

    def _product_date(self, x, store, label):
        text = x.get("text", "")
        low = text.lower()
        if not any(w in low for w in ("release", "available", "coming", "launch", "pre-order", "preorder", "arrives",
                                      "on sale", "drops", "starts", "opens")):
            return
        when = extract_when(text)
        if when and when[0] > datetime.now(CT):
            self._add_event(f"{label}: {x['name'][:120]}", x["url"], when[0], when[1], "release", store, product=True)

    # ------------------------------------------------------------ #watch: watch by name, test, focus check
    WATCH_HELP = ("**Watch by name** · every word must be in the product name (any order). `etb` = Elite Trainer Box, "
                  "`upc` = Ultra-Premium Collection, `-word` leaves it out, `@target` = one store only.\n"
                  "• `watch prismatic bundle` → @everyone here whenever any store has a match (loaded, in stock, "
                  "back in stock, drawing)\n• `test 151 etb -lot` → what that would catch right now\n"
                  "• `watching` → your list · `unwatch 2` or `unwatch prismatic bundle`\n"
                  "• `check focus` → is each 🔥 focus set / @everyone product actually matching anything\n"
                  "• Every product post comes with ✅ ❌ already on it: tap ❌ → deleted and never posts again (same item "
                  "at other stores too) · tap ✅ → kept (`kept` lists them)\n"
                  "• `block funko` → block by words · `blocked` → the list · `unblock 3` → undo\n"
                  "• `value prismatic etb` → what it sells for on eBay right now · `resale` → is eBay answering\n"
                  "• `prices` → the 💸 daily price list (🔥 sets + @everyone products) · `prices refresh` → update now\n"
                  "• `watch tcin 94681234` or paste a Target link → that exact Target product ID, checked every ~90 s: "
                  "@everyone when it gets LOADED, a DRAWING is set up, or it's IN STOCK · `unwatch tcin 94681234`\n"
                  "• `health` → the 🩺 health check: every source ✅ / 🟡 / ❌ and when it last worked")

    # ---------------------------------------------------------------- ❌ on a post = never show it (or its twins)
    def _blocklist(self):
        return list(settings_mod.load().get("blocked_products") or [])

    def block_product(self, message_id, title=""):
        """Someone reacted ❌ to a post. Our own product card -> block that product; any other post with a product
        title -> block that name. Takes down every matching card in every channel. -> reply text, or None when
        the message isn't a product post (boards, status...)."""
        message_id = str(message_id)
        store = pid = name = None
        with self._lock:
            for key, v in (self.state.get("product_cards") or {}).items():
                if str(v.get("id")) == message_id:
                    store, _, pid = key.partition(":")
                    name = v.get("name")
                    break
        if store and not name:
            name = next((r["name"] for r in self._catalog(max_age_hours=24 * 30)
                         if r["store"] == store and str(r["id"]) == pid), None)
        if not name and title:
            name = name_from_title(title)
        if not name or len(name) < 6:
            return None
        return self.block_name(name)

    def keep_product(self, message_id):
        """✅ on a product post: M wants it. Remembered (shown in the daily recap / `kept` in #watch); if that product
        had been blocked by mistake, it's unblocked."""
        message_id = str(message_id)
        with self._lock:
            hit = next((v.get("name") for v in (self.state.get("product_cards") or {}).values()
                        if str(v.get("id")) == message_id), None)
            if not hit:
                return None
            kept = self.state.setdefault("kept_products", [])
            if hit not in kept:
                kept.append(hit)
                del kept[:-300]
        bl = self._blocklist()
        still = [b for b in bl if not (not b.startswith("=") and blocked_by(hit, [b]))]
        if len(still) != len(bl):
            settings_mod.update({"blocked_products": still})
        return hit

    def block_name(self, name, typed=False):
        entry = ("=" + name) if typed else name
        blocked = self._blocklist()
        if entry.lower() not in [b.lower() for b in blocked]:
            blocked.append(entry)
            settings_mod.update({"blocked_products": blocked[-500:]})
        gone = self._retire_blocked()
        more = f" and {gone - 1} more like it" if gone > 1 else ""
        return (f"🚫 Blocked **{name[:120]}**{more} · it won't post in any channel again. "
                f"Undo: type `unblock {len(self._blocklist())}` in #watch (`blocked` shows the list).")

    def purge_unwanted_cards(self):
        """Takes down every product post the CURRENT rules don't want: old posts from before a filter changed
        (singles, toys, books, accessories), products ❌'d, and posts whose product we no longer track at all.
        Runs at launch and every 6 h. -> how many were removed."""
        blocked, gone = self._blocklist(), 0
        with self._lock:
            tracked, unwanted = set(), {}
            for t in self.targets():
                st = self.state.get("targets", {}).get(t["name"], {})
                if t["type"] == "retail_search":
                    keep = self._keep_filter(t)
                    for pid, r in (st.get("items") or {}).items():
                        if not r.get("carded"):
                            continue
                        key = f"{t['store']}:{pid}"
                        if not keep(f"{r.get('name', '')} {r.get('url', '')}") or blocked_by(r.get("name", ""), blocked):
                            unwanted.setdefault(key, []).append(r)
                        else:
                            tracked.add(key)
                elif t["type"] == "topps_sitemap":
                    for h, r in (st.get("products") or {}).items():
                        if r.get("carded"):
                            tracked.add(f"topps:{h}")
            for key, recs in unwanted.items():
                if key in tracked:                  # another scan of the same store still wants it
                    continue
                for r in recs:
                    r["carded"] = False
                self.cards.retire(*key.split(":", 1))
                gone += 1
            for key in list((self.state.get("product_cards") or {}).keys()):
                if key not in tracked and key not in unwanted:
                    self.cards.retire(*key.split(":", 1))
                    gone += 1
        if gone:
            log.info("cleanup: took down %d product posts the current rules don't want", gone)
        self._last_purge = time.time()
        return gone

    def _retire_blocked(self):
        """Take down every card whose product is now blocked. -> how many."""
        blocked, gone = self._blocklist(), 0
        with self._lock:
            return self._retire_blocked_locked(blocked)

    def _retire_blocked_locked(self, blocked):
        gone = 0
        for t in self.targets():
            st = self.state.get("targets", {}).get(t["name"], {})
            if t["type"] == "retail_search":
                items, store = st.get("items") or {}, t["store"]
            elif t["type"] == "topps_sitemap":
                items, store = st.get("products") or {}, "topps"
            else:
                continue
            for pid, r in items.items():
                if r.get("carded") and blocked_by(r.get("name", ""), blocked):
                    self.cards.retire(store, pid)
                    r["carded"] = False
                    gone += 1
        return gone

    def _catalog(self, max_age_hours=72):
        """Everything the scanners know about right now (all stores + Topps + Walmart drawings)."""
        out, now = [], time.time()
        for t in self.targets():
            st = self.state.get("targets", {}).get(t["name"], {})
            if t["type"] == "retail_search":
                for pid, r in (st.get("items") or {}).items():
                    if r.get("name") and now - r.get("seen", 0) < max_age_hours * 3600:
                        out.append({**r, "store": t["store"], "id": pid})
            elif t["type"] == "topps_sitemap":
                for h, r in (st.get("products") or {}).items():
                    if r.get("status"):
                        out.append({**r, "store": "topps", "id": h})
            elif t["type"] == "walmart_drawings":
                for k, r in (st.get("drawings") or {}).items():
                    if r.get("phase") != "closed":
                        out.append({**r, "store": "walmart", "id": k, "status": "Drawing " + r.get("phase", ""),
                                    "live": r.get("phase") == "open"})
        uniq = {}
        for r in out:
            uniq.setdefault((r["store"], r["id"]), r)
        return list(uniq.values())

    def _match_lines(self, w, limit=12):
        hits = [r for r in self._catalog() if watch_match(w, r["name"], r["store"])]
        hits.sort(key=lambda r: (not r.get("live"), STORE_NAMES.get(r["store"], r["store"]), r["name"]))
        lines = [f"{'🟢' if r.get('live') else '⚪'} **{STORE_NAMES.get(r['store'], r['store'])}** · "
                 f"[{r['name'][:70]}]({r['url']}) · {r.get('price') or ''} · {r.get('status', '')}" for r in hits[:limit]]
        if len(hits) > limit:
            lines.append(f"…and {len(hits) - limit} more")
        live = sum(1 for r in hits if r.get("live"))
        return hits, live, lines

    def handle_watch_command(self, text):
        """Messages typed in #watch -> replies (each under 2000 characters)."""
        msg = " ".join((text or "").split())
        low = msg.lower()
        terms = list(settings_mod.load().get("watch_terms") or [])
        if low in ("help", "?", "commands"):
            return [self.WATCH_HELP]
        if low in ("watching", "list", "watchlist"):
            ids = settings_mod.load().get("watch_tcins") or []
            tw = self.state.get("tcin_watch", {})
            id_lines = "\n".join(self._tcin_line(i, tw.get(i, {}).get("phase") or "unknown", tw.get(i, {}))
                                 for i in ids)
            if ids and not terms:
                return ["**Watching Target IDs:**\n" + id_lines]
            if ids:
                return ["**Watching** (@everyone in this channel when any store has a match):\n" +
                        "\n".join(f"`{i}` {q}" for i, q in enumerate(terms, 1)) + "\n\n**Target IDs:**\n" + id_lines]
            if not terms:
                return ["Not watching anything yet. Try `watch prismatic bundle` or `watch tcin 94681234`."]
            return ["**Watching** (@everyone in this channel when any store has a match):\n" +
                    "\n".join(f"`{i}` {q}" for i, q in enumerate(terms, 1))]
        if low in ("health", "health check", "status check"):
            title, desc = self.render_health()
            out, cur = [], f"**{title}**"
            for ln in desc.split("\n"):
                if len(cur) + len(ln) > 1900:
                    out.append(cur)
                    cur = ""
                cur += "\n" + ln
            return out + [cur]
        if low in ("check focus", "focus", "check focus list", "focus check"):
            return self.focus_report()
        if low in ("kept", "keepers", "liked"):
            kept = self.state.get("kept_products") or []
            return ["**✅ Kept** (products you tapped ✅ on):\n" + "\n".join(f"• {k}" for k in kept[-40:])
                    if kept else "Nothing ✅'d yet. Tap ✅ under any product post you want."]
        if low in ("blocked", "blocklist", "block list"):
            bl = self._blocklist()
            if not bl:
                return ["Nothing blocked. React ❌ to any product post (or type `block <words>` here) to block it."]
            lines = [f"`{i}` {b.lstrip('=')}{' (typed)' if b.startswith('=') else ''}" for i, b in enumerate(bl, 1)]
            out, cur = [], "**Blocked** (never posted) · `unblock N` to undo:"
            for ln in lines:
                if len(cur) + len(ln) > 1900:
                    out.append(cur)
                    cur = ""
                cur += "\n" + ln
            return out + [cur]
        cmd, _, rest = msg.partition(" ")
        cmd = cmd.lower()
        tc = self.tcin_in(rest) if cmd in ("watch", "add", "unwatch", "remove", "stop") else self.tcin_in(msg)
        if tc and cmd in ("unwatch", "remove", "stop"):
            ids = [x for x in (settings_mod.load().get("watch_tcins") or []) if x != tc]
            settings_mod.update({"watch_tcins": ids})
            self.state.get("tcin_watch", {}).pop(tc, None)
            return [f"🗑️ Stopped watching Target ID `{tc}`."]
        if tc and (cmd in ("watch", "add") or msg.strip() == tc or "target.com" in msg.lower()):
            ids = list(settings_mod.load().get("watch_tcins") or [])
            if tc not in ids:
                if len(ids) >= self.MAX_TCINS:
                    return [f"Already watching {self.MAX_TCINS} Target IDs (the most that stays safe for your "
                            "connection). `unwatch tcin <id>` one first."]
                ids.append(tc)
                settings_mod.update({"watch_tcins": ids})
            phase, info = self._tcin_check(tc) if self.stock_json else ("unknown", {})
            if phase != "unknown":
                self.state.setdefault("tcin_watch", {}).setdefault(tc, {}).update(
                    {"phase": phase, "checked": time.time(),
                     **{k: info[k] for k in ("name", "price", "image", "limit", "street_date") if info.get(k)}})
            return [f"✅ **Watching Target ID `{tc}`** · checked about every 90 s. @everyone here when it gets "
                    f"loaded, set up as a drawing / invite, or comes in stock.\nRight now: "
                    + (self._tcin_line(tc, phase, info) if phase != "unknown" else "couldn't ask Target yet "
                       "(it'll keep trying)")]
        if cmd in ("value", "resale", "ebay") and rest:
            from .resale import clean_query
            q = clean_query(rest)
            try:
                r = self.resale.check(q)
            except Exception as e:
                return [f"💸 Couldn't reach eBay ({type(e).__name__}). {self.resale.health()}"]
            self.resale.cache[q.lower()] = r
            if r.get("median"):
                return [f"💸 **{q}** · " + self.resale.text(r)]
            if r.get("ok"):
                return [f"💸 **{q}** · eBay answered, but no recent sold listings match all of those words. Try "
                        "fewer / different words."]
            return [f"💸 eBay didn't answer. {self.resale.health()}"]
        if low in ("prices", "price list", "pricelist"):
            out = []
            for title, desc in self._render_prices():
                out.append(f"**{title}**\n{desc}"[:1990])
            return out
        if low in ("prices refresh", "refresh prices", "update prices", "refresh"):
            return self.handle_prices_command("refresh")
        if low in ("resale", "resale status", "ebay"):
            return [self.resale.health()]
        if cmd == "block" and rest:
            return [self.block_name(rest, typed=True)]
        if cmd == "unblock" and rest:
            bl = self._blocklist()
            if rest.isdigit() and 1 <= int(rest) <= len(bl):
                gone = bl.pop(int(rest) - 1)
            else:
                gone = next((b for b in bl if b.lstrip("=").lower() == rest.lower()), None)
                if not gone:
                    return [f"`{rest}` isn't blocked. Type `blocked` to see the list."]
                bl.remove(gone)
            settings_mod.update({"blocked_products": bl})
            return [f"↩️ Unblocked: {gone.lstrip('=')} · it can post again from the next scan."]
        if cmd in ("watch", "add") and rest:
            w = parse_watch(rest)
            if not w["words"]:
                return ["Give me some words to watch for, e.g. `watch 151 etb`."]
            if rest.lower() not in [q.lower() for q in terms]:
                terms.append(rest)
                settings_mod.update({"watch_terms": terms})
            hits, live, lines = self._match_lines(w, limit=6)
            head = (f"✅ **Watching: {rest}** · you'll get @everyone here the moment any store has a match.\n"
                    f"Right now: {len(hits)} match ({live} in stock)")
            return [head + ("\n" + "\n".join(lines) if lines else "")]
        if cmd in ("unwatch", "remove", "stop") and rest:
            if rest.isdigit() and 1 <= int(rest) <= len(terms):
                gone = terms.pop(int(rest) - 1)
            elif rest.lower() in [q.lower() for q in terms]:
                gone = next(q for q in terms if q.lower() == rest.lower())
                terms.remove(gone)
            else:
                return [f"Not watching `{rest}`. Type `watching` to see the list."]
            settings_mod.update({"watch_terms": terms})
            return [f"🗑️ Stopped watching: {gone}"]
        if cmd in ("test", "find", "check") and rest:
            w = parse_watch(rest)
            hits, live, lines = self._match_lines(w)
            if not hits:
                return [f"🔎 `{rest}` matches nothing the scanners have seen in the last 3 days. Check the spelling, "
                        "use fewer words, or it may not be carried right now (a watch still catches it later)."]
            return [f"🔎 `{rest}` would catch **{len(hits)}** ({live} in stock):\n" + "\n".join(lines)]
        return [self.WATCH_HELP]

    def _watch_alerts(self, store, x, rec, event):
        """A product with news (loaded / in stock / back / drawing) that matches a #watch term -> @everyone there."""
        terms = settings_mod.load().get("watch_terms") or []
        if not terms or blocked_by(x.get("name", ""), self._blocklist()):
            return
        sent = self.state.setdefault("watch_sent", {})
        for q in terms:
            if not watch_match(parse_watch(q), x["name"], store):
                continue
            key = f"{q.lower()}|{store}|{x['id']}|{event.split(' at ')[0]}"
            if key in sent:
                continue
            seen_ping = self.notify.__dict__.get("_everyone_recent", {}).get(x["url"].split("?")[0], 0)
            if time.time() - seen_ping < 600:
                sent[key] = time.time()                 # this drop already pinged #watch in the last 10 min
                continue
            sent[key] = time.time()
            self.notify.send("urgent", f"👀 {event}: {x['name']}"[:250], x["url"],
                             {"Watching": q, "Price": x.get("price"), "Stock": rec.get("stock") or x.get("status"),
                              "💰 Deal": "", "Limit": x.get("limit")},
                             image=x.get("image", ""), links=self._buy_links(x), channel="watch", strict=True,
                             store=store, product=True, ping=True, record=False, everyone_ok=True)
            self.notify.__dict__.setdefault("_everyone_recent", {})[x["url"].split("?")[0]] = time.time()
        if len(sent) > 5000:
            for k in sorted(sent, key=sent.get)[:1000]:
                sent.pop(k)

    def focus_report(self):
        """Are the 🔥 focus sets and @everyone products set up right? How many products each matches right now."""
        from .parsing import _norm, _phrase_in
        s = settings_mod.load()
        cat = self._catalog()
        lines = ["**🔥 Focus sets** · products the scanners currently see from each set"]
        for name in s.get("focus_sets") or []:
            hits = [r for r in cat if ("pokemon" in _norm(r["name"]) or "tcg" in _norm(r["name"]))
                    and _phrase_in(name, _norm(r["name"]))]
            ex = "; ".join(r["name"][:45] for r in hits[:2])
            lines.append(f"{'✅' if hits else '⚠️'} **{name}** · {len(hits)}" + (f" · e.g. {ex}" if ex else
                         " · nothing right now (spelling, or simply not in stores at the moment)"))
        lines.append("\n**@everyone products**")
        for name in s.get("everyone_products") or []:
            hits = [r for r in cat if _phrase_in(name, _norm(r["name"]))]
            ex = "; ".join(r["name"][:45] for r in hits[:2])
            lines.append(f"{'✅' if hits else '⚠️'} **{name}** · {len(hits)}" + (f" · e.g. {ex}" if ex else
                         " · nothing right now"))
        text, chunks = "", []
        for ln in lines:
            if len(text) + len(ln) > 1900:
                chunks.append(text)
                text = ""
            text += ln + "\n"
        chunks.append(text)
        return chunks

    def _focus(self, name):
        """M's focus list from Settings: 🔥 focus Pokémon sets (box / bundle / ETB = @everyone) and the @everyone
        product list (2025-26 Topps basketball / Cooper Flagg...)."""
        s = settings_mod.load()
        return focus_match(name, s.get("focus_sets"), s.get("everyone_products"))

    def _wanted_sports_card(self, text):
        """Sealed sports cards in your sports only (Settings: baseball / basketball / football) - no soccer, WWE, F1."""
        wanted = set(settings_mod.load().get("sports") or ["Baseball", "Basketball", "Football"])
        return is_sports_card_product(text) and sport_of(text) in wanted

    def _wanted_card(self, text):
        return is_pokemon_product(text) or self._wanted_sports_card(text)

    def _product_alert(self, level, title, x, store_label, extra=None, channel=None, copy_to=None, store=None,
                       ping=False, strict=False, record=True):
        fields = {"Price": x.get("price"), "Stock": x.get("stock") or x.get("status"), "Limit": x.get("limit")}
        fields.update(extra or {})
        links = self._buy_links(x)
        self.notify.send(level, title, x["url"], fields, image=x.get("image", ""), channel=channel, copy_to=copy_to,
                         links=links, store=store, product=True, ping=ping, strict=strict, record=record)

    @staticmethod
    def _buy_links(x):
        """🛒 ADD TO CART / ⚡ BUY NOW where the store has direct links; otherwise the product page is the cart button."""
        if x.get("add_to_cart") or x.get("buy_now"):
            return [("🛒 Add to cart", x.get("add_to_cart")), ("⚡ Buy now", x.get("buy_now")), ("🔗 Product page", x["url"])]
        return [("🔗 Product page (tap Add to cart there)", x["url"])]

    DRAW_OPEN_WORDS = ("enter drawing", "enter the drawing", "enter now", "entry open", "entries open",
                       "drawing open", "drawing is open", "drawing ends", "drawing closes", "enter for a chance")

    @staticmethod
    def _norm(name):
        return re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("é", "e")).strip()

    def check_walmart_drawings(self, t, st, first):
        """walmart.com/shop/collectibles/draw - Walmart's limited-time drawings (lotteries).
        Pokémon / sports-card items only. Announces each drawing once when it appears (with its open time),
        then drawings_tick() - which runs on the clock every few seconds, not on this page's scan - pings
        15 min before, 1 min before and the minute it opens, plus 30 min before it closes."""
        status, final, html = self._fetch(t, st)
        self._save_debug(t["name"], html)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        tiles = parse_retail_tiles(html, final, "walmart")
        # the drawing page's tiles have no product links: add titles found on the page, and their drawing times
        by_name = {self._norm(x["name"]): x for x in tiles}
        for x in title_tiles(BeautifulSoup(html, "html.parser"), final, "walmart"):
            same = by_name.get(self._norm(x["name"]))
            if same:
                if not drawing_window(same.get("text", ""))[0]:
                    same["text"] = (x["text"] + " " + same.get("text", ""))[:1500]
                same["image"] = same.get("image") or x["image"]
                same["price"] = same.get("price") or x["price"]
            else:
                tiles.append(x)
                by_name[self._norm(x["name"])] = x
        if not tiles:
            return "empty", status
        blocked = self._blocklist()
        cards = [x for x in tiles if self._wanted_card(x["name"] + " " + x["url"])
                 and not is_accessory_only(x["name"]) and not blocked_by(x["name"], blocked)]
        known = st.setdefault("drawings", {})
        now = datetime.now(CT)
        # the page's own visible text: if every drawing on it shows the same "Drawing starts ..." time, a tile we
        # couldn't read a time from gets that one
        page_text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
        page_starts = set()
        for m in re.finditer(r"drawing\s+(?:starts|opens|begins)", page_text, re.I):
            w = drawing_window(page_text[m.start(): m.start() + 80], now)[0]
            if w:
                page_starts.add(w.isoformat()[:16])
        page_start = datetime.fromisoformat(next(iter(page_starts))) if len(page_starts) == 1 else None
        n_open = n_upcoming = 0
        for x in cards:
            visible = x.get("text", "")
            if " ".join(x["name"].split()[:4]).lower() in page_text.lower():
                i = page_text.lower().find(" ".join(x["name"].split()[:4]).lower())
                visible = visible + " " + page_text[max(0, i - 200): i + 400]
            start, end, _ = drawing_window(visible, now)
            start, end = x.get("start") or start or page_start, x.get("end") or end
            closed = any(w in page_text[max(0, page_text.lower().find(x["name"][:30].lower())):][:500].lower()
                         for w in ("drawing closed", "drawing ended", "drawing has ended", "entries closed"))
            if start and start > now:
                closed = False
            phase = self._draw_phase(start, end, closed, x.get("text", ""), now)
            n_open += phase == "open"
            n_upcoming += phase == "upcoming"
            key = f"{x['id']}|{start.isoformat()[:16] if start else ''}"
            for k in [k for k in known if k.split("|")[0] == x["id"] and k != key]:
                known.pop(k)                    # same item with an older / missing time: replaced by this one
            kind = "pokemon" if is_tcg_product(x["name"]) else "sports"
            _, info = price_check(x["name"], x["price"], kind)
            rec = known.get(key)
            fresh = rec is None
            rec = rec or {}
            rec.update({"phase": phase, "name": x["name"], "seen": time.time(), "url": x["url"], "page": t["url"],
                        "price": x["price"], "image": x.get("image", ""), "limit": x.get("limit", ""),
                        "retail": msrp_text(info), "start": start.isoformat() if start else "",
                        "end": end.isoformat() if end else ""})
            known[key] = rec
            if start and start > now:
                self._add_event(f"Walmart drawing: {x['name']}", x.get("url") or t["url"], start, True, "drawing",
                                "walmart")
            if fresh and phase != "closed":
                if phase == "upcoming":
                    self._draw_ping(rec, f"🎟️ WALMART DRAWING · opens {fmt_when(start)} CT: {x['name']}")
                elif phase == "open":
                    self._draw_ping(rec, f"🎟️ WALMART DRAWING OPEN NOW: {x['name']}")
                    rec["announced_open"] = True
                else:
                    self._draw_ping(rec, f"🎟️ WALMART DRAWING listed (open time not shown yet): {x['name']}")
        for k in [k for k, v in known.items() if time.time() - v.get("seen", 0) > 14 * 86400]:
            known.pop(k)
        self.drawings_tick()
        return (f"ok ({len(cards)} card drawings · {n_upcoming} upcoming · {n_open} open · "
                f"{len(tiles) - len(cards)} other items skipped)", status)

    def _clock_ticks(self):
        try:
            self.drawings_tick()
        except Exception as e:
            log.warning("drawing reminders: %s", e)

    def _draw_phase(self, start, end, closed, text, now):
        if closed or (end and now >= end):
            return "closed"
        if start and now < start:
            return "upcoming"
        if start or any(w in (text or "").lower() for w in self.DRAW_OPEN_WORDS):
            return "open"
        return "listed"                       # no time on the page and nothing saying it's open: don't guess

    def _draw_ping(self, rec, title):
        start = datetime.fromisoformat(rec["start"]) if rec.get("start") else None
        end = datetime.fromisoformat(rec["end"]) if rec.get("end") else None
        fields = {"Price": rec.get("price"), "Opens (CT)": fmt_when(start) if start else "",
                  "Closes (CT)": fmt_when(end) if end else "", "Limit": rec.get("limit"),
                  "Retail (MSRP)": rec.get("retail")}
        if start and start > datetime.now(CT):
            fields["Calendar"] = (f"[Add to Google Calendar]({gcal_link('Walmart drawing: ' + rec['name'], start, rec['page'])})")
        # Walmart has no separate entry link: the "Enter drawing" button is ON the product page (signed in)
        if "/ip/" in rec["url"]:
            links = [("🎟️ OPEN PRODUCT → tap ENTER DRAWING", rec["url"]), ("All Walmart drawings", rec.get("page", ""))]
        else:     # Walmart's page didn't give this item its own link: the drawings page is where its Enter button is
            links = [("🎟️ OPEN WALMART DRAWINGS → tap ENTER on this item", rec["url"])]
        self.notify.send("urgent", title, rec["url"], fields, image=rec.get("image", ""), links=links, channel="walmart",
                         copy_to=["drawings"], store="walmart", product=True,
                         ping=is_etb_or_upc(rec["name"]) or self._focus(rec["name"])["ping"])

    def drawings_tick(self):
        """Clock-driven drawing reminders, checked every few seconds from stored open/close times, so the
        'opens in 15 min', 'opens in 1 min' and 'OPEN NOW' pings land on time no matter when the page was read."""
        now = datetime.now(CT)
        for t in self.targets():
            if t.get("type") != "walmart_drawings":
                continue
            known = self.state.get("targets", {}).get(t["name"], {}).get("drawings") or {}
            for rec in known.values():
                if not rec.get("start"):
                    continue
                start = datetime.fromisoformat(rec["start"])
                end = datetime.fromisoformat(rec["end"]) if rec.get("end") else None
                if rec.get("phase") == "closed" or now - start > timedelta(hours=2) and not end:
                    continue
                if not rec.get("soon") and timedelta(minutes=1, seconds=30) < start - now <= timedelta(minutes=15):
                    rec["soon"] = True
                    self._draw_ping(rec, f"⏰ Walmart drawing opens in {max(1, round((start - now).total_seconds() / 60))} "
                                         f"min ({self._clock(start)} CT): {rec['name']}")
                if not rec.get("one_min") and timedelta(0) < start - now <= timedelta(minutes=1, seconds=30):
                    rec["one_min"] = rec["soon"] = True
                    self._draw_ping(rec, f"⏰ Walmart drawing opens in 1 min ({self._clock(start)} CT): {rec['name']}")
                if not rec.get("announced_open") and start <= now < start + timedelta(minutes=30):
                    rec["announced_open"] = rec["soon"] = rec["one_min"] = True
                    rec["phase"] = "open"
                    self._draw_ping(rec, f"🟢 OPEN NOW · enter the Walmart drawing: {rec['name']}")
                if end and rec.get("announced_open") and not rec.get("closing") \
                        and timedelta(0) < end - now <= timedelta(minutes=30):
                    rec["closing"] = True
                    self._draw_ping(rec, f"⏳ Walmart drawing closes in {max(1, int((end - now).total_seconds() // 60))} "
                                         f"min: {rec['name']}")
                if end and now >= end:
                    rec["phase"] = "closed"

    def _save_debug(self, name, html):
        """Last page each source saw - bundled by Settings > Save diagnostics so parsers can be tuned."""
        try:
            d = paths.DATA_DIR / "debug"
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
            h = html or ""
            if len(h) > 3_000_000:      # big pages are mostly styling up top: keep the start AND the end
                h = h[:1_200_000] + "\n<!-- … middle cut … -->\n" + h[-1_800_000:]
            (d / f"{safe}.html").write_text(h, encoding="utf-8", errors="ignore")
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
        products = allp if t.get("all_products") else [p for p in allp if p["sport"] in wanted]
        times = self.state.get("topps_times", {})
        for p in products:          # the exact time from the product's own page when the card only shows a day
            tt = times.get(p["slug"])
            if tt and not p.get("has_time"):
                d = datetime.fromisoformat(tt["when"])
                if not p.get("when") or abs((d.date() - datetime.fromisoformat(p["when"]).date()).days) <= 3:
                    p["when"], p["has_time"], p["time_from_page"] = tt["when"], True, tt.get("what", "Drops")
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
        self._note_eql_links(html)

        # calendar changes don't post one by one: the whole #topps-calendar board is re-posted fresh at the
        # bottom with @everyone and a one-line list of what changed. Drop alerts (live / 15 min / open) go to #topps.
        # 1.0.62: only a product Topps has NEVER listed before counts as new (one that drops off the page for a
        # check and comes back is not new). New products = fresh board + @everyone; every other change (status,
        # time, removed) just edits the board in place, quietly.
        ever = set(st.get("ever") or []) | set(known)
        changes, added = [], []
        for p in products:
            d = datetime.fromisoformat(p["when"]) if p["when"] else None
            fields = {"When (CT)": self._when_text(p), "Store": "Topps", "Sport": p["sport"], "Status": p["status"]}
            if d and d > now - timedelta(days=1):
                self._add_event(f"Topps: {p['name']}", p["url"], d, p["has_time"], "topps", "topps")
            old = known.get(p["slug"])
            if first:
                continue
            if old is None:
                if p["slug"] not in ever:
                    added.append(f"🆕 added **{p['name']}** · {self._when_text(p)}")
                continue
            if p["status"] != old.get("status"):
                if p["status"] in LIVE_STATUSES:
                    self.notify.send("urgent", f"🚨 TOPPS LIVE: {p['name']} · {p['status']}", p["url"], fields, ping=True,
                                     channel="topps", links=[("Open on Topps", p["url"])],
                                     copy_to=["drawings"] if p["status"] == "Drawing open" else None)
                changes.append(f"🔄 **{p['name']}**: {old.get('status')} → {p['status']}")
            if p["when"] and old.get("when") and p["when"][:16] != old["when"][:16]:
                was = datetime.fromisoformat(old["when"])
                if p["has_time"] and not old.get("has_time") and d and d.date() == was.date():
                    changes.append(f"🕐 time announced for **{p['name']}**: {p.get('time_from_page', 'Drops')} "
                                   f"{self._clock(d)} CT")
                else:
                    changes.append(f"📅 **{p['name']}** moved: {fmt_when(was, old.get('has_time', False))} → "
                                   f"{self._when_text(p)}")
        gone = [v.get("name", k) for k, v in known.items() if k not in {p["slug"] for p in products}] if not first else []
        changes += [f"➖ removed **{n}**" for n in gone[:5]]
        st["ever"] = list(ever | {p["slug"] for p in products})[-1500:]
        if added:
            self._post_topps_calendar(added + changes, t)
        elif changes:
            self._quiet_topps_calendar(changes, t)

        # heads-up before each timed drop, and again the minute it opens - both link straight to the page
        lead = int(t.get("remind_minutes_before", 15))
        reminded = set(st.get("reminded", []))
        for p in products:
            if not (p["when"] and p["has_time"]):
                continue
            d = datetime.fromisoformat(p["when"])
            formats = self._format_lines(p["slug"])
            fields = {"When (CT)": fmt_when(d), "Store": "Topps", "Sport": p["sport"], "Status": p["status"]}
            soon_key, open_key = f"{p['slug']}|{p['when'][:16]}", f"{p['slug']}|{p['when'][:16]}|open"
            if soon_key not in reminded and now <= d <= now + timedelta(minutes=lead):
                mins = max(1, int((d - now).total_seconds() // 60))
                self.notify.send("urgent", f"⏰ Topps drop in {mins} min: {p['name']}", p["url"], fields, ping=True,
                                 desc=formats, links=[("Open on Topps", p["url"])], channel="topps")
                reminded.add(soon_key)
            if open_key not in reminded and d <= now <= d + timedelta(minutes=10):
                self.notify.send("urgent", f"🟢 OPEN NOW on Topps: {p['name']}", p["url"], fields, desc=formats, ping=True,
                                 links=[("Open on Topps", p["url"])], channel="topps")
                reminded.add(open_key)
        st["reminded"] = list(reminded)[-300:]
        st["products"] = {p["slug"]: {"status": p["status"], "when": p["when"], "has_time": p["has_time"],
                                      "name": p["name"]} for p in products}
        return f"ok ({len(products)} products)", status

    def _post_topps_calendar(self, changes, t):
        """The updated Topps calendar, posted fresh with @everyone and what changed (no separate messages)."""
        summary = "@everyone · Topps calendar updated\n" + "\n".join(f"• {c}" for c in changes[:12])
        if len(changes) > 12:
            summary += f"\n• …and {len(changes) - 12} more"
        for c in changes:
            self._record_alert({"level": "urgent", "title": "Topps calendar: " + re.sub(r"\*\*", "", c), "url": t["url"],
                                "fields": {}, "desc": "", "image": "", "links": [], "channel": "topps_calendar",
                                "at": datetime.now(timezone.utc).isoformat()})
        if self.topps_board.hook().startswith("http"):
            self.topps_board.tick(self._render_topps_board, force=True, repost=True, content=summary)
        else:   # no #topps-calendar webhook: one message with the changes to #topps / main instead
            self.notify.send("urgent", "📅 Topps calendar updated", t["url"], desc="\n".join(changes)[:3900],
                             channel=["topps", "main"], ping=True, record=False)

    # ------------------------------------------------------------ Pokémon release dates (1.0.64)
    def _add_pokemon_release(self, name, when, url, source, store="", set_release=False):
        """An official / announced Pokémon release date -> the drop calendar (🎴). The same product from another
        source (press site vs pokemon.com vs a drop tracker) is kept once."""
        if when < datetime.now(CT) - timedelta(days=1) or blocked_by(name, self._blocklist()):
            return False
        if set_release:          # press site: a whole set's release day ("Mega Evolution—Delta Reign")
            name = f"{name} (set release)"
        elif not is_card_only(name) and not re.search(r"booster|elite trainer|collection|tin|bundle|box|blister",
                                                        name, re.I):
            return False
        for ev in self.state.get("events", {}).values():
            if ev.get("kind") == "pokemon" and ev["start"][:10] == when.date().isoformat() and \
                    rel.similar(ev["title"].split(":", 1)[-1], name):
                return False
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
        self._add_event(f"Pokémon release: {name}", f"{url}#{slug}", when, False, "pokemon", store or "pokemon")
        return True

    def check_pokemon_releases(self, t, st, first):
        """pokemon.com's monthly 'Check Out Every Pokémon TCG Product Release in <Month>' round-up (this month and
        next): every product with its release day goes on the drop calendar. The calendar re-posts when one is new."""
        found, ok_pages, last = 0, 0, 0
        for pair in rel.month_article_urls(months=int(t.get("months", 2))):
            for url in pair:
                status, final, html = self.fetcher.get(url, bool(t.get("browser", True)))
                self._note_request(url)
                last = status
                if looks_blocked(status, html) or status >= 400 or len(html or "") < 2000:
                    continue
                ok_pages += 1
                self._save_debug(f"{t['name']}-{url.rsplit('-in-', 1)[-1]}", html)
                anchor = rel.article_anchor(url, html) or datetime.now(CT)
                for item in rel.dated_products(html, final, anchor):
                    found += self._add_pokemon_release(item["name"], item["when"], final, t["name"])
                    st.setdefault("items", {})[item["name"][:100]] = item["when"].date().isoformat()
                break                                   # one URL style answered: skip the other
        st["items"] = dict(list(st.get("items", {}).items())[-200:])
        if not ok_pages:
            return ("blocked" if last in (403, 429) or not last else "error"), last
        return (f"ok ({len(st['items'])} Pokémon releases known · {found} new)" if st.get("items")
                else f"ok (0 dated products in {ok_pages} article(s) read)"), last

    def check_release_schedule(self, t, st, first):
        """A page listing product release dates: press.pokemon.com's Pokémon TCG product schedule, or a drop
        tracker's month calendar (`grid: true`, pokemonrestock.com). Future dates -> the drop calendar."""
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        self._save_debug(t["name"], html)
        now = datetime.now(CT)
        if t.get("grid"):
            items = rel.calendar_grid(html, now)
            future = [i for i in items if i["when"].date() >= now.date()]
            new = 0
            for i in future:
                label = f"{STORE_NAMES.get(i['store'], i['store'].title())}{' drawing' if i.get('walmart_draw') else ''}"
                new += self._add_pokemon_release(f"{i['name']} ({label})", i["when"], final, t["name"], i["store"])
            st["items"] = len(items)
            return (f"ok ({len(items)} drops on the calendar · {len(future)} upcoming · {new} new)" if items
                    else "ok (0 drops listed on the calendar)"), status
        items = rel.press_schedule(html, final) or rel.dated_products(html, final, now - timedelta(days=30))
        new = sum(self._add_pokemon_release(i["name"], i["when"], i["url"], t["name"], set_release=i.get("set", False))
                  for i in items)
        future = [i for i in items if i["when"].date() >= now.date()]
        latest = max((i["when"] for i in items), default=None)
        st["latest"] = latest.date().isoformat() if latest else ""
        return (f"ok ({len(items)} products · {len(future)} upcoming · latest {latest:%b} {latest.day} · {new} new)"
                if items else "ok (0 dated products on the page)"), status

    # ------------------------------------------------------------ Topps raffles on EQL (launches.topps.com)
    def _note_eql_links(self, html):
        """Any Topps page that links an EQL launch (product pages, the calendar): queue it, so a raffle is caught
        even before it shows on the launch list."""
        for u in rel.eql_launch_links(html or ""):
            known = self.state.setdefault("eql_launches", {})
            if u not in known:
                self.state.setdefault("eql_found", {})[u] = time.time()

    def check_eql_launches(self, t, st, first):
        """launches.topps.com: Topps' entry-window raffles (EQL). Every launch page is opened for its name, price,
        limit and entry window. New raffle = @everyone (#watch) + #topps + #drawings with the exact EQL link;
        again when a coming-soon raffle opens, and 1 hour before entries close. Alert only: M enters himself."""
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        self._save_debug(t["name"], html)
        known = self.state.setdefault("eql_launches", {})
        listed = rel.eql_launch_links(html, final)
        extra = self.state.pop("eql_found", {}) or {}
        for u in list(listed) + [u for u in extra if u not in listed]:
            if u not in known:
                known[u] = {"first_seen": time.time(), "baseline": bool(first) and u in listed, "checked": 0,
                            "name": listed.get(u) or u.rsplit("/", 1)[-1].replace("-", " ").title()}
        now_ts, now = time.time(), datetime.now(CT)

        def due(r):
            if not r.get("checked"):
                return True
            if r.get("status") == "Closed":
                return False
            closes = datetime.fromisoformat(r["closes"]) if r.get("closes") else None
            soon = closes and closes - now <= timedelta(hours=3)
            return now_ts - r["checked"] >= (600 if soon else 1800)
        opened = 0
        for u in sorted((u for u, r in known.items() if due(r)), key=lambda u: bool(known[u].get("checked")))[:4]:
            r = known[u]
            ps, pf, ph = self._fetch({"name": t["name"], "url": u, "browser": t.get("browser", True)}, st)
            r["checked"] = now_ts
            if looks_blocked(ps, ph) or ps >= 400:
                continue
            opened += 1
            info = rel.parse_eql_launch(ph, u)
            prev = r.get("status")
            r.update({"name": info["name"], "image": info["image"], "price": info["price"], "limit": info["limit"],
                      "status": info["status"],
                      "opens": info["opens"].isoformat() if info["opens"] else r.get("opens"),
                      "closes": info["closes"].isoformat() if info["closes"] else r.get("closes")})
            for key, label in (("opens", "opens"), ("closes", "closes")):
                if r.get(key):
                    self._add_event(f"Topps raffle {label}: {r['name']}", f"{u}#{key}", datetime.fromisoformat(r[key]),
                                    True, "drawing", "topps")
            if blocked_by(r["name"], self._blocklist()):
                continue
            event = ""
            if not prev and not r.get("baseline") and info["status"] != "Closed":
                event = "🎟️ NEW TOPPS RAFFLE (EQL)" + (" · OPEN NOW" if info["status"] == "Open" else " · coming soon")
            elif prev == "Coming soon" and info["status"] == "Open":
                event = "🟢 TOPPS RAFFLE OPEN NOW (EQL)"
            if event:
                self._eql_post(event, r, u)
        for u, r in known.items():            # entries closing within the hour: one reminder
            if r.get("closes") and r.get("status") != "Closed" and not r.get("reminded") and not r.get("baseline"):
                c = datetime.fromisoformat(r["closes"])
                if now <= c <= now + timedelta(minutes=60):
                    r["reminded"] = True
                    self._eql_post(f"⏳ Topps raffle closes in {max(1, int((c - now).total_seconds() // 60))} min", r, u)
        # forget raffles closed for 30+ days
        for u in [u for u, r in known.items() if r.get("closes") and
                  datetime.fromisoformat(r["closes"]) < now - timedelta(days=30)]:
            known.pop(u, None)
        live = [r for r in known.values() if r.get("status") in ("Open", "Coming soon")]
        if not listed and not live:
            return "ok · no Topps raffles open right now", status
        return f"ok ({len(listed)} launches listed · {len(live)} open / coming · {opened} pages opened)", status

    def _eql_post(self, event, r, u):
        fields = {"Price": r.get("price") or "see page", "Limit": f"{r['limit']} per person" if r.get("limit") else "",
                  "Status": r.get("status", "")}
        if r.get("opens"):
            fields["Entries open (CT)"] = fmt_when(datetime.fromisoformat(r["opens"]), True)
        if r.get("closes"):
            fields["Entries close (CT)"] = fmt_when(datetime.fromisoformat(r["closes"]), True)
        fields["How it works"] = "Raffle - enter any time before it closes (speed doesn't matter), winners are drawn"
        self.notify.send("urgent", f"{event}: {r['name']}"[:250], u, {k: v for k, v in fields.items() if v},
                         channel="topps", copy_to=["drawings"], ping=True, image=r.get("image", ""),
                         links=[("🎟️ ENTER THE RAFFLE ON EQL", u)])

    # ------------------------------------------------------------ TCG Drop Radar's public post archive (1.0.67)
    RE_DROP_POST = re.compile(r"queue|restock|drop|drawing|raffle|lottery|invite|in stock|stock is loaded|loaded|"
                              r"pre-?orders?|live|went up|is up|window", re.I)

    def check_dropradar_archive(self, t, st, first):
        """buttondown.com/tcgdropradar: their posts ("Pokémon Center queue is live right now", "Walmart drawing Sept 23
        at 5pm ET", "Amazon invite: …"). The archive is public - no account / login. RSS first, else the archive page.
        A queue post = a second opinion on our own queue check: @everyone (#watch) + #pokemon-queue unless our checker
        already saw a queue in the last hour. Drawings / invites -> the store's channel + #drawings; restocks / loaded
        stock -> the store's channel. Card-collecting articles (pulls, prices) are skipped."""
        posts, status = {}, 0
        for url in t.get("rss", []):
            try:
                status, final, body = self.fetcher.get(url, False)
            except Exception as e:          # M's PC 10/18: "Remote end closed connection" -> use the browser below
                log.info("%s RSS: %s", t["name"], e)
                continue
            low_body = (body or "").lower()
            if status < 400 and ("<item" in low_body or "<entry" in low_body):
                for e in feedparser.parse(body).entries:
                    if e.get("link"):
                        posts[e["link"].split("?")[0].rstrip("/")] = " ".join((e.get("title") or "").split())
                if posts:
                    break
        if not posts:
            try:
                status, final, html = self._fetch(t, st)
            except Exception as e:          # plain request cut off: the hidden browser reads the archive page
                log.info("%s: %s - trying the browser", t["name"], e)
                st["auto_browser"] = True
                status, final, html = self.fetcher.get(t["url"], True)
            if looks_blocked(status, html):
                return "blocked", status
            if status >= 400:
                return "error", status
            soup = BeautifulSoup(html, "html.parser")
            pat = re.compile(r"/tcgdropradar/archive/[a-z0-9][a-z0-9-]+/?$", re.I)
            for a in soup.find_all("a", href=True):
                u = urljoin(final, a["href"].split("?")[0].split("#")[0]).rstrip("/")
                if not pat.search(u + "/"):
                    continue
                head = a.find(["h1", "h2", "h3", "h4", "strong"])
                text = " ".join((head.get_text(" ") if head else a.get_text(" ")).split())
                if len(text) > len(posts.get(u, "")):
                    posts[u] = text[:200]
        if not posts:
            return "ok (0 posts found)", status
        seen = set(st.get("seen", []))
        if not first:
            for u, title in [(u, ti) for u, ti in posts.items() if u not in seen][:5]:
                self._dropradar_post(title or u.rsplit("/", 1)[-1].replace("-", " "), u)
        st["seen"] = list(seen | set(posts))[-500:]
        return f"ok ({len(posts)} posts)", status

    def _dropradar_post(self, title, url):
        low = title.lower()
        if not self.RE_DROP_POST.search(low) or blocked_by(title, self._blocklist()):
            return
        store = store_in(title)
        if "pokemon center" in low.replace("é", "e") and "queue" in low:
            q = next((x for x in self.targets() if x.get("track_duration") and "pokemoncenter" in x.get("url", "")), {})
            qst = self.state.get("targets", {}).get(q.get("name", ""), {})
            hist = self.state.get("queue_history", [])
            recent = qst.get("active") or any(
                datetime.now(CT) - datetime.fromisoformat(h.get("end") or h["start"]) < timedelta(hours=1) for h in hist[-3:])
            ours = ("✅ our checker saw it too" if recent else
                    f"⚠️ our checker has NOT seen it ({qst.get('health') or 'not run yet'}) - open Pokémon Center now")
            self.notify.send("urgent", f"📰 TCG Drop Radar: {title}"[:250], url,
                             {"Our queue check": ours, "Open": "[pokemoncenter.com](https://www.pokemoncenter.com/)"},
                             channel=["pokemon_queue", "pokemon"], ping=not recent, alarm=not recent,
                             links=[("Open Pokémon Center", "https://www.pokemoncenter.com/")])
            return
        raffle = bool(re.search(r"drawing|raffle|lottery|invite", low))
        self.notify.send("urgent" if raffle else "normal", f"📰 TCG Drop Radar: {title}"[:250], url,
                         {"Store": STORE_NAMES.get(store, store or "—"), "Source": "TCG Drop Radar"},
                         channel=[store, "pokemon"] if store else ["drawings" if raffle else "pokemon"],
                         copy_to=["drawings"] if raffle and store else None, ping=raffle and is_etb_or_upc(title))

    def _quiet_topps_calendar(self, changes, t):
        """Status / time / removed changes: the board is edited where it is - no new post, no ping."""
        for c in changes:
            self._record_alert({"level": "normal", "title": "Topps calendar: " + re.sub(r"\*\*", "", c), "url": t["url"],
                                "fields": {}, "desc": "", "image": "", "links": [], "channel": "topps_calendar",
                                "at": datetime.now(timezone.utc).isoformat()})
        if self.topps_board.hook().startswith("http"):
            self.topps_board.tick(self._render_topps_board, force=True)

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
            self._note_eql_links(html)
            formats, page_when = parse_topps_product_page(html, final)
            self._remember_topps_time(p, BeautifulSoup(html, "html.parser").get_text(" "))
            old = pages.get(p["slug"], {})
            new_map = {f["handle"]: f for f in formats}
            baseline = first or p["slug"] not in seen_slugs or t.get("times_only")
            for f in formats:
                prev = old.get(f["handle"])
                fields = {"Product": p["name"], "Format": f["name"], "Price": f["price"], "Status": f["status"],
                          "Drops (CT)": self._when_text(p), "Buy / enter": f"[Open this format]({f['url']})"}
                if baseline:
                    continue
                links = self._buy_links(f)
                fields.pop("Buy / enter", None)
                fields["Stock"], fields["Limit"] = f.get("stock") or f["status"], f.get("limit")
                if prev is None:
                    level = "urgent" if f["status"] in LIVE_STATUSES or is_hot(p) else "normal"
                    self.notify.send(level, f"🆕 Topps {f['name']} listed · {f['status']}", f["url"], fields, ping=True,
                                     image=f.get("image", ""), links=links, store="topps", product=True)
                elif f["status"] != prev.get("status"):
                    if f["status"] in LIVE_STATUSES:
                        self.notify.send("urgent", f"🚨 LIVE: {f['name']} · {f['status']}", f["url"], fields, ping=True,
                                         image=f.get("image", ""), links=links, store="topps", product=True,
                                         copy_to=["drawings"] if f["status"] == "Drawing open" else None)
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

    # ------------------------------------------------------------ topps.com: every product as it loads
    def check_topps_sitemap(self, t, st, first):
        """topps.com lists every product it loads in numbered product sitemaps (newest = highest numbers).
        New sealed products of ANY line (sports, Disney, F1, Star Wars...) and every format (Hobby, Jumbo, Mega,
        Blaster, Value, hanger, cases) get opened and posted in #topps as one self-editing card each.
        @everyone when a new format loads and when it goes on sale / pre-order / drawing. First run: the latest
        products are recorded and carded quietly, a few pages per run."""
        status, final, index = self._fetch(t, st)
        if looks_blocked(status, index) or status >= 400:
            return ("blocked" if looks_blocked(status, index) else "error"), status
        nums = topps_sitemap_numbers(index)
        if not nums:
            return "empty", status
        urls = []
        for n in nums[-int(t.get("sitemaps", 2)):]:
            sm = {"name": t["name"], "url": f"https://www.topps.com/products/sitemap/{n}.xml",
                  "browser": t.get("browser", False)}
            ss, _, body = self._fetch(sm, st)
            if not looks_blocked(ss, body) and ss < 400:
                urls += [u for u in topps_product_urls(body) if u not in urls]
        sealed = [u for u in urls if is_topps_sealed(u)]
        if not sealed:
            return f"ok (0 sealed products in {len(urls)} newest listings)", status
        known = st.setdefault("products", {})
        now = time.time()
        for u in sealed:
            h = topps_handle(u)
            if h not in known:
                known[h] = {"url": u, "first_seen": now, "baseline": bool(first), "checked": 0,
                            "name": humanize_handle(h)}
        # what to open this run: brand-new first, then anything near its drop / recently changed, then the backlog
        hot_every, idle_every = t.get("hot_seconds", 120), t.get("idle_minutes", 30) * 60

        def due(rec):
            if not rec.get("checked") or rec.get("force"):
                return True
            fresh = now - rec.get("first_seen", now) < 3 * 86400
            near = False
            if rec.get("when"):
                dt = datetime.fromisoformat(rec["when"]).timestamp()
                near = dt - 7200 <= now <= dt + 6 * 3600
            wait = hot_every if (near or (fresh and not rec.get("live"))) else idle_every
            return now - rec["checked"] >= wait
        order = sorted((h for h, r in known.items() if due(r)),
                       key=lambda h: (not known[h].get("force"), known[h].get("baseline", False),
                                      bool(known[h].get("checked")),
                                      known[h].get("checked", 0)))
        opened = 0
        use_cards = self.cards.enabled("topps")
        for h in order[: t.get("max_pages_per_run", 4)]:
            rec = known[h]
            ps, pf, ph = self._fetch({"name": t["name"], "url": rec["url"], "browser": t.get("browser", False)}, st)
            rec["checked"] = now
            rec.pop("force", None)
            if looks_blocked(ps, ph) or ps >= 400:
                continue
            opened += 1
            self._note_eql_links(ph)
            item = parse_topps_item_page(ph, rec["url"])
            prev_status, was_live, new = rec.get("status"), rec.get("live"), not rec.get("status")
            rec.update({k: v for k, v in item.items() if v not in ("", None) or k in ("live",)})
            rec["retail"], rec["ratio"] = "Topps' own price", None
            if blocked_by(rec.get("name", ""), self._blocklist()):        # ❌'d in Discord
                if rec.get("carded"):
                    self.cards.retire("topps", h)
                    rec["carded"] = False
                continue
            rec["focus"] = self._focus(rec.get("name", ""))["focus"]
            event = ""
            if new and not rec.get("baseline"):
                rec["loaded_before_stock"] = not rec["live"]
                event = f"🆕 NEW ON TOPPS · {rec['status']}"
            elif not new and rec["live"] and not was_live:
                event = f"🚨 LIVE ON TOPPS · {rec['status']}"
            if rec["live"]:
                rec["loaded_before_stock"] = False
            if rec["status"] == "Sold out":
                rec.setdefault("sold_out_at", now)
            else:
                rec.pop("sold_out_at", None)
            quiet_sold_out = rec["status"] == "Sold out" and not rec.get("carded")   # old sell-outs: no card
            if use_cards and not quiet_sold_out:
                tping = bool(event)
                if tping and self.notify.everyone_only_in_watch():
                    self.notify.everyone_copy(f"{event}: {rec['name']}"[:250], rec["url"],
                                              {"Store": "Topps", "Price": rec.get("price"), "Status": rec.get("status"),
                                               "Limit": rec.get("limit")}, image=rec.get("image", ""),
                                              links=self._buy_links(rec), store="topps")
                    tping = False
                self.cards.request("topps", h, rec, bump=bool(event), ping=tping, headline=event)
                rec["carded"] = True
            elif event:
                self.notify.send("urgent", f"{event}: {rec['name']}", rec["url"],
                                 {"Price": rec.get("price"), "Status": rec["status"], "Limit": rec.get("limit")},
                                 image=rec.get("image", ""), links=self._buy_links(rec), store="topps", product=True,
                                 ping=True, channel="topps",
                                 copy_to=["drawings"] if rec["status"] == "Drawing open" else None)
            if event and (rec["status"] == "Pre-order" or is_presale(rec.get("name", ""))):
                self.notify.send("urgent", f"🛍️ PRESALE · {event}: {rec['name']}", rec["url"],
                                 {"Price": rec.get("price"), "Limit": rec.get("limit")}, image=rec.get("image", ""),
                                 links=self._buy_links(rec), store="topps", product=True, ping=True,
                                 channel="presale", strict=True, record=False)
            if event and rec["status"] == "Drawing open" and use_cards:
                self.notify.send("urgent", f"🎟️ TOPPS DRAWING OPEN: {rec['name']}", rec["url"],
                                 {"Price": rec.get("price"), "Limit": rec.get("limit")}, image=rec.get("image", ""),
                                 links=self._buy_links(rec), store="topps", product=True, ping=True,
                                 channel="drawings", strict=True)
        # sold out for 3 days: take the card down so #topps stays about what you can still get
        for h, rec in known.items():
            if rec.get("carded") and rec.get("status") == "Sold out" and now - rec.get("sold_out_at", now) > 3 * 86400:
                self.cards.retire("topps", h)
                rec["carded"] = False
        if len(known) > 1500:
            for h in sorted(known, key=lambda h: known[h].get("first_seen", 0))[: len(known) - 1500]:
                known.pop(h)
        self._link_formats_to_calendar(known)
        live = sum(1 for r in known.values() if r.get("live"))
        waiting = sum(1 for r in known.values() if not r.get("checked"))
        return (f"ok ({len(known)} sealed products tracked · {live} live · {opened} pages opened"
                + (f" · {waiting} still to open" if waiting else "") + ")"), status

    def _link_formats_to_calendar(self, known):
        """Show each calendar product's formats (from the product listings) in the app's Topps tab."""
        def key(name):
            return re.sub(r"[^a-z0-9]+", " ", re.sub(r"[®™]", "", (name or "").lower())).strip()
        found = {}
        for p in self.topps:
            base = key(p["name"])
            fmts = [{"name": r.get("name", ""), "url": r["url"], "price": r.get("price", ""),
                     "status": r.get("status", ""), "add_to_cart": r.get("add_to_cart", ""),
                     "buy_now": r.get("buy_now", "")}
                    for r in known.values() if r.get("status") and key(r.get("name")).startswith(base)]
            if fmts:
                found[p["slug"]] = fmts
        if found:
            with self._lock:
                self.topps_formats = {**self.topps_formats, **found}

    def _remember_topps_time(self, p, page_text):
        """Topps product pages say 'Available September 30 at 12pm ET' / 'Drawing opens Oct 1 at 11am ET' even
        when the calendar card only shows the day. Keep that time for the calendar."""
        day = datetime.fromisoformat(p["when"]).date() if p.get("when") else None
        w = best_time_for(page_text[:30000], day) or best_time_for(page_text[:30000])
        if not w or (day and abs((w[0].date() - day).days) > 3):
            return
        low = page_text.lower()
        what = "Drawing opens" if re.search(r"drawing\s+(?:opens|starts|begins)", low) else "Drops"
        self.state.setdefault("topps_times", {})[p["slug"]] = {"when": w[0].isoformat(), "what": what,
                                                                "at": time.time()}

    def _when_text(self, p):
        if not p.get("when"):
            return "Now / date not shown"
        return fmt_when(datetime.fromisoformat(p["when"]), p["has_time"])

    def _format_lines(self, slug):
        fmts = getattr(self, "topps_formats", {}).get(slug, [])
        return "\n".join(f"• [{f['name']}]({f['url']}) {f['price']} · {f['status']}" for f in fmts[:10])

    # ------------------------------------------------------------ drop calendar
    KIND_ICON = {"topps": "🃏", "drawing": "🎟️", "release": "⚡", "news": "⚡", "pokemon": "🎴"}

    def upcoming_events(self, days=30, past_hours=2):
        now = datetime.now(CT)
        out = []
        for uid, ev in self.state.get("events", {}).items():
            d = datetime.fromisoformat(ev["start"])
            if now - timedelta(hours=past_hours) <= d <= now + timedelta(days=days) or \
                    (not ev["has_time"] and d.date() == now.date()):
                out.append({**ev, "uid": uid, "icon": self.KIND_ICON.get(ev.get("kind"), "📰"),
                            "store_name": STORE_NAMES.get(ev.get("store"), ""),
                            "when_text": fmt_when(d, ev["has_time"]),
                            "gcal": gcal_link(ev["title"], d, ev["url"], all_day=not ev["has_time"])})
        out.sort(key=lambda e: (e["start"][:10], not e["has_time"], e["start"]))
        return out

    SPORT_ICON = {"Baseball": "⚾", "Basketball": "🏀", "Football": "🏈"}

    @staticmethod
    def _short(title):
        """'Topps: 2026 Topps Midnight Football' -> 'Midnight Football' (the icon already says Topps / sport)."""
        t = re.sub(r"[®™]", "", title or "")
        t = re.sub(r"^(?:topps|walmart drawing|target|walmart|best buy|amazon|pokémon center|pokémon release)\s*:\s*", "",
                   t, flags=re.I)
        t = re.sub(r"^pok[eé]mon (?:tcg|trading card game)\s*:\s*", "", t, flags=re.I)
        t = re.sub(r"^20\d\d(?:-\d\d)?\s+", "", t)
        t = re.sub(r"^topps\s+(?=\S)", "", t, flags=re.I)
        return " ".join(t.split())

    def _icon(self, e):
        if e.get("kind") == "drawing":
            return "🎟️"
        if e.get("kind") == "pokemon":
            return "🎴"
        if e.get("kind") == "topps" or "topps" in (e.get("url") or ""):
            return self.SPORT_ICON.get(sport_of(e.get("title", "")), "🃏")
        return "⚡"

    def _event_line(self, e, extra=""):
        d = datetime.fromisoformat(e["start"])
        t = f"**{d.hour % 12 or 12}:{d:%M} {'PM' if d.hour >= 12 else 'AM'}**" if e["has_time"] else "*time TBA*"
        return f"{t} · {self._icon(e)} [{self._short(e['title'])[:70]}]({e['url']}){extra}"

    def _day_lines(self, events, extra=lambda e: ""):
        """Calendar layout shared by #drop-calendar and #topps-calendar: a bold day header, then one short line per
        drop - time first, sport icon, short name linked to the product. Doubles (same name, same day) shown once."""
        lines, day, seen = [], None, set()
        for e in events:
            d = datetime.fromisoformat(e["start"])
            key = (d.date(), self._short(e["title"]).lower())
            if key in seen:
                continue
            seen.add(key)
            if d.date() != day:
                day = d.date()
                today = datetime.now(CT).date()
                label = "Today" if day == today else "Tomorrow" if day == today + timedelta(days=1) else f"{d:%a} {d:%b} {d.day}"
                lines.append(f"\n__**{label}**__")
            lines.append(self._event_line(e, extra(e)))
        return lines

    def _render_calendar(self):
        lines = self._day_lines(self.upcoming_events(days=14))
        later = [e for e in self.upcoming_events(days=120) if e.get("kind") == "pokemon"
                 and datetime.fromisoformat(e["start"]) > datetime.now(CT) + timedelta(days=14)]
        if later:      # official Pokémon release dates further out: one line per day
            by_day = {}
            for e in later:
                by_day.setdefault(e["start"][:10], []).append(self._short(e["title"]))
            lines.append("\n__**🎴 Pokémon releases after that**__")
            for day, items in list(by_day.items())[:8]:
                d = datetime.fromisoformat(day)
                shown = ", ".join(items[:4]) + (f" +{len(items) - 4} more" if len(items) > 4 else "")
                lines.append(f"**{d:%a %b} {d.day}** · {shown}")
        legend = ("⚾🏀🏈🃏 Topps · 🎟️ drawing · 🎴 Pokémon release (official date) · ⚡ store product · times Central · "
                  "*time TBA* = day announced, time not yet")
        return "🗓️ Drop calendar · next 14 days", ("\n".join(lines).strip() + f"\n\n-# {legend}")

    def _render_topps_board(self):
        """Same layout as the drop calendar: every Topps product by day, linked to its Topps page."""
        dated, undated = [], []
        for p in self.topps:
            ev = {"title": p["name"], "url": p["url"], "kind": "topps", "start": p["when"], "has_time": p["has_time"],
                  "sport": p["sport"], "status": p["status"], "slug": p["slug"], "what": p.get("time_from_page")}
            (dated if p["when"] else undated).append(ev)
        dated.sort(key=lambda e: (e["start"][:10], not e["has_time"], e["start"]))

        def extra(e):       # only what's worth knowing: live status, a drawing, how many formats are listed
            n = len(self.topps_formats.get(e["slug"], []))
            tags = []
            if e.get("what") == "Drawing opens":
                tags.append("🎟️ drawing")
            if e["status"] in ("Pre-order", "On sale", "Drawing open"):
                tags.append(f"🟢 {e['status']}")
            if n:
                tags.append(f"{n} formats")
            return (" · " + " · ".join(tags)) if tags else ""
        lines = self._day_lines(dated, extra)
        if undated:
            lines.append("\n__**Date not shown right now**__")
            lines += [f"*now* · {self._icon(e)} [{self._short(e['title'])[:70]}]({e['url']}){extra(e)}" for e in undated]
        return (f"🃏 Topps release calendar · {len(self.topps)} products",
                "\n".join(lines).strip() + "\n\n-# ⚾🏀🏈🃏 sport · times Central · *time TBA* = Topps gave the day, not the time yet")

    # ------------------------------------------------------------ 💸 daily price list (🔥 sets + @everyone products)
    POKEMON_FORMATS = (("Booster Box", "Box"), ("Booster Bundle", "Bundle"), ("Elite Trainer Box", "ETB"))
    SPORTS_FORMATS = (("Hobby Box", "Hobby"), ("Mega Box", "Mega"), ("Blaster Box", "Blaster"))
    HOBBY_ONLY = ("impeccable", "national treasures", "crown royale", "court kings", "contenders", "flawless",
                  "immaculate", "noir", "spectra", "one and one", "eminence", "prizm draft picks", "university")

    def price_list_items(self):
        """-> [(section, row label, column label, eBay query, retail)] for every 🔥 focus set and @everyone product.
        Near-duplicate @everyone phrases ("NBA Hoops" / "Topps Hoops Basketball") become one row; names without a
        product line (Cooper Flagg) are left out - they're singles, which the app doesn't track."""
        from .msrp import typical_price
        from .parsing import DEFAULT_EVERYONE_PRODUCTS, DEFAULT_FOCUS_SETS
        s = settings_mod.load()
        sets = s.get("focus_sets")
        sets = DEFAULT_FOCUS_SETS if sets is None else sets
        everyone = s.get("everyone_products")
        everyone = DEFAULT_EVERYONE_PRODUCTS if everyone is None else everyone
        out = []
        for name in [x.strip() for x in sets if x.strip()]:
            for fmt, col in self.POKEMON_FORMATS:
                q = f"Pokemon {name} {fmt}"
                tp = typical_price(q.lower(), "pokemon")
                out.append(("pokemon", name, col, q, tp[0] if tp else None))
        seen = set()
        for phrase in [x.strip() for x in everyone if x.strip()]:
            if not re.search(r"\b(?:19|20)\d{2}\b", phrase):
                continue                              # a player name, not a product line
            words = re.sub(r"\bupdates\b", "update", phrase.lower())
            key = " ".join(w for w in words.split() if w not in ("topps", "nba", "basketball", "the"))
            if key in seen:
                continue
            seen.add(key)
            if not re.sub(r"\b(?:19|20)\d{2}(?:-\d{2,4})?\b", "", key).strip():
                continue                              # just a year + Topps ("2025-26 Topps") = the Flagship row
            label = re.sub(r"\s+basketball$", "", phrase, flags=re.I)
            hobby_only = any(w in phrase.lower() for w in self.HOBBY_ONLY)
            for fmt, col in self.SPORTS_FORMATS[:1] if hobby_only else self.SPORTS_FORMATS:
                q = f"{phrase} {fmt}"
                if "basketball" not in q.lower():
                    q = f"{phrase} Basketball {fmt}"
                tp = typical_price(q.lower(), "sports")
                out.append(("sports", label, col, q, tp[0] if tp else None))
        return out

    def _price_keys(self):
        return {self.resale.key(q) for *_, q, _ in self.price_list_items()}

    def _record_price(self, key, result):
        hist = self.state.setdefault("price_history", {}).setdefault(key, {})
        day = datetime.now(CT).date().isoformat()
        if result.get("median"):
            hist[day] = {"median": result["median"], "avg": result.get("avg") or result["median"],
                         "n": result.get("n", 0)}
        for d in sorted(hist)[:-30]:                  # keep a month
            hist.pop(d, None)
        self._prices_dirty = True

    def price_list_tick(self, force=False, reason="daily"):
        """Once a day (Settings price_list_hour, 8 AM CT) - or when M types `refresh` in the price-list channel -
        every item is looked up on eBay again (in the background, ahead of other valuing). When the answers are in
        (or after 20 min), a NEW price list message is posted; earlier days' lists stay above it as a record."""
        now = datetime.now(CT)
        run = self.state.get("price_refresh")
        if run:
            if not run.get("pending") or time.time() - run.get("started", 0) > 2700:
                self.state.pop("price_refresh", None)
                self._post_price_list(run.get("reason", "daily"))
            return 0
        hour = int(settings_mod.load().get("price_list_hour", 8))
        due = hour >= 0 and now.hour >= hour and self.state.get("price_list_day") != now.date().isoformat()
        if not force and not due:
            # items eBay didn't answer for: ask again every 30 min (in the background, ahead of other valuing)
            if time.time() - getattr(self, "_price_retry_at", 0) > 1800:
                self._price_retry_at = time.time()
                for *_, q, _ in self.price_list_items():
                    hit = self.resale.cache.get(self.resale.key(q)) or {}
                    if hit.get("ok") is False and time.time() - hit.get("at", 0) > 1800:
                        self.resale.cache.pop(self.resale.key(q), None)
                        self.resale.lookup(q, priority=1, force=True)
            if getattr(self, "_prices_dirty", False):         # a late answer: edit today's list
                self._prices_dirty = False
                self.prices_board.tick(self._render_prices, force=True)
            return 0
        self.state["price_list_day"] = now.date().isoformat()
        pending = []
        for *_, q, _ in self.price_list_items():
            k = self.resale.key(q)
            self.resale.cache.pop(k, None)
            self.resale.lookup(q, priority=1, force=True)       # ahead of background valuing, behind live drops
            if k not in pending:
                pending.append(k)
        self.state["price_refresh"] = {"pending": pending, "started": time.time(), "reason": reason}
        return len(pending)

    def _post_price_list(self, reason="daily"):
        """A fresh message at the bottom of the channel (the previous list is kept, not edited)."""
        now = datetime.now(CT)
        self.state.pop("board_prices", None)            # forget yesterday's message: post a new one
        self.prices_board.next = 0
        head = (f"💸 **Price list · {now:%a %b} {now.day}**" + (" · refreshed on request" if reason != "daily" else
                                                               " · daily update"))
        self.prices_board.tick(self._render_prices, force=True, content=head)
        self._prices_dirty = False

    def handle_prices_command(self, text):
        """Messages typed in the price-list channel."""
        low = " ".join((text or "").lower().split())
        if low in ("refresh", "update", "check", "check again", "prices refresh", "refresh prices", "recheck"):
            if self.state.get("price_refresh"):
                left = len(self.state["price_refresh"].get("pending", []))
                return [f"💸 Already refreshing · {left} lookups left. The new list posts here when they're done."]
            n = self.price_list_tick(force=True, reason="refresh")
            return [f"🔄 Checking eBay again for {n} items · about {max(2, n * 8 // 60)} min. "
                    "A new price list posts here when it's done."]
        if low in ("status", "ebay", "health", "why"):
            run = self.state.get("price_refresh") or {}
            st = self.resale.stats
            return [f"{self.resale.health()}\n"
                    f"Refresh running: {'yes · ' + str(len(run.get('pending', []))) + ' lookups left' if run else 'no'}\n"
                    f"Last eBay answer: {st.get('last_page') or ('ok' if st.get('ok') else 'none yet')} · "
                    f"last error: {st.get('last_error') or 'none'} · via {st.get('via') or '-'}"]
        if low in ("prices", "list", "show", "price list"):
            return [f"**{t}**\n{d}"[:1990] for t, d in self._render_prices()]
        return ["Type `refresh` to check eBay again now (a new list posts here when done), `prices` to see the "
                "current list, or `status` to see whether eBay is answering. A new list also posts every morning."]

    def _price_cell(self, key, retail=None):
        hist = self.state.get("price_history", {}).get(key) or {}
        days = sorted(hist)
        if not days:
            hit = self.resale.cache.get(key) or {}
            if hit.get("ok"):
                return "—"                                   # eBay answered: no matching sales
            if hit and hit.get("ok") is False:
                return "⚠️"                                  # eBay didn't answer (blocked / error)
            return "⏳"                                      # not looked up yet
        val = lambda d: hist[d].get("avg") or hist[d]["median"]      # average sale price (outliers dropped)
        cur = val(days[-1])
        cell = f"**${cur:,.0f}**"
        prev = next((val(d) for d in reversed(days[:-1])), None)
        if prev:
            pct = (cur - prev) / prev * 100
            if abs(pct) >= 1:
                cell += f" {'▲' if pct > 0 else '▼'}{abs(pct):.0f}%"
        if retail:
            cell += f" ({cur / retail:.1f}× retail)"
        return cell

    def _render_prices(self):
        items = self.price_list_items()
        rows = {}
        for section, label, col, q, retail in items:
            rows.setdefault((section, label), []).append(f"{col} {self._price_cell(self.resale.key(q), retail)}")
        poke = [f"🔥 **{label}** · " + " · ".join(cells) for (sec, label), cells in rows.items() if sec == "pokemon"]
        hoops = [f"🟢 **{label}** · " + " · ".join(cells) for (sec, label), cells in rows.items() if sec == "sports"]
        day = self.state.get("price_list_day") or "not run yet"
        cells = [self._price_cell(self.resale.key(q), None) for *_, q, _ in items]
        n_ok = sum(1 for c in cells if c.startswith("**"))
        n_none = cells.count("—")
        n_fail = cells.count("⚠️")
        n_wait = cells.count("⏳")
        run = self.state.get("price_refresh") or {}
        progress = (f"**{n_ok} priced · {n_none} no recent sales · {n_fail} eBay didn't answer · {n_wait} waiting**"
                    + (f" · refresh running, {len(run.get('pending', []))} left" if run else ""))
        foot = (f"\n-# Average eBay SOLD price of recent sales (lots / graded / empty boxes and far-off prices left out) · "
                f"checked once a day at {int(settings_mod.load().get('price_list_hour', 8)) % 12 or 12} "
                f"{'AM' if int(settings_mod.load().get('price_list_hour', 8)) < 12 else 'PM'} CT · last: {day} · "
                f"▲▼ = change since the previous day · — = no recent sales found · ⚠️ = eBay didn't answer (retries "
                f"in 30 min) · ⏳ = not looked up yet")
        out = [("💸 Price list · status", progress + (f"\n⚠️ eBay: {self.resale.stats.get('last_page') or self.resale.stats.get('last_error')}"
                                                      if n_fail else ""))]
        health = self.resale.health()
        if "❌" in health or self.resale.stats.get("last_page"):
            warn = health + (f" · last answer: {self.resale.stats['last_page']}" if self.resale.stats.get("last_page")
                             and "❌" in health else "")
            if "❌" in health:
                poke = [f"**{warn}**"] + poke
        if poke:
            out.append(("💸 Price list · 🔥 Pokémon focus sets", "\n".join(poke)))
        if hoops:
            out.append(("💸 Price list · 🟢 @everyone basketball", "\n".join(hoops)))
        if not out:
            out.append(("💸 Price list", "Add focus sets / @everyone products in Settings → Focus."))
        title, desc = out[-1]
        out[-1] = (title, desc + foot)
        return out
    # ------------------------------------------------------------ daily health check (1.0.62)
    HEALTH_NOTHING_HOURS = 24      # a source that loads but hasn't found a single product for this long gets 🟡

    def health_tick(self):
        """Once a day (Settings health_hour, 9 AM CT by default): one report card in #app-status."""
        hour = int(settings_mod.load().get("health_hour", 9))
        now = datetime.now(CT)
        if hour < 0 or now.hour < hour or self.state.get("health_day") == now.date().isoformat():
            return
        if not self.first_pass_done:
            return
        self.state["health_day"] = now.date().isoformat()
        title, desc = self.render_health(now)
        self.notify.send("system", title, desc=desc, channel="status")

    def render_health(self, now=None):
        """Every source and feature: ✅ working and finding things · 🟡 loads but finds nothing · ❌ failing,
        with the last time each one actually worked."""
        now = now or datetime.now(CT)
        t_now = time.time()
        ago = lambda ts: (self._dur(timedelta(seconds=t_now - ts)) + " ago") if ts else "never"
        tstate = self.state.get("targets", {})
        bad, quiet, good = [], [], []
        for t in self.targets():
            st = tstate.get(t["name"], {})
            h = st.get("health", "")
            if not h:
                continue
            if h.split(" ")[0].rstrip(":") in ("blocked", "error", "empty"):
                since = st.get("bad_since")
                bad.append(f"❌ **{t['name']}** · {h[:90]} · failing for {self._dur(timedelta(seconds=t_now - since)) if since else '?'}"
                           f" · last worked {ago(st.get('last_ok_at'))}")
                continue
            found = st.get("last_found_at")
            running = t_now - st.get("since", t_now)
            if (found and t_now - found > self.HEALTH_NOTHING_HOURS * 3600) or (
                    not found and running > self.HEALTH_NOTHING_HOURS * 3600):
                quiet.append(f"🟡 **{t['name']}** · page loads but nothing found · last found something {ago(found)}")
            else:
                good.append(t["name"])
        total = len(bad) + len(quiet) + len(good)
        lines = [f"**{len(good)} of {total} sources working** · {len(quiet)} finding nothing · {len(bad)} failing"]
        if bad:
            lines += ["", "**Needs a look**"] + bad
        if quiet:
            lines += ["", "**Loads, but finds nothing** (fine for a quiet store; days in a row = the page changed)"] + quiet
        if good:
            lines += ["", "✅ " + " · ".join(good)]
        items = self.price_list_items()
        cells = [self._price_cell(self.resale.key(q), None) for *_, q, _ in items]
        n_priced = sum(1 for c in cells if c.startswith("**"))
        qt = next((t for t in self.targets() if t.get("track_duration") and "pokemoncenter" in t.get("url", "")), None)
        qst = tstate.get(qt["name"], {}) if qt else {}
        day_ago = t_now - 86400
        n_alerts = sum(1 for a in self.state.get("alerts", [])
                       if datetime.fromisoformat(a["at"]).timestamp() >= day_ago and a.get("level") == "urgent")
        lines += ["", "**Features**",
                  f"🎟️ Pokémon Center queue check: {qst.get('health') or 'not run'} · last good check {ago(qst.get('last_ok_at'))}"
                  + (f" · ⚠️ blocked {qst.get('blocked_streak')} in a row" if qst.get("blocked_streak") else "")
                  + (f"\n   ↳ yesterday: {self._queue_day_line(1)}" if self._queue_day_line(1) else ""),
                  self.stock_health(), self.topps_fast_health(), self.resale.health(),
                  f"💸 Price list: {n_priced} of {len(cells)} priced · last run {self.state.get('price_list_day') or 'never'}",
                  f"🤖 Discord bot: {getattr(self.bot, 'state', '') or 'not started'}",
                  f"📣 Alerts in the last 24 h: {n_alerts}"]
        return f"🩺 Daily health check · {now:%a %b} {now.day}", "\n".join(lines)[:4000]

    def recap_tick(self):
        """Once a day (Settings recap_hour, 9 PM CT by default): what dropped today, how fast it sold out, by store."""
        hour = int(settings_mod.load().get("recap_hour", 21))
        now = datetime.now(CT)
        if hour < 0 or now.hour < hour or self.state.get("recap_day") == now.date().isoformat():
            return
        self.state["recap_day"] = now.date().isoformat()
        title, desc = self.render_recap(now)
        self.notify.send("system", title, desc=desc, channel="status")

    def render_recap(self, now=None):
        from collections import Counter
        now = now or datetime.now(CT)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        alerts = [a for a in self.state.get("alerts", []) if a.get("level") == "urgent"
                  and datetime.fromisoformat(a["at"]).timestamp() >= start]
        drops = [a for a in alerts if any(w in a["title"] for w in ("IN STOCK", "DRAWING", "INVITE", "LIVE", "just got"))]
        sold = [s for s in self.state.get("sellouts", []) if s["at"] >= start]
        by_store = Counter(STORE_NAMES.get(s, s) for s in (a.get("channel", "") for a in drops) if s)
        lines = [f"**{len(drops)} drops** today · **{len(sold)} sold out**"]
        if by_store:
            lines.append("By store: " + " · ".join(f"{k} {v}" for k, v in by_store.most_common()))
        fast = sorted(sold, key=lambda s: s["minutes"])[:8]
        if fast:
            lines.append("\n**⚡ Fastest sellouts**")
            lines += [f"• {'🔥 ' if s.get('focus') else ''}[{s['name'][:60]}]({s['url']}) · {STORE_NAMES.get(s['store'], s['store'])}"
                      f" · gone in **{self._dur(timedelta(minutes=s['minutes']))}**" for s in fast]
        hot = [a for a in drops if a["title"].startswith("🔥") or "Focus" in str(a.get("fields", {}))][:8]
        if hot:
            lines.append("\n**🔥 Focus-list drops**")
            lines += [f"• [{a['title'][:80]}]({a['url']})" for a in hot]
        q = [h for h in self.state.get("queue_history", [])
             if datetime.fromisoformat(h["start"]).timestamp() >= start]
        if q:
            lines.append("\n**Pokémon Center queue**: " + " · ".join(
                f"{self._clock(datetime.fromisoformat(h['start']))} for {self._dur(timedelta(minutes=h['minutes']))}"
                for h in q))
        if not drops and not sold and not q:
            lines.append("Quiet day: nothing came in stock.")
        return f"📊 Daily recap · {now:%a %b} {now.day}", "\n".join(lines)[:4000]

    def calendar_tick(self):
        """Every loop: post newly found dates to #drop-calendar, remind 15 min before news-announced drops,
        send the 8 AM rundown, and keep the two self-updating boards current. Posts only to channels that
        have their own webhook."""
        if not self.first_pass_done:
            return
        events = self.state.get("events", {})
        now = datetime.now(CT)
        # 1.0.62: "new" = a product link the calendar has never had (a date that moves, or an entry that drops off
        # and comes back, is not new). New -> fresh board at the bottom; anything else edits the board in place.
        seen = self.state.get("cal_seen_urls")
        if seen is None:                                      # first run / upgrade: everything known is baseline
            seen = {ev["url"]: time.time() for ev in events.values()}
        added = []
        for ev in events.values():
            if ev["url"] not in seen and datetime.fromisoformat(ev["start"]) > now - timedelta(hours=1):
                added.append(ev)
            seen[ev["url"]] = time.time()
        cutoff = time.time() - 60 * 86400                     # forget links not on the calendar for 60 days
        self.state["cal_seen_urls"] = {u: ts for u, ts in seen.items() if ts > cutoff}
        self.state.pop("cal_announced", None)
        if added:
            # no separate "added" messages: the whole calendar is re-posted fresh at the bottom of #drop-calendar
            what = "\n".join(f"• {self.KIND_ICON.get(e.get('kind'), '⚡')} {e['title'][:120]}" for e in added[:10])
            self.cal_board.tick(self._render_calendar, force=True, repost=True,
                                content=f"🗓️ Calendar updated\n{what}")
        self._boards_tick()

    # ------------------------------------------------------------ channel boards (current state of each channel)
    def _all_boards(self):
        out = [(self.cal_board, self._render_calendar), (self.topps_board, self._render_topps_board),
               (self.drawings_board, self._render_drawings), (self.restock_board, self._render_restocks)]
        if not self.state.get("price_refresh"):       # mid-refresh: leave the last list alone until the new one posts
            out.append((self.prices_board, self._render_prices))
        return out

    def _retire_store_boards(self):
        """1.0.13: store channels switched from one list message to one message per product - remove the old list."""
        hooks = settings_mod.load().get("webhooks") or {}
        for key in self.old_store_boards:
            saved = self.state.pop(key, None) or {}
            if saved.get("id") and saved.get("hook") in hooks.values():
                try:
                    requests.delete(f"{saved['hook']}/messages/{saved['id']}", timeout=15)
                except requests.RequestException:
                    pass

    def _boards_tick(self, repost=False):
        self._retire_store_boards()
        if repost:
            self.cards.resend_all()
        if self.state.get("boards_redropped") != "1.0.14":        # new calendar layout: post it fresh once
            self.state["boards_redropped"] = "1.0.14"
            for board, render in ((self.topps_board, self._render_topps_board), (self.cal_board, self._render_calendar)):
                try:
                    board.tick(render, force=True, repost=True)
                except Exception as e:
                    log.warning("board %s: %s", board.channel, e)
        calendars = (self.cal_board, self.topps_board)
        for board, render in self._all_boards():
            try:
                # calendars move to the bottom of their channel ONLY when a new drop is added (1.0.62): a launch,
                # an update or "Sync all channels" just brings them up to date where they are
                board.tick(render, force=repost, repost=repost and board not in calendars)
            except Exception as e:
                log.warning("board %s: %s", board.channel, e)

    def request_sync(self):
        """Sync all channels: full scan now, then every channel's board is posted fresh at the bottom."""
        self.sync_at = time.time()
        self.scan_now()

    @staticmethod
    def _section(title, lines, limit=3600):
        text, shown = "", 0
        for ln in lines:
            if len(text) + len(ln) + 1 > limit:
                break
            text += ln + "\n"
            shown += 1
        if shown < len(lines):
            text += f"-# …and {len(lines) - shown} more (see the app)"
        return title, text.strip()

    def _store_items(self, store, max_age_hours=3):
        seen, now = {}, time.time()
        for t in self.targets():
            if t.get("type") != "retail_search" or t.get("store") != store:
                continue
            for pid, rec in (self.state.get("targets", {}).get(t["name"], {}).get("items") or {}).items():
                if rec.get("url") and now - rec.get("seen", 0) < max_age_hours * 3600 and rec.get("over") != "way_over":
                    seen[pid] = rec
        return sorted(seen.values(), key=lambda r: r["name"].lower())

    def _item_line(self, r):
        bits = [f"[{r['name'][:70]}]({r['url']})"]
        if r.get("price"):
            bits.append(r["price"])
        if r.get("stock"):
            bits.append(r["stock"])
        if r.get("over") == "over":
            bits.append("⚠️ above retail")
        if r.get("add_to_cart"):
            bits.append(f"[🛒 ATC]({r['add_to_cart']})")
        if r.get("buy_now"):
            bits.append(f"[⚡ Buy]({r['buy_now']})")
        return "• " + " · ".join(bits)

    def _render_store(self, store):
        name = STORE_NAMES.get(store, store)
        items = self._store_items(store)
        live = [r for r in items if r.get("live")]
        out = [r for r in items if not r.get("live")]
        parts = []
        if store == "walmart" and self._drawings_section(only_store="walmart"):
            parts.append(self._drawings_section(only_store="walmart"))
        parts.append(self._section(f"🟢 In stock at {name} · {len(live)}", [self._item_line(r) for r in live]))
        parts.append(self._section(f"⚪ Out of stock / not buyable yet · {len(out)}",
                                   [self._item_line(r) for r in out], limit=1600))
        return parts

    def _drawings_section(self, only_store=None):
        lines, now = [], time.time()
        for t in self.targets():
            st = self.state.get("targets", {}).get(t["name"], {})
            if t.get("type") == "walmart_drawings" and only_store in (None, "walmart"):
                for rec in (st.get("drawings") or {}).values():
                    if rec.get("phase") == "closed" or now - rec.get("seen", 0) > 86400 or not rec.get("url"):
                        continue
                    ct = datetime.now(CT)
                    start = datetime.fromisoformat(rec["start"]) if rec.get("start") else None
                    end = datetime.fromisoformat(rec["end"]) if rec.get("end") else None
                    if end and ct >= end:
                        continue
                    if start and ct < start:
                        when = f"⏰ opens **{fmt_when(start)} CT**"
                    elif start or rec.get("phase") == "open":
                        when = "🟢 OPEN NOW" + (f" · closes {fmt_when(end)} CT" if end else "")
                    else:
                        when = "open time not shown yet"
                    lines.append(f"• 🎟️ Walmart · [{rec['name'][:70]}]({rec['url']}) · {rec.get('price', '')} · {when}")
            if t.get("type") == "retail_search" and (only_store is None or t.get("store") == only_store):
                for rec in (st.get("items") or {}).values():
                    if str(rec.get("status", "")).startswith("Drawing") and now - rec.get("seen", 0) < 3 * 3600:
                        lines.append(f"• 🎟️ {STORE_NAMES.get(t['store'], t['store'])} · "
                                     f"[{rec['name'][:70]}]({rec['url']}) · {rec.get('price', '')} · open")
        if only_store in (None, "topps"):          # Topps raffles on EQL (open or opening soon)
            for u, r in (self.state.get("eql_launches") or {}).items():
                if r.get("status") not in ("Open", "Coming soon"):
                    continue
                when = ""
                if r.get("status") == "Coming soon" and r.get("opens"):
                    when = f"⏰ opens **{fmt_when(datetime.fromisoformat(r['opens']))} CT**"
                elif r.get("closes"):
                    when = f"🟢 OPEN · closes {fmt_when(datetime.fromisoformat(r['closes']))} CT"
                else:
                    when = "🟢 OPEN"
                lines.append(f"• 🎟️ Topps (EQL) · [{r.get('name', '')[:70]}]({u}) · {r.get('price', '')} · {when}")
        return self._section(f"🎟️ Drawings & raffles right now · {len(lines)}", lines) if lines else None

    def _render_drawings(self):
        """#drawings: the board exists ONLY while a raffle / drawing is caught - no 'nothing right now' message."""
        sec = self._drawings_section()
        return [sec] if sec else None

    def _render_formats(self):
        lines = []
        for p in self.topps:
            fmts = self.topps_formats.get(p["slug"], [])
            if not fmts:
                continue
            lines.append(f"**[{p['name']}]({p['url']})** · {self._when_text(p)}")
            for f in fmts:
                bits = [f"[{f['name'][:60]}]({f['url']})", f.get("price", ""), f.get("status", "")]
                if f.get("add_to_cart"):
                    bits.append(f"[🛒 ATC]({f['add_to_cart']})")
                if f.get("buy_now"):
                    bits.append(f"[⚡ Buy]({f['buy_now']})")
                lines.append("  • " + " · ".join(b for b in bits if b))
        if not lines:
            lines = ["No formats listed on Topps product pages right now. They appear here when Topps lists "
                     "Hobby / Mega / Blaster boxes for a product."]
        return [self._section("🃏 Topps formats listed now", lines, limit=3900)]

    # ------------------------------------------------------------ calendar + csv
    def _when_fields(self, title, url, when, fields, kind="news", store=None):
        """A date mentioned in news: shown on the post with a Google Calendar link. It does NOT go on the drop
        calendar - that is only the Topps calendar, drawings, and products that show a date or countdown."""
        if not when:
            return
        start, has_time, tz_note = when
        fields["When (CT)"] = fmt_when(start, has_time) + tz_note
        fields["Calendar"] = f"[Add to Google Calendar]({gcal_link(title, start, url, all_day=not has_time)})"

    def _add_event(self, title, url, start, has_time, kind="news", store=None, product=False):
        """Everything with a date lands here: the drop calendar (app tab, drops.ics, #drop-calendar)."""
        events = self.state.setdefault("events", {})
        uid = hashlib.sha1(f"{url}|{start.date()}".encode()).hexdigest()[:16]
        if kind not in ("topps", "drawing", "pokemon") and not product:
            return
        if uid in events and events[uid]["start"] == start.isoformat() and events[uid].get("kind") == kind:
            return
        events[uid] = {"title": title, "url": url, "start": start.isoformat(), "has_time": has_time,
                       "kind": kind, "store": store or "", "product": bool(product)}
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

    def status_values(self):
        now = datetime.now(CT)
        mins = int((now - self.started).total_seconds()) // 60
        with self._lock:
            health = dict(self.health)
        bad = [f"❌ {k}: {v['health']}" for k, v in health.items() if not v["health"].startswith("ok")]
        ok = len(health) - len(bad)
        return {"uptime": f"{mins // 60}h {mins % 60}m", "version": __version__,
                "last_scan": (fmt_when(self.last_scan) + " CT") if self.last_scan else "starting up",
                "sources_ok": f"{ok}/{len(health)}", "alerts_today": self.alerts_today(),
                "problems": "\n".join(bad[:8]), "problems_count": len(bad),
                "state": "paused" if settings_mod.load().get("paused") else "running",
                "time": self._clock(now) + " CT"}

    def bot_reply(self):
        """The bot's answer, from the wording in Settings. {placeholders} fill in live values."""
        from collections import defaultdict
        tpl = settings_mod.load().get("bot_reply") or settings_mod.DEFAULTS["bot_reply"]
        vals = defaultdict(str, self.status_values())
        try:
            text = tpl.replace("\\n", "\n").format_map(vals)
        except (ValueError, IndexError):
            text = tpl
        if vals["state"] == "paused":
            text = "⏸️ **Rip Radar is on, but paused** (no scanning until you press Resume).\n" + text
        return "\n".join(line for line in text.split("\n")).strip() or self.status_text()

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
                "calendar": self.upcoming_events(days=30),
                "queue_history": self.state.get("queue_history", [])[:10],
                "sync_pending": bool(self.sync_at),
                "drop_mode": self.drop_mode(),
                "bot": self.bot.state,
            }

    def _load_state(self):
        try:
            return json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"targets": {}, "events": {}, "alerts": []}

    def _save_state(self):
        from .stock import GUARD
        self.state["target_guard"] = {"pause_until": GUARD["pause_until"], "pauses": GUARD["pauses"]}
        data = None
        for _ in range(5):            # the queue / card threads may be changing a dict this instant: retry
            try:
                with self._lock:
                    data = json.dumps(self.state, indent=1)
                break
            except RuntimeError:
                time.sleep(0.05)
        if data is None:
            return
        tmp = paths.STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(paths.STATE_FILE)
        except OSError as e:
            log.warning("couldn't save state yet: %s", e)
