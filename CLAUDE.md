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
- What counts (M's rules, latest 1.0.41): Pokémon = sealed (ETB, UPC, booster bundles/boxes/packs/blisters, tins,
  collections, chests); sports = sealed. 1.0.42: EVERY store is cards only - no accessories (sleeves, binders,
  playmats, deck boxes...) anywhere, incl. Pokémon Center, Walmart drawings and watched pages. ALWAYS cut
  (`is_excluded_listing`): SINGLE CARDS (`is_single_card`: card numbers like 102/128, "holo rare", "secret rare",
  "full art", Topps NOW, "#12", RC/auto/patch/refractor... unless it's a sealed product with bonus promos), graded
  slabs (PSA/BGS/CGC/SGC/TAG + grade, "graded", "slab"), card lots / bulk / random / mystery / repacks,
  battle/theme decks without packs. Posts for listings that stop qualifying are taken down.
- Focus (1.0.36, Settings → Focus, `parsing.focus_match`): `settings["focus_sets"]` (Pokémon sets: 🔥 tag; their
  Booster Box / Booster Bundle / ETB = @everyone) and `settings["everyone_products"]` (phrases matched word-by-word
  in order, season years normalised "25-26" = "2025-26"; most specific entry names it). ANY 2025-26 Topps / Bowman
  basketball product or "Flagg" = @everyone (Cooper Flagg rookie year). Applies to store cards, restocks, Walmart
  drawings, news. Merch (plush, pillows, video games, Squishmallows...) is cut even with "Pokémon TCG" in the name
  unless it's sealed (pack / bundle / tin / collection...).
- 1.0.37: THREE store-scanner browsers ("Rip Radar scanner", "… 2", "… 3"; `Engine(extra_browsers=...)`): the main
  loop runs a batch of up to 3 due sources at once, never two on the same site (`_run_batch`; `engine.fetcher` is a
  thread-local property). retail_search intervals are halved (min 120 s) when >1 browser.
  ATC: real add-to-cart links only for Walmart / Amazon / Best Buy / Topps; every other store's post says "No direct
  add-to-cart link at this store" instead of a fake ATC button. Images: `image_in` (lazy attrs, <source>, CSS bg) then
  `_fill_images` opens ≤2 product pages/run for og:image.
  Resale (`resale.py` ResaleChecker, setting `resale_check`): eBay SOLD search per product (plain HTTP, cached 24 h),
  titles must match ~75% of the product words, lots/graded/empty/ranges skipped, median of the rest -> "💸 Resale"
  field. PriorityQueue: live drops (priority 0) are searched IMMEDIATELY (~1-2 s) and `_resale_ready` edits the value
  into the existing post (no re-ping); everything else is valued in the background, 1 search / 8 s, so values are
  usually known before a drop. "💰 Deal" field (`cards.deal_verdict`): ✅ at retail / 🟡 above retail but under
  market / 🔴 over market. NOTHING is skipped for being over market (M's call) - it's information only.
  Daily recap (`recap_tick`/`render_recap`, setting `recap_hour`, default 21 = 9 PM CT, -1 off): drops by store,
  fastest sellouts (`state["sellouts"]`: how long each product stayed in stock), 🔥 focus drops, PC queue times.
- 1.0.39 (from M's 10/14 diagnostics): Best Buy 2026 links are /product/<slug>/<BSIN>; `bestbuy_page_items` reads
  the page's Apollo data (name, image, customerPrice, buttonState ADD_TO_CART/PRE_ORDER/SOLD_OUT/SEE_DETAILS=3P only,
  1P skuId -> real cart link; id = BSIN). Ace = Mozu JSON `data-mz-preload-PLPModel` (`ace_page_items`, incl.
  onlineStockAvailable count; product urls /<slug>/p/<code>). `BrowserFetcher.READ_PAGE` also serializes shadow roots
  (CVS renders its grid in web components). Queue browser waits 30 s for Imperva "Please stand by". Debug saves keep
  head+tail of >3 MB pages. clean_name strips prices first ("$349.99 New at Pokemon…"), rating/review/shipping text;
  promo text ("Flash Sale! …", "3.5 out of 5…") never becomes a name. Costco hides price/stock unless signed in.
  #watch channel (`watch` webhook; bot routes messages there to `engine.handle_watch_command`): `watch <words>` /
  `test <words>` / `watching` / `unwatch N` / `check focus`. `parsing.watch_match`: every word in the name (etb/upc
  synonyms, -exclude, @store). `_watch_alerts`: @everyone in #watch once per (term, store, item, event).
  `focus_report` (also Settings → Check focus lists) shows matches per focus set / @everyone product.
  IP safety (NO proxies - M's home IP only): per-site hourly page budgets (`SITE_BUDGET`, `settings["site_budgets"]`,
  drawings + queue exempt, "⏳ Easing off" notice), `_ease_off` 3-min breather when a site made the browser wait on a bot
  check, quiet hours `settings["quiet_hours"]` (2-6 AM: store intervals x2). Target tripwire thread
  (`target_tripwire_tick`): hot Target items not in stock (ETB/UPC, focus, #watch) get a product_fulfillment check
  every 90 s (≤120/h); a flip to in stock re-scans Target immediately. Topps calendar 90 s, sitemap hot items 60 s.
- `cards_only: true` (1.0.40) on Barnes & Noble, Hot Topic, Amazon, GameStop, Dick's, Costco: each has exactly one
  Pokémon-cards scan + one sports-cards scan, and keeps CARDS only (`parsing.is_card_only`: sealed or singles; no
  accessories / merch). Since 1.0.42 `cards_only` defaults to TRUE for every store. No store keeps single cards (1.0.41).
- ❌ to block (1.0.43): the bot's `on_raw_reaction_add` (❌ ✖️ 🚫 ❎) on any post -> `engine.block_product(message_id,
  title)`: our own product card (found by message id in `state["product_cards"]`, which now stores the name) or any
  post whose embed links a product (name via `parsing.name_from_title`) -> name added to `settings["blocked_products"]`,
  every matching card retired, other posts deleted by the bot if it has Manage Messages, a 45 s confirmation.
  `parsing.blocked_by`: all words of the blocked name (one may differ for 5+ words) = the same item at any store;
  "=words" entries come from typing `block <words>` in #watch. `blocked` / `unblock N` in #watch. Applied to store
  scans, Topps formats, Walmart drawings, restocks and #watch alerts.
- 1.0.44: Topps FAST CHECK (`Engine.topps_fast_tick`, own thread): hot Topps formats (`_hot_topps_items`: not live, and
  dropping within 48 h / new in 3 days / focus / @everyone / #watch) -> plain-HTTP GET topps.com/products/<handle>.js
  (Shopify product JSON, `parsing.shopify_product_js`) every 60 s each, ≤240/h. Available -> `rec["force"]` + Topps
  sitemap source due now -> that page is opened first and the normal card/@everyone goes out. 403/429 or 10 fails ->
  paused 30 min (the normal page checks continue). Startup message line "⚡ Topps fast check" (`topps_fast_health`).
  Costco / Sam's Club sign-in was tried in 1.0.44 and REMOVED in 1.0.45 at M's request: Costco posts are links only
  (no member price / stock).
- 1.0.46 (M's 10/14 2:48 PM diagnostics; `python tools/diag_filter.py <unzipped dir> [--cut]` replays every saved
  store page through the app's own parser + `Engine._keep_filter`): `purge_unwanted_cards()` at launch + every 6 h takes
  down every product post the CURRENT rules reject (old singles / toys / books from before a filter change, ❌'d items)
  and posts for products no longer tracked (removed sources). Back-in-stock FLICKER guard: out < 10 min then back
  (`rec["oos_since"]`) = no new ping (Hot Topic flapped twice in 2 min). Pokémon Center scans = sealed categories only
  (new-releases?category=trading-card-game 420 s, elite-trainer-box 900 s, boxed-sets 900 s; never sped up by the
  3-browser halving) - /category/tcg-cards is all sleeves/binders/playmats. Queue protection: queue blocked 2+ checks
  in a row -> Pokémon Center product scans pause; 3+ -> queue every 120 s (8+ -> 180 s, drop mode 60 s) until it
  gets through. Barnes & Noble = ONE scan (collectible-card-games, products: cards): its Pokémon pages are books/toys.
- 1.0.47: ✅ ❌ come pre-placed under every product post: `notify.REACTIONS` (own thread) PUTs both reactions with the
  bot token (REST, paced, 429-aware) after `ProductCards` posts (`saved[key]["reacted"]`) and after product
  `notify.send` posts (`?wait=true` -> channel_id/id). `ProductCards.react_existing()` adds them once to older posts.
  M tapping ❌ (2nd reaction) -> block; ✅ -> `engine.keep_product` (state["kept_products"], `kept` in #watch, undoes
  a matching ❌ block). The bot ignores its own reactions. Needs Add Reactions + Read Message History.
- 1.0.48: TARGET ID WATCH (#watch: `watch tcin 94681234`, a target.com link, or a bare 8-9 digit ID; `unwatch tcin
  …`; ≤15 IDs; `settings["watch_tcins"]`, `state["tcin_watch"]`). `tcin_watch_tick` (Target tripwire thread) asks
  redsky pdp_client_v1 (public product data the product page loads; `stock.target_pdp` / `parse_target_pdp`, via the
  hidden Target window) + product_fulfillment, each ID every max(90, 30×IDs) s. Phases not loaded -> loaded -> drawing
  (DRAWING_WORDS outside product copy) -> in stock; moving up = @everyone in #watch (drawing also #drawings).
  First look = baseline. NO ID enumeration / guessing ranges (M's home IP; only IDs he pastes). Target search
  sources already sortBy=newest.
  CRUNCHYROLL STORE (store key `crunchyroll`, shares #hottopic via STORE_CHANNEL; ids /products/<slug>-<UPC>.html):
  one Pokémon scan of /collections/trading-cards (search is robots-blocked) + "Crunchyroll queue" watcher
  (`site: Crunchyroll Store`, channel hottopic) in the queue browser. Queue code is generic via `_queue_site(t)`
  (messages, debug file names, history key `queue_history_<slug>`); bot-check posts and the blocked-queue
  protection stay Pokémon Center only.
- 1.0.49 RESALE FIX: eBay answered HTTP 403 to EVERY plain request (M's 10/14 log) and failures were cached 24 h, so no
  values ever showed. Now: hidden "Rip Radar resale" browser window (`App.resale_browser` -> `Engine(resale_fetch=)` ->
  `ResaleChecker(browser_fetch=)`, plain HTTP only as fallback), failures retried after 30 min (`FAIL_TTL`, cache
  entries carry "ok"), background pace 1 / 15 s. Posts always show the state: "⏳ checking eBay sold listings…" ->
  value, or "No recent eBay sales match this product", or "⚠️ eBay didn't answer · retrying in 30 min"
  (`Engine.RESALE_*`). `resale.health()` line in the startup message; `_resale_watchdog` posts to #app-status once
  after 15 failures in a row (and once when it works again). #watch: `value <product>` (live eBay lookup) and `resale`.
- 1.0.50 💸 PRICE LIST: `prices` channel (webhook "prices", else falls back to #watch - `LiveBoard` channel may be a
  preference tuple). `price_list_items()`: every focus set x (Booster Box, Booster Bundle, ETB) and every @everyone
  phrase that names a product line (has a year; near-duplicates like NBA Hoops / Topps Hoops collapse; names like
  Cooper Flagg skipped) x (Hobby, Mega, Blaster). `price_list_tick` once a day at `price_list_hour` (8 AM CT, -1 off):
  re-looks each up on eBay at resale priority 1 (0 = live drops, 2 = background); results -> `state["price_history"]`
  (30 days) via `_resale_ready` -> self-editing board: median, ▲▼ % vs the previous day, × retail (msrp). #watch:
  `prices`, `prices refresh`. 1.0.51: each daily run (and `refresh` typed in the price-list channel - the bot
  routes that channel to `engine.handle_prices_command`) re-asks eBay for every item (`state["price_refresh"]`
  pending keys, priority 1 paced 6 s) and, when all answered or after 20 min, posts a NEW message
  (`_post_price_list`: forgets the old board id, "💸 Price list · <day> · daily update / refreshed on request");
  earlier lists stay as a record. Boards sync skips the prices board while a refresh is running.
- 1.0.52 PRESALES + RAFFLE ROUTING: new `presale` channel ("Presales & pre-orders"). Any store product whose name /
  status / Shopify tags say PRESALE / PRE-SALE / PRE SALE / pre-order (`parsing.is_presale`, tags like
  "DeltaReignPreorder" too) gets a "🛍️ PRESALE · …" copy in #presales (strict: only with its webhook) besides its
  store card; Topps Pre-order events too. Any product with raffle / lottery / drawing in its name
  (`parsing.is_raffle`) or a Drawing status gets the #drawings copy. COLLECTOR STORE (`collectorstore`, Shopify,
  channel = presale via STORE_CHANNEL): /collections/coming-soon/products.json (plain HTTP, 180 s;
  `parsing.shopify_collection_items`, used by parse_retail_tiles for `shopify: True` stores or any products.json:
  real cart link /cart/<variant>:1). Walmart drawing button now reads "OPEN PRODUCT → tap ENTER DRAWING": Walmart
  has no direct entry link - the Enter drawing button is on the product page (and the app never enters for M).
- 1.0.54 PRICE ACCURACY (M: "Walmart prices are off"): audit of the 10/14 pages = every app price matched Walmart's own
  data / tile ("Price Now $X"), BUT nearly every Walmart search listing is a MARKETPLACE RESELLER (all 40 Pokémon
  listings by "Mascot"; 5 of 50 sports by Walmart.com) - the price is the reseller's. Now: `walmart_json_items` keeps
  `sellerName` -> post field "Sold by: ✅ Walmart itself" / "⚠️ <seller> (3rd-party reseller - NOT Walmart's own
  price)" (`parsing.first_party`); Price shows "· since 3:10 PM" (`price_since`, set when the price changes); a carded
  product not seen on the store page for 30+ min gets "⚠️ Price check: may have changed" (`rec["stale"]`, cleared the
  next time it's seen).
- 1.0.55: commands in #watch / #price-list never go silent: `ChatBot._reply` posts as the bot, else through that
  channel's webhook (and `bot.state` names the missing permission); a handler exception is answered with the error;
  `channel_id_of` no longer caches an empty / failed webhook lookup.
- 1.0.56 QUEUE SELF-HEAL (10/14 log: the PC queue check recovered right after every app restart): `_queue_self_heal`
  (queue thread) - 5 blocked PC queue checks in a row (~7-10 min), no queue live, ≥45 min since the last one
  (`state["last_self_heal"]`) -> #app-status "♻️ Restarting Rip Radar…" -> `restart_hook` = `updater.restart_self`
  (cmd script waits for this PID to exit, then `start RipRadar.exe --background`, via launch_new_copy). Discord
  `restart` in the urgent-only channel (`engine.request_restart`) does the same on demand.
- 1.0.57 PRICE LIST FIX (M: "price list not working"): `resale.parse_sold_generic` fallback reads any eBay layout
  (every /itm/<id> link + first price in its box, shipping / "was" removed, ranges skipped) when the s-item / s-card
  reader finds nothing; `_get` accepts a browser page with listing links / "results for" as an answer and names bot
  checks ("pardon our interruption") in `stats["last_page"]`; `queued` is now key -> priority so a price-list
  request (1) jumps a waiting background lookup (2). Price list shows the AVERAGE sold price (`summarize` adds "avg";
  history stores avg), one check per item per day; when eBay isn't answering the list says so at the top.
  #price-list `status` = eBay health + refresh progress.
- 1.0.58 diagnostics: the last eBay page is saved as debug/ebay-last-lookup.html (`ResaleChecker.debug_save`, header
  comment = query + HTTP + browser/http); "eBay answered but no sold listings matched" lands in `stats["last_page"]`;
  the bot logs every message it hears (channel + id) and when a watch/prices webhook's channel can't be read;
  status.json in the zip adds bot state, which webhook keys are set (never URLs), channels the bot resolves, resale
  stats and price-refresh progress (`Api._diag_extra`).
- 1.0.59 price list (M pasted a list of all ⏳): cells now say ⚠️ when eBay didn't answer (cache ok=False) vs ⏳ not
  looked up yet vs — no matching sales; a "💸 Price list · status" block on top counts priced / no sales / failed /
  waiting (+ the last eBay problem); failed items re-asked every 30 min (`_price_retry_at`); a refresh waits up to 45
  min before posting; fewer rows: high-end lines (`HOBBY_ONLY`: Impeccable, National Treasures, Crown Royale, Court
  Kings, Contenders, Prizm Draft Picks, Bowman University...) get Hobby only, and a bare "2025-26 Topps" row is dropped.
- 1.0.60 @EVERYONE ONLY IN #WATCH (M, 10/18: store channels kept pinging): `settings["everyone_only_in_watch"]` (default
  on, needs the watch webhook). `Notifier.send(ping=True)` to any channel other than #watch posts there QUIETLY and
  makes ONE `Notifier.everyone_copy` in #watch (deduped per product link for 10 min, so store + #drawings +
  #presales copies ping once). Product cards (stores + Topps) post quietly; the engine sends the #watch copy.
  `_watch_alerts` posts count as that item's ping (and skip if it already pinged). Exceptions (`everyone_ok=True`):
  the queue-live alerts (Pokémon Center / Crunchyroll) and the Topps calendar re-post (LiveBoard content).
- 1.0.61 (M's 10/18 startup message: "Target store counts ❌ fiats_v1 HTTP 435 PerimeterX captcha" and "Resale 0 priced
  of 2, 25 failed"): TARGET GUARD in stock.py (`GUARD`, `_guard_ok`, `_trip`): every Target stock / pdp request shares
  ≤60/h; a block (403/429/435 or px-cdn / captcha text) pauses ALL Target stock requests 2 h (4, 6, 8 h on repeats;
  forgiven after 6 quiet h), persisted in `state["target_guard"]`. `_refresh_target_stock` 4/run, hot items
  (ETB/UPC/focus) every 15 min, the rest hourly; tripwire every 5 min ≤24/h; ZIP look-up ≤20 products, 1.5 s apart,
  `urgent=True` (skips the hourly cap, never the pause). EBAY: per-product lookups OFF (one-time migration
  `resale_off_1061` sets resale_check False); the price list still runs (`lookup(force=True)`); price-list pace
  `LIST_PACE` 45 s; an eBay bot-check page pauses all lookups 1 h (`stats["paused_until"]`); health ❌ when failures
  outnumber answers, ⏸️ while paused. Never solve / click through either check - wait it out.
- 1.0.62 CALENDARS QUIET + HEALTH CHECK: #topps-calendar / #drop-calendar move to the bottom (+ @everyone for Topps) ONLY
  when a drop that was never on them is added (Topps: `st["ever"]` slugs; drop calendar: `state["cal_seen_urls"]` by
  product link, 60 days). Status / time / moved / removed = board edited in place (`_quiet_topps_calendar`), no ping.
  Launch / update / "Sync all channels" no longer re-post the two calendar boards (edit in place). 🩺 DAILY HEALTH
  CHECK (`health_tick` / `render_health`, setting `health_hour` 9 AM CT, -1 off; `health` in #watch): every source
  ✅ / 🟡 loads-but-found-nothing for 24 h (`st["last_found_at"]`: health "ok (N" with N>0, or ok without a count) /
  ❌ failing (since `bad_since`, last worked `last_ok_at`), plus queue, Target counts, Topps fast check, eBay, price
  list, bot, alerts in 24 h.
- 1.0.63 NEW SOURCES: "Topps launches (EQL raffles)" = `listing` source on launches.topps.com/en-US/ (browser, 300 s,
  link_pattern /en-US/launch/<slug>): Topps' entry-window raffles for its hottest hobby boxes; a new launch page =
  "🎟️ TOPPS RAFFLE (EQL) open" in #topps + #drawings + @everyone (#watch). `listing` sources now take `label`,
  `link_label`, `ping`, `copy_to`, `debug` (saves the page for diagnostics) and respect the ❌ blocklist.
  "News · PokeBeach" RSS (Pokémon Center invite-only / lottery news first). News raffle words + "early access", "eql".
- 1.0.64 POKÉMON RELEASE DATES + EQL RAFFLES (`releases.py`, pure + tested): `dated_products` (product names next to
  dates: "X - Available Nov 6", a date heading + the products under it, a product + its "Release date" line / table
  cell; `iter_when(horizon_days, past_hours)` widened), `calendar_grid` (month grid: "October 2026" + day cells
  "16 Target …"), `eql_launch_links` / `parse_eql_launch` (name, og:image, price, "Maximum of N per customer",
  opening/closing times from page data or text), `similar` (same product from two sources). Sources:
  `pokemon_releases` (pokemon.com monthly round-up, this + next month, /us/news/ then /us/pokemon-news/, 6 h),
  `release_schedule` (press.pokemon.com schedule, 6 h; `grid: true` = pokemonrestock.com calendar, 1 h) -> calendar
  kind "pokemon" 🎴 via `_add_pokemon_release` (cards only, ❌ blocklist, deduped by day + `similar`); the drop
  calendar board adds "🎴 Pokémon releases after that" (beyond 14 days). `eql_launches` ("Topps raffles (EQL)",
  replaces the 1.0.63 listing source): every launch page opened (≤4/run, 30 min / 10 min near close), state
  `state["eql_launches"]`; new / coming-soon -> open / 1 h before close = @everyone (#watch) + #topps + #drawings with
  the exact EQL link ("🎟️ ENTER THE RAFFLE ON EQL"); open + close times on the drop calendar. `_note_eql_links` picks
  launch links off Topps calendar / product pages too. EQL has no public all-brands list; its other card launches
  are Australian Pokémon shops - not tracked.
- 1.0.65: #drawings board exists ONLY while a raffle / drawing is caught (`_render_drawings` -> None when empty;
  `LiveBoard.tick`: render() None = delete the old message, post nothing); EQL raffles (open / coming soon) are on it.
  PC QUEUE DROP WINDOW (`Engine.queue_window`, setting `queue_window` {"days": [0-4], "start": 9, "end": 15} CT,
  "days": [] = off; from tcgdropradar's log: weekdays ~10 AM-4 PM ET, median 11:12 AM CT - a summary, no live feed /
  per-drop log to follow). 1.0.66 (M: worried about an IP ban): the window does NOT speed the queue up - it stays 60 s
  (120 s in quiet hours 2-6 AM; 30 s ONLY in drop mode M turns on) and PC product scans REST in the window, so the
  queue check is our only Pokémon Center traffic there (~60 requests/h). Reliability counts
  (`state["queue_checks"]` per day: checks / blocked, in-window) on the #pokemon-queue status line and in the health
  check (`queue_reliability`, `_queue_day_line`).
- 1.0.67 TCG DROP RADAR POSTS (`dropradar_archive`, plain HTTP, 120 s): buttondown.com/tcgdropradar - the archive is
  PUBLIC (no login; never handle M's password). RSS (`rss:` list) first, else archive links /tcgdropradar/archive/<slug>.
  New post with drop words (`RE_DROP_POST`) -> `_dropradar_post`: "Pokémon Center … queue" = second opinion on our
  queue check -> #pokemon-queue, @everyone (#watch) only if our checker hasn't seen a queue in the last hour; drawing /
  raffle / invite -> store channel + #drawings (@everyone for ETB/UPC); restock / loaded -> store channel. Articles skipped.
- 1.0.68 PHONE ALARM FOR THE QUEUE: `Notifier.send(alarm=True)` = also a Twilio text and a ntfy push (Priority max,
  "🚨 QUEUE IS UP"). Only the Pokémon Center queue going live (`_track_queue`), the TCG Drop Radar queue post when our
  checker missed it, and Sources → "Test queue alert" are alarms. Settings `sms_for` / `ntfy_for` default ["queue"]
  (= alarms only; "urgent" / "all" widen it); one-time migration `alarm_1068` turns an old ["urgent"] sms_for into
  ["queue"]. `notify.last_sms_error` keeps Twilio's last refusal.
- Captcha detection judges VISIBLE text (+ captcha iframes) only: normal pages load Imperva / reCAPTCHA scripts too.
  3 checks in a row before a 🛡️ post.
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
- Hot Topic (1.0.33): store key/channel `hottopic`, ids /product/<slug>/<id>.html, trading-cards category + search.
- Queue live -> `winsys.open_in_chrome` opens pokemoncenter.com in M's own Chrome (setting `open_browser_on_queue`,
  default on). He joins the line himself; the app never clicks anything there.
- Rip Radar NEVER changes the PC's power, sleep, lid, battery, Windows Update or network-adapter settings - M sets
  those himself in Windows. The one exception M asked back for (1.0.35): Settings → "Keep this PC awake while Rip
  Radar runs", OFF by default (`settings["keep_awake"]`); `Engine._apply_keep_awake` calls `winsys.keep_awake`
  (SetThreadExecutionState, a temporary request, no settings changed) from the scanner thread each loop.
- GameStop (1.0.25): store key/channel `gamestop`, product ids from /products/<slug>/<id>.html, search pages.
- In-store restock tracker (1.0.25, Target only - the one store publishing per-store counts): `_refresh_target_stock`
  checks EVERY Target card product (incl. sold out online) - stores within `settings["restock_miles"]` (30) of
  `settings["zip"]` (77007) via fiats_v1. `_store_restocks`: a store going 0 -> N (or +5) = restock -> post to
  `instore` channel (else #target), @everyone for ETB/UPC; logged in `state["restock_log"]`; #in-store board
  (`_render_restocks`) shows each store's usual restock days/time learned from the log. First look = baseline.
- Target stock requests (1.0.32) go through `app.TargetStockBrowser`: a hidden window parked on target.com runs a
  synchronous XHR to redsky with the page's own apiKey + visitorId cookie (plain Python requests were turned away).
  `stock.target_stock(browser_json=...)`; `stock.LAST_ERROR` says why it failed; the startup message reports
  "🏬 Target store counts: ✅/❌" (`engine.stock_health`).
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
- Queue has its OWN hidden browser window ("Rip Radar queue watch", `BrowserFetcher(scroll=False, settle=2,
  challenge_wait=12)`) and its own thread (`Engine._queue_loop`): never waits behind store pages; the main loop skips
  `track_duration` sources while that thread runs. DROP MODE (`settings["drop_mode_until"]`; app top-bar button = 3 h;
  Discord "drop mode on" / "drop mode 5" / "drop mode off" ONLY in the urgent-only channel = the channel of
  `discord_webhook_urgent`, or a channel named *urgent* if that webhook isn't set; ignored elsewhere): queue every 30 s and the
  Pokémon Center product scanners are skipped (less PC traffic = fewer bot walls). The ONLY exception to the 60 s rule.
- Bot-check watch (1.0.29): `parsing.wall_kind` names the wall (Imperva, hCaptcha, reCAPTCHA, PerimeterX, Cloudflare,
  DataDome, Akamai...). `_watch_bot_check` posts to the queue channel (no @everyone) when Pokémon Center's bot check
  turns on / off for the checker (2 checks in a row, ≥20 min apart). Detect only - never solve or click through.
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
- Since 1.0.60 every product @everyone below lands in #watch only (store channels quiet) - see 1.0.60 note.
- @everyone (`ping=True`) ONLY for: Pokémon Center queue going live; every Topps calendar change (as ONE re-posted
  calendar with the list of changes), Topps drops going live / 15-min / open now (in #topps); Topps formats listed or going live; and any ETB / UPC
  (`parsing.is_etb_or_upc`) loaded-not-in-stock, in stock, back in stock, or drawing - in any channel, incl. news;
  and EVERY Amazon invite request ("Request invitation", any product) - #amazon card + #drawings copy;
  and M's Focus lists (focus-set Booster Box / Bundle / ETB, the @everyone product list, 2025-26 Topps/Bowman hoops).
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
- Keep scan intervals ≥ 60 s per source (exception: the Pokémon Center queue at 30 s while drop mode is on - only
  when M turns it on). Pokémon Center traffic stays minimal: M's home IP must never get banned there.
- pywebview 6.x: `closing` handlers returning False cancel the close (used for hide-to-tray);
  `load_url`/`evaluate_js` block until the window is shown/ready (hidden windows still fire `shown`).
- topps.com returns 403 to plain HTTP from data-center IPs (the build machine's selftest always shows
  Topps as blocked). The engine's `_fetch` auto-switches any blocked non-feed source to the hidden
  browser window (sticky per source via `auto_browser` in state), so the installed app still reads it.
- The Topps calendar page is server-rendered HTML: cards are links to `/pages/<slug>` with text like
  "Wednesday, Sep 30 at 4:00 PM UTC 2026 Bowman Football" and a button (Notify me / Pre-order).
