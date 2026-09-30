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
5. Installed apps (1.0.4+) check `releases/latest` every 15 min, post "⬆️ Rip Radar X is ready" to the main
   Discord channel once, and - with auto_update on (default) - install it themselves in the first 2 minutes
   after launch or while in the tray; otherwise the blue "Restart to update" bar waits for a click.
   Since 1.0.5 the X quits the app unless Settings → "Keep scanning in the tray" (keep_running_when_closed). After restarting,
   the app posts "✅ Rip Radar updated to X" (via `just_updated.json` in the data folder).
6. Verify: `curl -s https://api.github.com/repos/mccombsmba2026-eng/rip-radar/releases/latest` –
   the release body contains the selftest report (live source health + current Topps list).
   Pushing without a version bump only runs tests.

## Layout
- `run.py` – entry point. `rip_radar/app.py` – pywebview window, tray (pystray), single-instance
  (localhost:47831), self-install to `%LOCALAPPDATA%\Programs\RipRadar`, shortcuts, autostart.
- `rip_radar/engine.py` – scanner thread + watchers: `topps_calendar`, `topps_products` (per-format: Hobby,
  Mega, Blaster... from each product's /pages/ page, with Shopify cart links), `retail_search` (store search
  pages: Target, Walmart, Dick's, Best Buy, Amazon, Pokémon Center; pings only when a Pokémon/sports-card
  product is in stock or a drawing/invite opens; first run is silent; `verify_pages` opens product pages when
  tiles hide stock; `msrp.py` ignores listings >60% over typical retail and flags >20%), `walmart_drawings`
  (walmart.com/shop/collectibles/draw: pings on first sight, 15 min before open, at open, 30 min before close),
  `listing`, `keywords`, `feed`. One source runs at a time, most overdue first; a site that blocks us is paused
  5→10→20→30 min (`host_backoff`), except drawings.
  Sources marked `browser: true` load in a hidden pywebview window (real Edge/WebView2) so bot-walled
  sites (Pokémon Center, Walmart) see a normal browser; falls back to plain HTTP only on a real browser error -
  "browser not ready" (right after launch) waits up to ~90 s, because plain HTTP gets client-rendered stores' empty
  shell ("empty [HTTP 200]"). `BrowserFetcher` scrolls the page until the link count stops growing (lazy tiles).
- `rip_radar/parsing.py` – pure parsing (dates → Central time, Topps cards, sports). Unit-tested.
- `rip_radar/targets.yaml` – built-in sources + "Watch a page" presets. Edit here to add sources.
- `rip_radar/notify.py` – Discord/Twilio/ntfy, self-editing status message, chat bot. One Discord channel per
  store (`CHANNELS`: topps, pokemon, walmart, target, dicks, amazon, bestbuy) plus `drawings` (every drawing /
  raffle / invite from any store) and `status` (system messages, status board, update notices) via
  `settings["webhooks"]`. `channel` may be a preference list, e.g. `[drawings, walmart]`: first with a webhook
  wins, else the main webhook. News feeds use `route_by_store: true`. Store scanners (not Pokémon Center) keep
  Pokémon items only if the name says Pokémon (skips Magic, Lorcana...). `topps_calendar` gets the Topps
  calendar watcher's alerts (falls back to `topps`, then main). `calendar` is STRICT (posts only with its own
  webhook): "added to calendar" posts, 15-min reminders for news/release dates, 8 AM digest.
  Store channels are PRODUCTS ONLY. NEWS (1.0.22): nothing general is posted any more - a news item is posted only when
  its HEADLINE is about cards (pokémon/topps/bowman/trading card/tcg/elite trainer/booster) AND is a raffle / drawing /
  lottery / invite; it goes to the store's channel + #drawings, once per story across all feeds (`state["news_seen"]`). Every raffle/drawing/invite (store scanners, Walmart draw page, Topps, watched pages,
  raffle news naming a store) posts to the store's channel AND `drawings` via `copy_to=["drawings"]`.
  `pokemon_queue` channel: `track_duration` on the queue watcher -> @everyone when up, one self-editing
  "up for X min" message, "closed · was up X" with start/end + recent history (`state["queue_history"]`). `LiveBoard` keeps a
  self-editing pinned message in `calendar` (next 14 days) and `topps_calendar` (all Topps products).
- More stores (1.0.15): costco, samsclub (Walmart platform: page data via `_merge_walmart_json`), cvs + walgreens
  (share the `pharmacy` channel: `notify.STORE_CHANNEL` / `channel_of`), ace, barnes. Store keys stay separate
  (colors, names, product ids); `channel_of(store)` picks the Discord channel everywhere (Notifier, ProductCards).
- Store channels (target, walmart, dicks, amazon, bestbuy, pokemon, + the stores above) = ONE self-editing message PER PRODUCT
  (`cards.py` `ProductCards`, background poster thread): link, ATC/Buy, big picture, price vs retail (MSRP), stock,
  limit, nearby stores. Edits in place on changes; new / back in stock / drawing open / loaded = delete + fresh
  post at the bottom (@everyone only for ETB/UPC). Unseen 24 h = deleted. Only card products in the user's sports
  (`settings["sports"]`: baseball/basketball/football - no soccer, WWE, F1) or Pokémon.
  Stock: Target = exact counts via redsky `product_fulfillment_v1` + `fiats_v1` (`stock.py`, stores near
  `settings["zip"]`, ≤14 lookups/run, each item ≤ every 10 min); other stores open ≤2 in-stock product pages per run
  for "Only N left" / Walmart page data.
- Channel boards (`LiveBoard`, one self-editing message per channel, only where that channel has a webhook):
  #drawings = current drawings; #topps = formats; #topps-calendar / #drop-calendar = schedules, same day-grouped
  layout (`_day_lines`). #pokemon-queue has a self-editing "No queue right now · last checked" line.
  "Sync all channels" (app top bar) -> `engine.request_sync()`: full scan, then every board is deleted and
  re-posted at the bottom of its channel, plus "🔄 All channels synced" in #app-status.
- Calendar channels (#topps-calendar, #drop-calendar) get NO single messages - only the full board. When something
  changes, the board is deleted and re-posted fresh at the bottom (`LiveBoard.tick(repost=True, content=...)`):
  Topps calendar with "@everyone · Topps calendar updated" + a bullet list of changes; drop calendar with
  "🗓️ Calendar updated" + what was added (no @everyone). No "added to calendar" posts, no 15-min reminders, no 8 AM
  digest there. Topps drop alerts (LIVE / 15 min / OPEN NOW) go to #topps.
- Calendar line format (`_day_lines` / `_event_line`): `__**Today**__` / `__**Wed Oct 7**__` headers, then
  "**11:00 AM** · 🏈 [Short Name](url)" (`_short` drops "Topps:", year, leading "Topps"; `_icon` = sport icon for Topps,
  🎟️ drawings, ⚡ store products); no time = "*time TBA*". Same day + same short name shows once. Topps FORMAT pages
  (/products/) never go on the calendar (purged on start) - only the calendar product (/pages/).
- Source failure posts: only after 30 min of failing in a row ("hasn't worked for 30+ min"), once, then "working again".
- Barnes & Noble: /s/ search URLs 404 - use the Pokémon CCG /b/ category and the collectible-card-games collection.
  Costco: CatalogSearch?dept=All&keyword=... (the /s? search returned unrelated items).
  Ace: /pokemon-cards and /trading-card-games category pages (search is robots-blocked); names end in "Mfr# ..."
  (stripped); "Pokemon X Trading Cards" (a pack) counts as sealed.
- GameStop (1.0.25): store key/channel `gamestop`, product ids from /products/<slug>/<id>.html, search pages.
- In-store restock tracker (1.0.25, Target only - the one store publishing per-store counts): `_refresh_target_stock`
  checks EVERY Target card product (incl. sold out online) - stores within `settings["restock_miles"]` (30) of
  `settings["zip"]` (77007) via fiats_v1. `_store_restocks`: a store going 0 -> N (or +5) = restock -> post to
  `instore` channel (else #target), @everyone for ETB/UPC; logged in `state["restock_log"]`; #in-store board
  (`_render_restocks`) shows each store's usual restock days/time learned from the log. First look = baseline.
- Channel names in the app: `instore` = "Mat local" (the restock tracker above), `lookup` = "In store look up".
  Look-up (1.0.26): the Discord BOT watches the channel its `lookup` webhook posts to (`ChatBot.lookup_channel_id`
  GETs the webhook for channel_id). A message that is a ZIP ("33175" or "33175 15" for miles) -> `engine.zip_lookup`:
  every Target card product known (≤60, ETB/UPC first) x fiats_v1 for that ZIP -> stores nearest first with counts,
  split into <2000-char messages. Target only (the one store with public per-store counts). Needs the bot token.
- Best Buy search URLs carry `intl=nosplash` (otherwise a "choose a country" splash page = empty).
- Every launch (incl. after an update) syncs by itself (`Engine.start` sets `sync_at`): after the first full pass all
  boards are re-posted and #app-status gets "🟢 Rip Radar X is running · all channels synced" with any failing sources.
- Drop calendar = `state["events"]` (kind: topps / drawing / release, store). ONLY the Topps calendar, Walmart
  drawings and store product pages that show a date/countdown (`product=True`), each linked to the product.
  News / Reddit never add calendar entries (`_add_event` rejects them).
- Topps: `topps_sitemap` watches topps.com/products/sitemap.xml -> the 2 highest-numbered product sitemaps (newest
  products). Sealed formats of EVERY line (`is_topps_sealed`: box/pack/blaster/mega/hobby/jumbo/value/case..., not Topps
  NOW / Living Set singles) are opened (`parse_topps_item_page`: og tags, JSON-LD, button text, ProductVariant id,
  "Limit per cart: N") and posted as cards in #topps (store key "topps"); @everyone on new + going live.
  `topps_products` now runs `times_only`: reads each calendar product's /pages/ page for the announced time
  (`best_time_for`) -> `state["topps_times"]` -> calendar gets the exact time ("🕐 Topps time announced" ping).
  `settings["topps_all_products"]` (default True): the Topps calendar includes Disney, F1, soccer...
- Walmart drawings: announced once when first seen; the 15-min / 1-min / OPEN NOW / 30-min-to-close pings come from
  `drawings_tick()` on the clock (runs after every source + every loop), not from page scans. No time on the page =
  "listed (open time not shown yet)", never "open". Times also come from page data (`_event_times`).
- Scheduler: sources run once per pass, EXCEPT `track_duration` (the queue), which re-runs whenever due even mid-pass
  (a full pass takes minutes). The queue saves every check to debug/pokemon-center-queue-last-check.html.
- Walmart drawings: open time from tile text, else the page's visible text near the item name, else the single
  "Drawing starts ..." time shared by the page; "closed" only from visible page text (page data has template strings).
- Pokémon Center queue: `check_queue` (keywords + `track_duration`) never goes into host_backoff; a blocked browser
  check gets a plain-HTTP second look; live = queue address OR short page with queue wording (a long homepage
  mentioning "virtual queue" is not live). Blocked = "Couldn't check" on the status line, never "no queue".
  Sources → "Test queue alert" posts a TEST sample (`engine.test_queue_alert`).
- Walmart pages: parse `__NEXT_DATA__` (`walmart_json_items`); the drawing page's tiles have no /ip/ links, so
  `title_tiles` (h3 titles + card text "Drawing starts Sep 30, 2:00pm PDT") is merged in. Prices read
  "$7994current price $79.94" -> use `price_in` (labelled price first).
- "Still running?" bot (`notify.ChatBot`): settings `bot_triggers` (phrases, matched in short messages),
  `bot_reply` (template: {uptime} {version} {last_scan} {sources_ok} {alerts_today} {problems} {time} {state}),
  `bot_channel` (name or ID; blank = any). Read on every message. When the app/PC is off the bot is offline and
  silent - it can't answer "no"; the stale #app-status time is the tell.
- `rip_radar/ui/index.html` – the whole UI (vanilla JS; talks to `Api` in app.py via `pywebview.api`).
- `rip_radar/updater.py` – GitHub Releases check + swap.
- User data lives in `%APPDATA%\RipRadar` (settings.json holds the webhook and any tokens).
- Tuning store parsers: the user clicks Settings → Save diagnostics, which zips the last HTML each source saw
  (`%APPDATA%\RipRadar\debug`) + log + status (never settings) to their Desktop; they attach it in chat.
  Parser tests with fixtures live in `tests/test_retail.py`.

## Rules
- @everyone (`ping=True`) ONLY for: Pokémon Center queue going live; every Topps calendar change (as ONE re-posted
  calendar with the list of changes), Topps drops going live / 15-min / open now (in #topps); Topps formats listed or going live; and any ETB / UPC
  (`parsing.is_etb_or_upc`) loaded-not-in-stock, in stock, back in stock, or drawing - in any channel, incl. news;
  and EVERY Amazon invite request ("Request invitation", any product) - #amazon card + #drawings copy.
  Everything else posts without @everyone. Store scanners ping "Loaded at X, not in stock yet" for every new
  card product (`alert_new_listed` defaults on) and back-in-stock pings show "Link was up X before stock".
- Never set a webhook `username`: posts must show the name/avatar the user gave each webhook in Discord
  (Palm Tree Edge Cards). "Rip Radar" is the program's name, not the poster's.
- Product pings: `notify.send(..., store=<key>, product=True)` -> store color, big image, footer, and the
  "🛒 ADD TO CART · ⚡ BUY NOW" row first (Walmart/Amazon/Best Buy/Topps direct links; others link the page).
  Stock/limit come from `parsing.stock_hint` when the page shows "Only N left" / "Limit N per order".
- Any launch of another copy of the app (install relaunch, update restart) must go through
  `winsys.launch_new_copy` (clean env + PYINSTALLER_RESET_ENVIRONMENT). Otherwise the one-file exe's child
  reuses the parent's deleted _MEI folder: "Failed to load Python DLL". CI's "Relaunch check" step guards this.
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
