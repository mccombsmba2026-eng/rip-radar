"""One Discord message per product in each store channel. The message edits itself when price, stock or status
change; when a product comes in stock (or a drawing opens, or its link first loads) the old message is deleted and
a fresh one posted at the bottom of the channel, so it notifies like a new ping."""
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

import requests

from .notify import STORE_COLORS, STORE_NAMES

log = logging.getLogger("rip_radar")
GREY, AMBER, PURPLE = 0x8A8F9E, 0xF0A020, 0x9B59B6


def card_status(rec):
    """-> (icon + label, color key)"""
    st = str(rec.get("status", ""))
    if st.startswith("Drawing"):
        return "🎟️ DRAWING / INVITE OPEN", "drawing"
    if rec.get("live"):
        return "🟢 IN STOCK", "live"
    if rec.get("loaded_before_stock"):
        return "🆕 LOADED · NOT IN STOCK YET", "loaded"
    return "⚪ OUT OF STOCK", "oos"


def build_card(store, rec, headline=""):
    """The product message: title, links (ATC / Buy now / page), big picture, price vs retail, stock, limit."""
    label, kind = card_status(rec)
    name = rec.get("name", "")
    title = f"{headline or label} · {name}"[:250]
    links = []
    if rec.get("add_to_cart"):
        links.append(("🛒 ADD TO CART", rec["add_to_cart"]))
    if rec.get("buy_now"):
        links.append(("⚡ BUY NOW", rec["buy_now"]))
    links.append(("🔗 PRODUCT PAGE" if links else "🛒 OPEN & ADD TO CART", rec.get("url", "")))
    desc = "   ".join(f"**[{a}]({u})**" for a, u in links if u)
    if headline and headline != label:
        desc = f"Now: **{label}**\n\n" + desc
    fields = []

    def add(n, v, inline=True):
        if v:
            fields.append({"name": n, "value": str(v)[:1000], "inline": inline})

    price = rec.get("price") or "not shown"
    retail = rec.get("retail") or ""
    ratio = rec.get("ratio")
    if retail and ratio:
        pct = int(round((ratio - 1) * 100))
        verdict = "✅ at retail" if pct <= 5 else (f"⚠️ {pct}% above retail" if pct > 20 else f"{pct}% above retail")
        if pct < -5:
            verdict = f"✅ {-pct}% below retail"
        retail = f"{retail} · {verdict}"
    add("Price", price)
    add("Retail (MSRP)", retail or "unknown for this item")
    add("Stock", rec.get("stock") or ("in stock (count not shown by this store)" if rec.get("live") else
                                      rec.get("status", "")))
    add("Limit", rec.get("limit"))
    add("Stores", rec.get("stores"), inline=False)
    add("Link was up", rec.get("up_before"))
    color = {"live": STORE_COLORS.get(store), "drawing": PURPLE, "loaded": AMBER}.get(kind) or GREY
    embed = {"title": title, "url": rec.get("url", ""), "color": color, "description": desc[:4000],
             "fields": fields, "footer": {"text": f"{STORE_NAMES.get(store, store)} · updates itself"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    if str(rec.get("image", "")).startswith("http"):
        embed["image"] = {"url": rec["image"]}
    return {"embeds": [embed]}


def signature(body):
    e = dict(body["embeds"][0])
    e.pop("timestamp", None)
    return json.dumps(e, sort_keys=True)


class ProductCards:
    """Background poster so a channel's worth of cards never holds up scanning. Discord allows ~5 requests
    per 2 s per webhook; we go slower than that."""

    def __init__(self, get_settings, state, pace=0.8, lock=None):
        self.get_settings, self.state, self.pace = get_settings, state, pace
        self.state_lock = lock or threading.RLock()     # the engine saves state from its own thread
        self.pending = {}                 # key -> (store, rec, bump, ping, headline)
        self.q = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self.http = requests

    def hook(self, store):
        return (self.get_settings().get("webhooks") or {}).get(store) or ""

    def enabled(self, store):
        return self.hook(store).startswith("http")

    def saved(self):
        return self.state.setdefault("product_cards", {})

    def request(self, store, pid, rec, bump=False, ping=False, headline=""):
        """Queue an upsert. bump: move to the bottom as a fresh message (new / back in stock / drawing open)."""
        key = f"{store}:{pid}"
        with self._lock:
            old = self.pending.get(key)
            if old:
                bump, ping, headline = bump or old[2], ping or old[3], headline or old[4]
            else:
                self.q.put(key)
            self.pending[key] = (store, dict(rec), bump, ping, headline)
        self.start()

    def retire(self, store, pid):
        key = f"{store}:{pid}"
        with self._lock:
            if key not in self.pending:
                self.q.put(key)
            self.pending[key] = (store, None, False, False, "")
        self.start()

    def resend_all(self):
        """Sync: re-check every card we have (edits only the ones that changed)."""
        with self.state_lock:
            for v in self.saved().values():
                v["sig"] = ""

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="product-cards")
        self._thread.start()

    def _run(self):
        while True:
            key = self.q.get()
            with self._lock:
                job = self.pending.pop(key, None)
            if job:
                try:
                    self.process(key, *job)
                except Exception as e:
                    log.warning("product card %s: %s", key, e)

    def _call(self, method, url, **kw):
        for _ in range(4):
            r = self.http.request(method, url, timeout=15, **kw)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(wait + 0.3)
                continue
            time.sleep(self.pace)
            return r
        return r

    def process(self, key, store, rec, bump, ping, headline):
        saved = self.saved()
        old = saved.get(key) or {}
        hook = self.hook(store)
        if not hook.startswith("http"):
            return
        same_hook = old.get("hook") == hook and old.get("id")
        if rec is None:                                   # product gone from the store's listings
            if same_hook:
                self._call("DELETE", f"{hook}/messages/{old['id']}")
            with self.state_lock:
                saved.pop(key, None)
            return
        body = build_card(store, rec, headline if bump else "")
        sig = signature(build_card(store, rec))
        if same_hook and not bump:
            if old.get("sig") == sig:
                return
            r = self._call("PATCH", f"{hook}/messages/{old['id']}", json=body)
            if r.status_code < 400:
                with self.state_lock:
                    old["sig"] = sig
                return
            if r.status_code != 404:
                return
        if same_hook and bump:
            self._call("DELETE", f"{hook}/messages/{old['id']}")
        if ping:
            body["content"] = "@everyone"
        r = self._call("POST", hook + "?wait=true", json=body)
        if r.status_code < 400:
            with self.state_lock:
                saved[key] = {"hook": hook, "id": r.json()["id"], "sig": sig, "at": time.time()}
