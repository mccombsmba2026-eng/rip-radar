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
from .notify import ChatBot, Notifier, StatusBoard
from .parsing import (CT, LIVE_STATUSES, categorize, extract_when, fmt_when, gcal_link, looks_blocked,
                      parse_topps_calendar)

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
                        "alert_title": f"{pre.get('alert', '🚨 PAGE LIVE')}: {p['name']}", "user": True})
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
            for t in self.targets():
                if self._stop.is_set():
                    return
                if time.time() < self.next_due.get(t["name"], 0):
                    continue
                self._run_target(t, report)
                iv = t.get("interval_seconds", default_iv)
                self.next_due[t["name"]] = time.time() + iv * random.uniform(0.85, 1.15)
                self._save_state()
                time.sleep(random.uniform(1, 2.5))
            if report and not self.first_pass_done:
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
        try:
            health, code = watcher(t, st, first) if watcher else (f"error: unknown type {t['type']}", 0)
        except Exception as e:
            health, code = f"error: {type(e).__name__}: {e}"[:160], 0
            log.exception("%s failed", t["name"])
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
        """Alert on NEW product links appearing on a page (product loaded)."""
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        soup = BeautifulSoup(html, "html.parser")
        pat = re.compile(t.get("link_pattern", r"/products?/"), re.I)
        want = [k.lower() for k in t.get("include", [])]
        found = {}
        for a in soup.find_all("a", href=True):
            href = urljoin(final, a["href"].split("?")[0].split("#")[0])
            if not pat.search(href):
                continue
            text = " ".join(a.get_text(" ").split()) or a.get("aria-label", "") or href.rsplit("/", 1)[-1]
            if want and not any(k in (text + " " + href).lower() for k in want):
                continue
            if href not in found or len(text) > len(found[href]):
                found[href] = text
        if not found:
            return "empty", status
        seen = set(st.get("links", []))
        if not first:
            for h in [h for h in found if h not in seen][:10]:
                name = found[h] or h
                fields = {"Source": t["name"], "Category": t.get("category") or categorize(name + " " + h)}
                self._when_fields(name[:120], h, extract_when(name), fields)
                self.notify.send(t.get("level", "urgent"), f"New product on {t['name']}: {name[:120]}", h, fields)
        st["links"] = list(seen | set(found))[-3000:]
        return f"ok ({len(found)} products)", status

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
            self.notify.send(level, title[:240], link, fields, desc=summary[:300])
            sent += 1
            if sent >= t.get("max_per_run", 8):
                break
        st["seen"] = list(seen)[-2000:]
        return f"ok ({len(feed.entries)} posts)", status

    def check_topps_calendar(self, t, st, first):
        status, final, html = self._fetch(t, st)
        if looks_blocked(status, html):
            return "blocked", status
        if status >= 400:
            return "error", status
        wanted = set(t.get("sports", ["Baseball", "Basketball", "Football"]))
        allp = parse_topps_calendar(html, final)
        products = [p for p in allp if p["sport"] in wanted]
        if not products:
            return ("empty" if not allp else f"ok (0 of {len(allp)} match your sports)"), status
        with self._lock:
            self.topps = products
        self.state["topps_products"] = products
        self._write_topps_csv(products)
        known, now = st.get("products", {}), datetime.now(CT)

        if first:
            lines = [f"`{p['sport'][:4]}` **{p['name']}** · {fmt_when(datetime.fromisoformat(p['when']), p['has_time'])}"
                     f" · {p['status']}" for p in products]
            self.notify.send("normal", f"Topps calendar: {len(products)} products", t["url"], desc="\n".join(lines))

        for p in products:
            d = datetime.fromisoformat(p["when"])
            fields = {"Sport": p["sport"], "Drops (CT)": fmt_when(d, p["has_time"]), "Status": p["status"],
                      "Calendar": f"[Add to Google Calendar]({gcal_link(p['name'], d, p['url'], not p['has_time'])})"}
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
            if p["when"][:16] != old.get("when", "")[:16]:
                fields["Was"] = fmt_when(datetime.fromisoformat(old["when"]), old.get("has_time", False))
                self.notify.send("normal", f"📅 Topps date moved: {p['name']}", p["url"], fields)

        lead = int(t.get("remind_minutes_before", 15))
        reminded = set(st.get("reminded", []))
        for p in products:
            d = datetime.fromisoformat(p["when"])
            key = f"{p['slug']}|{p['when'][:16]}"
            if p["has_time"] and key not in reminded and now <= d <= now + timedelta(minutes=lead):
                mins = max(1, int((d - now).total_seconds() // 60))
                self.notify.send("urgent", f"⏰ Topps drop in {mins} min: {p['name']}", p["url"],
                                 {"Sport": p["sport"], "Drops (CT)": fmt_when(d), "Status": p["status"]})
                reminded.add(key)
        st["reminded"] = list(reminded)[-200:]
        st["products"] = {p["slug"]: {"status": p["status"], "when": p["when"], "has_time": p["has_time"]}
                          for p in products}
        return f"ok ({len(products)} products)", status

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
                    w.writerow([p["sport"], p["name"], fmt_when(datetime.fromisoformat(p["when"]), p["has_time"]),
                                p["status"], p["section"], p["url"]])
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
