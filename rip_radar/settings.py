"""User settings (settings.json in %APPDATA%\\RipRadar). Secrets live only here, never in the repo."""
import copy
import json
import threading

from . import paths

DEFAULTS = {
    "discord_webhook": "",
    "discord_webhook_urgent": "",
    # one channel per store; empty = that store's alerts go to discord_webhook
    "webhooks": {"topps": "", "topps_calendar": "", "pokemon": "", "pokemon_queue": "", "walmart": "", "target": "",
                 "dicks": "", "amazon": "", "bestbuy": "", "costco": "", "samsclub": "", "pharmacy": "", "ace": "",
                 "barnes": "", "gamestop": "", "hottopic": "", "watch": "", "presale": "", "prices": "", "instore": "", "drawings": "", "calendar": "", "status": ""},
    "ntfy_topic": "",
    "twilio": {"account_sid": "", "auth_token": "", "from": "", "to": ""},
    "sms_for": ["queue"],            # texts: only the Pokémon Center queue going live ("urgent" = every urgent alert)
    "ntfy_for": ["queue"],           # ntfy phone push: only the queue (loud, max priority); "urgent" / "all" = more
    "bot_token": "",
    # "still running?" bot: what it answers to, what it says, and where it listens (blank = every channel it can see)
    "bot_triggers": ["still running", "running", "status", "you up", "you on", "are you on", "alive"],
    "bot_reply": "🟢 **Yes, Rip Radar is on.**\nUp {uptime} · v{version}\nLast scan: {last_scan}\n"
                 "Sources OK: {sources_ok} · Alerts today: {alerts_today}\n{problems}",
    "bot_channel": "",
    "status_every_minutes": 5,
    "sports": ["Baseball", "Basketball", "Football"],
    "zip": "77007",                  # Target stock counts + in-store restock tracker for stores near this ZIP
    "restock_miles": 30,
    "drop_mode_until": 0,            # Pokémon Center drop mode: queue every 30 s until this time
    "open_browser_on_queue": True,   # queue goes live -> open pokemoncenter.com in Chrome on this PC
    "focus_sets": ["Ascended Heroes", "151", "Prismatic Evolutions", "Crown Zenith", "Destined Rivals",
                   "Paldea Evolved", "Evolving Skies", "Team Up"],
    "everyone_products": ["2025-26 Topps Flagship Basketball", "2025-26 Topps Basketball",
                          "2025-26 Topps Chrome Basketball", "2025-26 Topps Chrome Update Basketball",
                          "2025-26 NBA Hoops", "2025-26 Topps Hoops Basketball", "2025-26 Topps Finest Basketball",
                          "2025-26 Topps Signature Class Basketball", "Cooper Flagg"],
    "keep_awake": False,
    "resale_check": True,
    "watch_terms": [],
    "price_list_hour": 8,        # daily eBay price list refresh (CT hour); -1 = off
    "everyone_only_in_watch": True,   # @everyone only in #watch (store channels post quietly); queue alerts excepted
    "watch_tcins": [],           # Target product IDs watched from #watch (`watch tcin 94681234`)
    "blocked_products": [],      # ❌ in Discord / `block <words>` in #watch: never posted
    "quiet_hours": [2, 6],           # store pages half as often 2-6 AM CT (queue + drawings stay full speed)
    "site_budgets": {},              # override pages-per-hour per site, e.g. {"walmart.com": 120}               # #watch channel: product-name watches (typed in Discord)            # eBay sold median on product posts
    "queue_window": {"days": [0, 1, 2, 3, 4], "start": 9, "end": 15},   # PC usual drop window (CT): PC product scans rest, queue only; "days": [] = off
    "heartbeat_url": "",             # dead-man's switch: healthchecks.io-style ping URL, pinged every 5 min
    "health_hour": 9,                # daily 🩺 health check in #app-status at this hour (CT); -1 = off
    "recap_hour": 21,                # daily recap in #app-status at this hour (CT); -1 = off             # OFF unless M turns it on: ask Windows not to sleep while Rip Radar runs
    "topps_all_products": True,      # Topps: every product line (Disney, F1, soccer...) - not just your sports
    "start_with_windows": True,
    "keep_running_when_closed": False,   # False: the X quits. True: closing hides to the tray and keeps scanning
    "auto_update": True,             # install updates by itself while the app is in the tray
    "paused": False,
    "disabled_sources": [],
    # pages you add in the app: {"name","url","preset","keywords","enabled"}
    "watch_pages": [],
}

_lock = threading.Lock()


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        try:
            data = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        merged = _merge(DEFAULTS, data)
        old = merged.pop("discord_webhook_pokemon", "")
        if old and not merged["webhooks"].get("pokemon"):
            merged["webhooks"]["pokemon"] = old
        if data.get("zip") == "77002":          # the first default; M's area is 77007
            merged["zip"] = "77007"
        return merged


def save(settings):
    with _lock:
        paths.ensure_dirs()
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_FILE)


def update(changes):
    s = _merge(load(), changes)
    save(s)
    return s
