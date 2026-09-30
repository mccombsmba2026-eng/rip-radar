# Rip Radar – notes for working on this repo

Windows desktop app that alerts M (Houston, collects Topps + Pokémon) to product drops, retailer
raffles/drawings, Pokémon Center queues and Topps release-calendar changes. Alerts go to Discord
(optional Twilio SMS, ntfy, and a "still running?" Discord bot).

**Alert-only, by design.** The app never enters queues, raffles or checkouts, never solves
captchas, and never automates purchases. Keep it that way.

## Shipping an update (the whole loop)
1. Change code, run `pytest -q` (all tests must pass).
2. Bump `__version__` in `rip_radar/__init__.py` (semver; the app compares numbers).
3. Commit with a one-line, user-readable message (it becomes the release note shown in the app's
   update banner) and push to `main`.
4. GitHub Actions (`.github/workflows/build.yml`) runs tests, builds `RipRadar.exe` with PyInstaller
   on Windows, runs `RipRadar.exe --selftest` (checks packaging + hits every non-browser source
   live), and publishes release `v<version>` with the exe attached.
5. Installed apps check `releases/latest` at launch and every 6 h, show "Restart to update",
   download the exe, and swap it in via a tiny cmd script after exiting.
6. Verify: `curl -s https://api.github.com/repos/mccombsmba2026-eng/rip-radar/releases/latest` –
   the release body contains the selftest report (live source health + current Topps list).
   Pushing without a version bump only runs tests.

## Layout
- `run.py` – entry point. `rip_radar/app.py` – pywebview window, tray (pystray), single-instance
  (localhost:47831), self-install to `%LOCALAPPDATA%\Programs\RipRadar`, shortcuts, autostart.
- `rip_radar/engine.py` – scanner thread + watchers: `topps_calendar`, `topps_products` (per-format: Hobby,
  Mega, Blaster... from each product's /pages/ page), `listing` (`tcg_only` = Pokémon card products only),
  `keywords`, `feed`.
  Sources marked `browser: true` load in a hidden pywebview window (real Edge/WebView2) so bot-walled
  sites (Pokémon Center, Walmart) see a normal browser; falls back to plain HTTP.
- `rip_radar/parsing.py` – pure parsing (dates → Central time, Topps cards, sports). Unit-tested.
- `rip_radar/targets.yaml` – built-in sources + "Watch a page" presets. Edit here to add sources.
- `rip_radar/notify.py` – Discord/Twilio/ntfy, self-editing status message, chat bot. One Discord channel per
  store (`CHANNELS`: topps, pokemon, walmart, target, dicks, amazon, bestbuy) via `settings["webhooks"]`;
  sources set `channel:`, news feeds `route_by_store: true` (store named in the headline); empty = main webhook.
- `rip_radar/ui/index.html` – the whole UI (vanilla JS; talks to `Api` in app.py via `pywebview.api`).
- `rip_radar/updater.py` – GitHub Releases check + swap.
- User data lives in `%APPDATA%\RipRadar` (settings.json holds the webhook and any tokens).

## Rules
- Never commit secrets (webhooks, bot tokens, Twilio keys). The repo is public. They belong in the
  app's Settings screen only.
- Keep scan intervals ≥ 60 s per source.
- pywebview 6.x: `closing` handlers returning False cancel the close (used for hide-to-tray);
  `load_url`/`evaluate_js` block until the window is shown/ready (hidden windows still fire `shown`).
- topps.com returns 403 to plain HTTP from data-center IPs (the build machine's selftest always shows
  Topps as blocked). The engine's `_fetch` auto-switches any blocked non-feed source to the hidden
  browser window (sticky per source via `auto_browser` in state), so the installed app still reads it.
- The Topps calendar page is server-rendered HTML: cards are links to `/pages/<slug>` with text like
  "Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football" and a button (Notify me / Pre-order).
