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
from .notify import CHANNELS, STORE_NAMES, ChatBot, LiveBoard, Notifier, StatusBoard, store_in
from .parsing import (CT, best_time_for, is_topps_sealed, parse_topps_item_page, topps_handle, topps_product_urls,
                      topps_sitemap_numbers, LIVE_STATUSES, RETAIL_STORES, categorize, extract_when, fmt_when, gcal_link, is_card_product,
                      is_etb_or_upc, is_pokemon_product, is_sports_card_product, is_tcg_product, looks_blocked, parse_retail_tiles, parse_topps_calendar,
                      parse_topps_product_page, humanize_handle, tile_status, drawing_window, next_data, sport_of, stock_hint,
                      title_tiles)
from .cards import ProductCards
from .stock import page_data_stock, target_key_in, target_stock, target_stock_text

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
    def __init__(self, browser_fetch=None):
        paths.ensure_dirs()
        self.builtin = load_builtin()
        self.state = self._load_state()
        self.state.setdefault("alerts", [])
        self.fetcher = Fetcher(browser_fetch)
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
        self.formats_board = LiveBoard(settings_mod.load, "topps", self.state, "board_topps", every_minutes=5,
                                       color=STORE_COLORS.get("topps"))
        self.sync_at = 0
        self.startup_sync = False
        self.bot = ChatBot(self.status_text, settings_mod.load, self.bot_reply)
        self.started = datetime.now(CT)
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
                t["all_products"] = s.get("topps_all_products", True)
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
            pass_started = time.time()
            targets = self.targets()
            pending = {t["name"] for t in targets}
            while not self._stop.is_set():
                # one source at a time, most overdue first (relative to its interval), so fast sources like the
                # Pokémon Center queue stay on time even when slow store pages pile up
                now = time.time()
                # the queue watcher may run again mid-pass: a full pass takes minutes, the queue can't wait that long
                due = [t for t in targets if (t["name"] in pending or t.get("track_duration"))
                       and now >= self.next_due.get(t["name"], 0)
                       and (t["type"] == "walmart_drawings" or t.get("track_duration")   # never paused
                            or now >= self.host_backoff.get(self._host(t), (0, 0))[0])]
                if not due:
                    break
                t = max(due, key=lambda t: (now - self.next_due.get(t["name"], 0))
                        / t.get("interval_seconds", default_iv))
                pending.discard(t["name"])
                self._run_target(t, report)
                self._clock_ticks()
                iv = t.get("interval_seconds", default_iv)
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
                                     desc=f"{ok}/{len(report)} sources working.\n" + "\n".join(bad)[:3500])
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
        self._save_debug("pokemon-center-queue-last-check", f"<!-- {final} -->\n" + (html or ""))
        if active:
            self._save_debug("pokemon-center-queue-page", f"<!-- {final} -->\n" + (html or ""))
        if blocked and not active:
            st["blocked_streak"] = st.get("blocked_streak", 0) + 1
            if was is not True:
                self._queue_watch(t, st, f"⚠️ **Couldn't check the Pokémon Center queue** at {self._clock(now)} CT · the "
                                         f"site turned the check away. Retrying every minute.")
            return ("blocked" if looks_blocked(status, html) else "error"), status
        st["blocked_streak"] = 0
        self._track_queue(t, st, active, was, detected)
        st["active"] = active
        since = st.get("live_since")
        if active and since:
            return f"ok · LIVE for {self._dur(now - datetime.fromisoformat(since))}", status
        return "ok · not live", status

    def test_queue_alert(self):
        t = next((x for x in self.targets() if x.get("track_duration")), {"url": "https://www.pokemoncenter.com/",
                                                                            "channel": ["pokemon_queue", "pokemon"]})
        now = datetime.now(CT)
        return self.notify.send("urgent", "🧪 TEST · 🚨 POKÉMON CENTER QUEUE IS LIVE", t["url"],
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

    def _track_queue(self, t, st, active, was, detected):
        now = datetime.now(CT)
        history = self.state.setdefault("queue_history", [])
        if active and was is not True:
            st["live_since"] = now.isoformat()
            st.pop("session_msg", None)
            self.notify.send("urgent", t.get("alert_title", "🚨 POKÉMON CENTER QUEUE IS LIVE"), t["url"],
                             {"Went up (CT)": self._clock(now), "Detected": detected},
                             links=[("Join the queue", t["url"])], ping=True)
        if not active:
            # one self-editing line so you can see it's watching; nothing new is posted until a queue goes up
            self._queue_watch(t, st, f"⚪ **No Pokémon Center queue right now** · last checked {self._clock(now)} CT"
                                     f"\n-# Checked every minute. You'll get an @everyone the moment a queue goes up.",
                              repost=was is True)
        else:
            self._queue_watch(t, st, f"🟢 **Queue is UP** since {self._clock(datetime.fromisoformat(st['live_since']))} CT "
                                     f"· see the alert below")
        if active:
            since = datetime.fromisoformat(st.get("live_since") or now.isoformat())
            self._queue_live_message(t, st, f"🟢 **Pokémon Center queue is LIVE** · up for **{self._dur(now - since)}** "
                                            f"(since {self._clock(since)} CT)\n-# Last checked {self._clock(now)} CT")
        elif was is True:
            since = datetime.fromisoformat(st.pop("live_since", now.isoformat()))
            dur = self._dur(now - since)
            history.insert(0, {"start": since.isoformat(), "end": now.isoformat(), "minutes": int((now - since).total_seconds() // 60)})
            del history[20:]
            self._queue_live_message(t, st, f"⚪ Pokémon Center queue closed · was up **{dur}** "
                                            f"({self._clock(since)}–{self._clock(now)} CT)")
            st.pop("session_msg", None)
            recent = "\n".join(f"• {datetime.fromisoformat(h['start']):%a %b} {datetime.fromisoformat(h['start']).day}: "
                               f"{self._clock(datetime.fromisoformat(h['start']))}–{self._clock(datetime.fromisoformat(h['end']))}"
                               f" ({self._dur(timedelta(minutes=h['minutes']))})" for h in history[:5])
            self.notify.send("normal", f"⚪ Pokémon Center queue closed · was up {dur}", t["url"],
                             {"Went up (CT)": self._clock(since), "Closed (CT)": self._clock(now), "Up for": dur},
                             desc=f"Recent queues:\n{recent}")

    def check_feed(self, t, st, first):
        """News / Reddit RSS: new posts about raffles, drawings, invites, queues or drop times."""
        status, final, body = self.fetcher.get(t["url"])
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
            is_raffle = any(w in head for w in ("drawing", "raffle", "lottery", "invite", "sweepstakes"))
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
                                 channel=store or "drawings", copy_to=["drawings"], ping=is_etb_or_upc(title))
            else:
                # a product announcement: main alerts channel, keeping store channels products-only
                self.notify.send(level, "📣 " + title[:240], link, fields, desc=summary[:300], channel="main",
                                 ping=is_etb_or_upc(title) and any(w in blob for w in ("restock", "drop", "live", "in stock",
                                                                                         "pre-order", "preorder")))
            sent += 1
            if sent >= t.get("max_per_run", 8):
                break
        st["seen"] = list(seen)[-2000:]
        del shared[:-3000]
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
        keep = {"pokemon": is_pokemon_product, "sports": self._wanted_sports_card}.get(t.get("products", "cards"),
                                                                                       self._wanted_card)
        if store == "pokemon":
            keep = is_tcg_product       # everything at Pokémon Center is Pokémon, names don't always say so
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
                       "retail": msrp_text(info), "ratio": info.get("ratio")}
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
            big = is_etb_or_upc(x["name"])                      # ETB / UPC: @everyone in any channel
            listed_at = (prev or {}).get("first_seen")
            up_before = ""
            if listed_at and not (prev or {}).get("live"):
                atleast = "at least " if (prev or {}).get("baseline") else ""
                up_before = f"{atleast}{self._dur(timedelta(seconds=now - listed_at))} before stock"
                extra["Link was up"] = up_before
            event = None
            if not first and x["live"] and (prev is None or not prev.get("live")):
                if x["status"].startswith("Drawing"):
                    event = f"🎟️ DRAWING / INVITE OPEN at {label}"
                else:
                    event = f"🟢 {'NEW & IN STOCK' if prev is None else 'BACK IN STOCK'} at {label}"
            elif not first and prev is None and t.get("alert_new_listed", True):
                event = f"🆕 Loaded at {label}, not in stock yet"   # page is up before stock: track it
            rec = known.setdefault(x["id"], {})
            if "first_seen" not in rec:
                rec["first_seen"], rec["baseline"] = now, bool(first)
                rec["loaded_before_stock"] = not first and not x["live"]
            if x["status"] != "Listed" or "live" not in rec:
                rec["live"] = x["live"]
            if rec["live"]:
                rec["loaded_before_stock"] = False
            rec.update(details)
            if event and x["live"] and up_before:
                rec["up_before"] = up_before
            elif not rec["live"]:
                rec.pop("up_before", None)
            # an announced date on a product that isn't buyable yet -> drop calendar, linked to the product
            if not x["live"]:
                self._product_date(x, store, label)
            if event and not use_cards:
                title = f"{event}: {x['name']}{warn}"
                self._product_alert("urgent" if x["live"] else "normal", title, x, label, extra,
                                    copy_to=["drawings"] if x["status"].startswith("Drawing") else None,
                                    store=store, ping=big)
            elif event:
                self._record_alert({"level": "urgent" if x["live"] else "normal", "title": f"{event}: {x['name']}{warn}",
                                    "url": x["url"], "fields": {k: v for k, v in {"Price": x["price"], **extra}.items() if v},
                                    "desc": "", "image": x.get("image", ""), "links": self._buy_links(x),
                                    "channel": store, "at": datetime.now(timezone.utc).isoformat()})
                if x["status"].startswith("Drawing"):         # the store's card bumps AND #drawings gets it
                    self._product_alert("urgent", f"{event}: {x['name']}{warn}", x, label, extra,
                                        channel="drawings", store=store, ping=big, strict=True, record=False)
            changed.append((x, rec, event, big and bool(event)))
        if store == "target":
            self._refresh_target_stock(html, [c for c in changed], st)
        elif t.get("stock_pages", 2):
            self._refresh_page_stock(t, st, changed)
        if use_cards:
            for x, rec, event, ping in changed:
                self.cards.request(store, x["id"], rec, bump=bool(event), ping=ping, headline=event or "")
                rec["carded"] = True
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
    def _refresh_target_stock(self, html, changed, st, per_run=14, every=600):
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
        todo = sorted(changed, key=lambda c: (not c[2], c[1].get("stock_at", 0)))
        n = 0
        for x, rec, event, _ in todo:
            if n >= per_run or (not event and now - rec.get("stock_at", 0) < every):
                continue
            n += 1
            info = target_stock(session, x["id"], zip_code, self.state.get("target_key", ""), miles=miles)
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
        if not got:
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
                         channel=["instore", store], store=store, product=True, ping=is_etb_or_upc(x["name"]))
        self._restock_board_dirty = True

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
        return [("🛒 Open & add to cart", x["url"])]

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
        cards = [x for x in tiles if self._wanted_card(x["name"] + " " + x["url"])]
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
                self._add_event(f"Walmart drawing: {x['name']}", t["url"], start, True, "drawing", "walmart")
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
        links = [("🎟️ ENTER THE DRAWING", rec["url"]), ("All Walmart drawings", rec.get("page", ""))]
        self.notify.send("urgent", title, rec["url"], fields, image=rec.get("image", ""), links=links, channel="walmart",
                         copy_to=["drawings"], store="walmart", product=True, ping=is_etb_or_upc(rec["name"]))

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

        # calendar changes don't post one by one: the whole #topps-calendar board is re-posted fresh at the
        # bottom with @everyone and a one-line list of what changed. Drop alerts (live / 15 min / open) go to #topps.
        changes = []
        for p in products:
            d = datetime.fromisoformat(p["when"]) if p["when"] else None
            fields = {"When (CT)": self._when_text(p), "Store": "Topps", "Sport": p["sport"], "Status": p["status"]}
            if d and d > now - timedelta(days=1):
                self._add_event(f"Topps: {p['name']}", p["url"], d, p["has_time"], "topps", "topps")
            old = known.get(p["slug"])
            if first:
                continue
            if old is None:
                changes.append(f"🆕 added **{p['name']}** · {self._when_text(p)}")
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
        if changes:
            self._post_topps_calendar(changes, t)

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
            if not rec.get("checked"):
                return True
            fresh = now - rec.get("first_seen", now) < 3 * 86400
            near = False
            if rec.get("when"):
                dt = datetime.fromisoformat(rec["when"]).timestamp()
                near = dt - 7200 <= now <= dt + 6 * 3600
            wait = hot_every if (near or (fresh and not rec.get("live"))) else idle_every
            return now - rec["checked"] >= wait
        order = sorted((h for h, r in known.items() if due(r)),
                       key=lambda h: (known[h].get("baseline", False), bool(known[h].get("checked")),
                                      known[h].get("checked", 0)))
        opened = 0
        use_cards = self.cards.enabled("topps")
        for h in order[: t.get("max_pages_per_run", 4)]:
            rec = known[h]
            ps, pf, ph = self._fetch({"name": t["name"], "url": rec["url"], "browser": t.get("browser", False)}, st)
            rec["checked"] = now
            if looks_blocked(ps, ph) or ps >= 400:
                continue
            opened += 1
            item = parse_topps_item_page(ph, rec["url"])
            prev_status, was_live, new = rec.get("status"), rec.get("live"), not rec.get("status")
            rec.update({k: v for k, v in item.items() if v not in ("", None) or k in ("live",)})
            rec["retail"], rec["ratio"] = "Topps' own price", None
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
                self.cards.request("topps", h, rec, bump=bool(event), ping=bool(event), headline=event)
                rec["carded"] = True
            elif event:
                self.notify.send("urgent", f"{event}: {rec['name']}", rec["url"],
                                 {"Price": rec.get("price"), "Status": rec["status"], "Limit": rec.get("limit")},
                                 image=rec.get("image", ""), links=self._buy_links(rec), store="topps", product=True,
                                 ping=True, channel="topps",
                                 copy_to=["drawings"] if rec["status"] == "Drawing open" else None)
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
    KIND_ICON = {"topps": "🃏", "drawing": "🎟️", "release": "⚡", "news": "⚡"}

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
        t = re.sub(r"^(?:topps|walmart drawing|target|walmart|best buy|amazon|pokémon center)\s*:\s*", "", t, flags=re.I)
        t = re.sub(r"^20\d\d(?:-\d\d)?\s+", "", t)
        t = re.sub(r"^topps\s+(?=\S)", "", t, flags=re.I)
        return " ".join(t.split())

    def _icon(self, e):
        if e.get("kind") == "drawing":
            return "🎟️"
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
        legend = "⚾🏀🏈🃏 Topps · 🎟️ drawing · ⚡ store product · times Central · *time TBA* = day announced, time not yet"
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

    def calendar_tick(self):
        """Every loop: post newly found dates to #drop-calendar, remind 15 min before news-announced drops,
        send the 8 AM rundown, and keep the two self-updating boards current. Posts only to channels that
        have their own webhook."""
        if not self.first_pass_done:
            return
        events = self.state.get("events", {})
        now = datetime.now(CT)
        if self.state.get("cal_announced") is None:          # first run: everything already known is baseline
            self.state["cal_announced"] = list(events)
        ann = set(self.state["cal_announced"])
        added = [ev for uid, ev in events.items()
                 if uid not in ann and datetime.fromisoformat(ev["start"]) > now - timedelta(hours=1)]
        ann |= set(events)
        self.state["cal_announced"] = list(ann)[-3000:]
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
        for board, render in self._all_boards():
            try:
                board.tick(render, force=repost, repost=repost)
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
        if store == "walmart":
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
        return self._section(f"🎟️ Drawings & raffles right now · {len(lines)}", lines)

    def _render_drawings(self):
        return [self._drawings_section()]

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
        if kind not in ("topps", "drawing") and not product:
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
