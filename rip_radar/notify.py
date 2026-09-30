"""Sends alerts: Discord webhook (+ @everyone for urgent), optional Twilio SMS and ntfy push.
Also keeps a self-updating status message and an optional 'still running?' chat bot."""
import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("rip_radar")
COLORS = {"urgent": 0xE0342B, "normal": 0x2350C8, "system": 0x8A8F9E}


# One Discord channel per store. Key -> label shown in the app. Webhooks live in settings["webhooks"].
CHANNELS = {"topps": "Topps", "pokemon": "Pokémon Center", "walmart": "Walmart", "target": "Target",
            "dicks": "Dick's", "amazon": "Amazon", "bestbuy": "Best Buy",
            "drawings": "Drawings & raffles (all stores)", "status": "App status"}
# news posts go to the store they mention first (checked in this order)
STORE_WORDS = [("pokemon", ("pokémon center", "pokemon center", "pokemoncenter")),
               ("walmart", ("walmart",)), ("target", ("target",)),
               ("dicks", ("dick's", "dicks sporting", "dick’s", "dickssportinggoods", "dick's sporting goods")),
               ("amazon", ("amazon",)), ("bestbuy", ("best buy", "bestbuy")), ("topps", ("topps", "bowman"))]


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

    def send(self, level, title, url="", fields=None, desc="", channel=None, image="", links=None):
        """links: [(label, url)] shown as a row of clickable links (Add to cart, Buy now...). image: thumbnail."""
        fields = {k: v for k, v in (fields or {}).items() if v}
        links = [(lbl, u) for lbl, u in (links or []) if u]
        channel = channel or getattr(self._local, "channel", None)
        log.info("ALERT [%s] %s %s", level, title, url)
        self.on_alert({"level": level, "title": title, "url": url, "fields": fields, "desc": desc,
                       "image": image, "links": links,
                       "channel": (channel[0] if isinstance(channel, (list, tuple)) else channel) or "",
                       "at": datetime.now(timezone.utc).isoformat()})
        s = self.get_settings()
        link_row = "  ·  ".join(f"**[{lbl}]({u})**" for lbl, u in links)
        body = "\n\n".join(x for x in (link_row, desc or "") if x)
        embed = {"title": title[:250], "color": COLORS.get(level, COLORS["normal"]),
                 "description": body[:4000],
                 "fields": [{"name": k, "value": str(v)[:1000], "inline": k != "Calendar"}
                            for k, v in fields.items()],
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        if url.startswith("http"):
            embed["url"] = url
        if image.startswith("http"):
            embed["thumbnail"] = {"url": image}
        payload = {"username": "Rip Radar", "embeds": [embed]}
        if level == "urgent":
            payload["content"] = "@everyone"
        hooks_by_channel = s.get("webhooks") or {}
        # channel can be a preference list, e.g. ("drawings", "walmart"): the first one with a webhook wins
        wanted = list(channel) if isinstance(channel, (list, tuple)) else ([channel] if channel else [])
        if level == "system" and not wanted:
            wanted = ["status"]             # app messages (source checks, problems, updates) -> status channel
        own = next((hooks_by_channel.get(c, "") for c in wanted if hooks_by_channel.get(c, "").startswith("http")), "")
        if own:
            hooks = {own}                   # a channel with its own webhook gets only its own alerts
        else:
            hooks = {s.get("discord_webhook", "")}
            if level == "urgent":
                hooks.add(s.get("discord_webhook_urgent", ""))
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
            r = requests.post(hook + "?wait=true", json={**body, "username": "Rip Radar"}, timeout=15)
            if r.status_code < 400:
                self.state["status_msg"] = {"hook": hook, "id": r.json()["id"]}
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("status message failed: %s", e)


class ChatBot:
    """Answers 'still running?' / 'status' in Discord. Needs a bot token (Settings)."""
    TRIGGERS = {"still running", "running", "status", "you up", "alive"}

    def __init__(self, status_text):
        self.status_text = status_text
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
            bot.state = "online"
            log.info("Discord bot online as %s", client.user)

        @client.event
        async def on_message(msg):
            if msg.author.bot:
                return
            if msg.content.lower().strip(" ?!.") in bot.TRIGGERS:
                await msg.reply(bot.status_text(), mention_author=False)

        self.state = "connecting"
        try:
            client.run(token, log_handler=None)
        except Exception as e:  # bad token, intent not enabled, network
            self.state = f"error: {e}"[:160]
            log.warning("Discord bot stopped: %s", e)
