"""Sends alerts: Discord webhook (+ @everyone for urgent), optional Twilio SMS and ntfy push.
Also keeps a self-updating status message and an optional 'still running?' chat bot."""
import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("rip_radar")
COLORS = {"urgent": 0xE0342B, "normal": 0x2350C8, "system": 0x8A8F9E}
STORE_COLORS = {"target": 0xCC0000, "walmart": 0x0071CE, "bestbuy": 0x0046BE, "amazon": 0xFF9900,
                "dicks": 0x006B54, "pokemon": 0xFFCB05, "topps": 0xE31837, "costco": 0xE31837, "samsclub": 0x0067A0,
                "cvs": 0xCC0000, "walgreens": 0xE31837, "ace": 0xD40029, "barnes": 0x2A5934,
                "gamestop": 0xE4002B}


# One Discord channel per store. Key -> label shown in the app. Webhooks live in settings["webhooks"].
CHANNELS = {"topps": "Topps products (formats, in stock)", "topps_calendar": "Topps calendar",
            "pokemon": "Pokémon Center products", "pokemon_queue": "Pokémon Center queue",
            "walmart": "Walmart", "target": "Target", "dicks": "Dick's", "amazon": "Amazon", "bestbuy": "Best Buy",
            "costco": "Costco", "samsclub": "Sam's Club", "pharmacy": "CVS & Walgreens", "ace": "Ace Hardware",
            "barnes": "Barnes & Noble", "gamestop": "GameStop",
            "instore": "In-store restocks (stores near you)",
            "drawings": "Drawings & raffles (all stores)", "calendar": "Drop calendar (all announced dates)",
            "status": "App status"}
STORE_NAMES = {"topps": "Topps", "pokemon": "Pokémon Center", "walmart": "Walmart", "target": "Target",
               "dicks": "Dick's", "amazon": "Amazon", "bestbuy": "Best Buy", "costco": "Costco",
               "samsclub": "Sam's Club", "cvs": "CVS", "walgreens": "Walgreens", "ace": "Ace Hardware",
               "barnes": "Barnes & Noble", "gamestop": "GameStop"}
# stores that share a Discord channel (store key -> channel key); everything else posts to its own key
STORE_CHANNEL = {"cvs": "pharmacy", "walgreens": "pharmacy"}


def channel_of(store):
    return STORE_CHANNEL.get(store, store)
# channels that only ever post to their own webhook (never spill into the main channel)
STRICT_CHANNELS = {"calendar"}
# news posts go to the store they mention first (checked in this order)
STORE_WORDS = [("pokemon", ("pokémon center", "pokemon center", "pokemoncenter")),
               ("walmart", ("walmart",)), ("target", ("target",)),
               ("dicks", ("dick's", "dicks sporting", "dick’s", "dickssportinggoods", "dick's sporting goods")),
               ("amazon", ("amazon",)), ("bestbuy", ("best buy", "bestbuy")),
               ("samsclub", ("sam's club", "sams club", "samsclub")), ("costco", ("costco",)),
               ("cvs", ("cvs",)), ("walgreens", ("walgreens",)), ("ace", ("ace hardware", "acehardware")),
               ("barnes", ("barnes & noble", "barnes and noble", "barnesandnoble")), ("gamestop", ("gamestop", "game stop")),
               ("topps", ("topps", "bowman"))]


def store_in(text):
    """Which store a headline or link is about ('Target' only as a word, not 'targeting')."""
    import re
    t = (text or "").lower()
    for key, words in STORE_WORDS:
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in words):
            return key
    return None


class Notifier:
    def __init__(self, get_settings, on_alert=None):
        self.get_settings = get_settings   # callable -> current settings dict (picks up edits live)
        self.on_alert = on_alert or (lambda a: None)
        self._local = threading.local()    # channel is per-thread: the scanner sets it, UI tests don't see it

    def set_channel(self, channel):
        self._local.channel = channel

    def send(self, level, title, url="", fields=None, desc="", channel=None, image="", links=None, strict=False,
             record=True, copy_to=None, store=None, product=False, ping=False):
        """links: [(label, url)] shown as a row of clickable links (Add to cart, Buy now...). image: thumbnail.
        strict: only post if one of the wanted channels has its own webhook (never fall back to main).
        record: also show it in the app's alert list.
        copy_to: extra channels that ALSO get this alert if they have a webhook (e.g. raffles -> #drawings too).
        ping: @everyone. Only for the Pokémon Center queue, Topps drops/changes, and ETB/UPC drops."""
        fields = {k: v for k, v in (fields or {}).items() if v}
        links = [(lbl, u) for lbl, u in (links or []) if u]
        channel = channel or getattr(self._local, "channel", None)
        if isinstance(channel, str):
            channel = channel_of(channel)
        log.info("ALERT [%s] %s %s", level, title, url)
        if record:
            self.on_alert({"level": level, "title": title, "url": url, "fields": fields, "desc": desc,
                           "image": image, "links": links,
                           "channel": (channel[0] if isinstance(channel, (list, tuple)) else channel) or "",
                           "at": datetime.now(timezone.utc).isoformat()})
        s = self.get_settings()
        # buy links first and big, right under the product name
        link_row = "   ".join(f"**[{lbl.upper() if 'cart' in lbl.lower() or 'buy' in lbl.lower() else lbl}]({u})**"
                               for lbl, u in links)
        body = "\n\n".join(x for x in (link_row, desc or "") if x)
        color = STORE_COLORS.get(store) if (store and level != "system") else None
        embed = {"title": title[:250], "color": color or COLORS.get(level, COLORS["normal"]),
                 "description": body[:4000],
                 "fields": [{"name": k, "value": str(v)[:1000], "inline": k not in ("Calendar", "Typical retail")}
                            for k, v in fields.items()],
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        if store and store in STORE_NAMES:
            embed["footer"] = {"text": STORE_NAMES[store]}
        if url.startswith("http"):
            embed["url"] = url
        if image.startswith("http"):
            embed["image" if product else "thumbnail"] = {"url": image}
        payload = {"embeds": [embed]}   # the webhook's own name + avatar
        if ping:
            payload["content"] = "@everyone"
        hooks_by_channel = s.get("webhooks") or {}
        # channel can be a preference list, e.g. ("drawings", "walmart"): the first one with a webhook wins
        wanted = list(channel) if isinstance(channel, (list, tuple)) else ([channel] if channel else [])
        if level == "system" and not wanted:
            wanted = ["status"]             # app messages (source checks, problems, updates) -> status channel
        own = next((hooks_by_channel.get(c, "") for c in wanted if hooks_by_channel.get(c, "").startswith("http")), "")
        if own:
            hooks = {own}                   # a channel with its own webhook gets only its own alerts
        elif strict or (wanted and all(c in STRICT_CHANNELS for c in wanted if c)):
            return False                    # that channel isn't set up: stay out of the main channel
        else:
            hooks = {s.get("discord_webhook", "")}
            if level == "urgent":
                hooks.add(s.get("discord_webhook_urgent", ""))
        for extra in copy_to or []:            # e.g. a Walmart drawing: #walmart AND #drawings
            h = hooks_by_channel.get(extra, "")
            if h.startswith("http"):
                hooks.add(h)
        results = [self._post_discord(h, payload) for h in hooks if h.startswith("http")]
        if s.get("ntfy_topic") and level != "system":
            try:
                requests.post(f"https://ntfy.sh/{s['ntfy_topic']}", data=title.encode("utf-8"),
                              headers={"Click": url, "Priority": "urgent" if level == "urgent" else "default"},
                              timeout=15)
            except requests.RequestException as e:
                log.warning("ntfy failed: %s", e)
        tw = s.get("twilio") or {}
        if all(tw.get(k) for k in ("account_sid", "auth_token", "from", "to")) and level in s.get("sms_for", []):
            try:
                r = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{tw['account_sid']}/Messages.json",
                    auth=(tw["account_sid"], tw["auth_token"]),
                    data={"From": tw["from"], "To": tw["to"], "Body": f"{title}\n{url}"[:600]}, timeout=15)
                if r.status_code >= 400:
                    log.warning("Twilio error %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.warning("Twilio failed: %s", e)
        return all(results) if results else False

    @staticmethod
    def _post_discord(hook, payload):
        for _ in range(3):
            try:
                r = requests.post(hook, json=payload, timeout=15)
                if r.status_code == 429:
                    time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                    continue
                if r.status_code >= 400:
                    log.warning("Discord error %s: %s", r.status_code, r.text[:200])
                    return False
                return True
            except requests.RequestException as e:
                log.warning("Discord failed: %s", e)
                time.sleep(2)
        return False


class LiveBoard:
    """A single Discord message in one channel that rewrites itself (a pinned, always-current list).
    Posts only when that channel has its own webhook. render() returns (title, text) or a list of them
    (one Discord embed each; a message holds up to ~6000 characters in total)."""

    def __init__(self, get_settings, channel, state, key, every_minutes=10, color=None):
        self.get_settings, self.channel, self.state, self.key = get_settings, channel, state, key
        self.every = every_minutes * 60
        self.color = color
        self.next = 0
        self.last_body = None

    def hook(self):
        return ((self.get_settings().get("webhooks") or {}).get(self.channel) or "")

    def _body(self, render):
        parts = render()
        if isinstance(parts, tuple):
            parts = [parts]
        embeds, budget = [], 5800
        for title, desc in parts[:9]:
            desc = (desc or "Nothing right now.")[:min(4000, max(200, budget - len(title)))]
            budget -= len(title) + len(desc)
            embeds.append({"title": title[:250], "description": desc, "color": self.color or COLORS["normal"]})
            if budget <= 200:
                break
        embeds[-1]["timestamp"] = datetime.now(timezone.utc).isoformat()
        embeds[-1]["footer"] = {"text": "Updates itself every few minutes · pin this message"}
        return {"embeds": embeds}

    def tick(self, render, force=False, repost=False, content=None):
        """force: update now. repost: delete the old message and post a fresh one at the bottom of the channel.
        content: text above the board on a fresh post (e.g. '@everyone · what changed')."""
        hook = self.hook()
        if not hook.startswith("http") or (time.time() < self.next and not force and not repost):
            return False
        self.next = time.time() + self.every
        body = self._body(render)
        sig = json.dumps([(e["title"], e["description"]) for e in body["embeds"]])
        if sig == self.last_body and not force and not repost:
            return False
        saved = self.state.get(self.key) or {}
        try:
            if saved.get("hook") == hook and saved.get("id"):
                if repost:
                    requests.delete(f"{hook}/messages/{saved['id']}", timeout=15)
                elif requests.patch(f"{hook}/messages/{saved['id']}", json=body, timeout=15).status_code < 400:
                    self.last_body = sig
                    return True
            if content:
                body["content"] = content[:1900]
            r = requests.post(hook + "?wait=true", json=body, timeout=15)
            if r.status_code < 400:
                self.state[self.key] = {"hook": hook, "id": r.json()["id"]}
                self.last_body = sig
                return True
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("%s board failed: %s", self.channel, e)
        return False


class StatusBoard:
    """One Discord message that edits itself every few minutes. A stale time = the app is down."""

    def __init__(self, get_settings, status_text, state):
        self.get_settings, self.status_text, self.state = get_settings, status_text, state
        self.next = 0

    def tick(self):
        s = self.get_settings()
        hook = (s.get("webhooks") or {}).get("status", "") or s.get("discord_webhook", "")
        if not hook.startswith("http") or time.time() < self.next:
            return
        self.next = time.time() + int(s.get("status_every_minutes", 5)) * 60
        body = {"content": self.status_text(heading="**Rip Radar is running.**")
                + "\n-# Updates every few minutes. If this time is old, the app is closed or the PC is asleep."}
        saved = self.state.get("status_msg") or {}
        try:
            if saved.get("hook") == hook and saved.get("id"):
                if requests.patch(f"{hook}/messages/{saved['id']}", json=body, timeout=15).status_code < 400:
                    return
            r = requests.post(hook + "?wait=true", json=body, timeout=15)
            if r.status_code < 400:
                self.state["status_msg"] = {"hook": hook, "id": r.json()["id"]}
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("status message failed: %s", e)


def bot_matches(text, triggers):
    """'Still running??' / 'you on' / 'rip radar status' -> True for a short message containing a trigger phrase."""
    import re
    t = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())
    t = " ".join(t.split())
    if not t or len(t) > 60:
        return False
    for trig in triggers or []:
        g = " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(trig).lower()).split())
        if g and (t == g or re.search(r"(^| )" + re.escape(g) + r"( |$)", t)):
            return True
    return False


def bot_channel_ok(channel_name, channel_id, wanted):
    """wanted: '' (any channel), a channel name ('app-status' / '#app-status') or a channel ID."""
    w = str(wanted or "").strip().lstrip("#").lower()
    return not w or w == str(channel_name or "").lower() or w == str(channel_id or "")


class ChatBot:
    """Answers 'still running?' (or the phrases set in Settings) in Discord with the reply set in Settings.
    Needs a bot token. Wording changes apply right away - it reads settings on every message."""
    TRIGGERS = {"still running", "running", "status", "you up", "alive"}

    def __init__(self, status_text, get_settings=None, reply_text=None):
        self.status_text = status_text
        self.get_settings = get_settings or (lambda: {})
        self.reply_text = reply_text or (lambda: status_text())
        self.token = None
        self.state = "off"   # off | connecting | online | error: ...
        self._thread = None

    def start(self, token):
        if not token or token == self.token and self._thread and self._thread.is_alive():
            return
        self.token = token
        self._thread = threading.Thread(target=self._run, args=(token,), daemon=True, name="discord-bot")
        self._thread.start()

    def _run(self, token):
        import asyncio
        try:
            import discord
        except ImportError:
            self.state = "error: discord.py missing"
            return
        asyncio.set_event_loop(asyncio.new_event_loop())
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        bot = self

        @client.event
        async def on_ready():
            bot.state = "online · waiting for a message (if it never hears one, it can't see that channel)"
            log.info("Discord bot online as %s", client.user)

        @client.event
        async def on_message(msg):
            if msg.author.bot:
                return
            s = bot.get_settings()
            where = "#" + str(getattr(msg.channel, "name", "") or "DM")
            if not msg.content:
                # Discord sent the message without its text: Message Content Intent is off for this bot
                bot.state = (f"online · heard a message in {where} but can't read it: turn on Message Content Intent "
                             "(discord.com/developers → your app → Bot) and save")
                return
            if not bot_channel_ok(getattr(msg.channel, "name", ""), getattr(msg.channel, "id", ""), s.get("bot_channel")):
                bot.state = f"online · heard {where}, but it only answers in #{str(s.get('bot_channel')).lstrip('#')}"
                return
            if not bot_matches(msg.content, s.get("bot_triggers") or list(bot.TRIGGERS)):
                return
            text = bot.reply_text()[:1900]
            try:
                await msg.reply(text, mention_author=False)
                bot.state = f"online · last answered in {where}"
            except Exception as e:                       # no Read Message History: a plain message still works
                try:
                    await msg.channel.send(text)
                    bot.state = f"online · last answered in {where}"
                except Exception as e2:
                    bot.state = (f"online · heard {where} but isn't allowed to post there - give the bot View Channel, "
                                 f"Send Messages and Read Message History in that channel ({type(e2).__name__})")
                    log.warning("bot can't reply in %s: %s / %s", where, e, e2)

        self.state = "connecting"
        try:
            client.run(token, log_handler=None)
        except Exception as e:  # bad token, intent not enabled, network
            self.state = f"error: {e}"[:160]
            log.warning("Discord bot stopped: %s", e)
