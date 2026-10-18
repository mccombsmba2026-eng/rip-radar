"""How many are left. Target publishes exact counts (online and per store) through the same stock service its
product pages use; other stores only say "Only N left" when it's low, or a number in their page data.
Read-only lookups, a handful per scan - nothing is added to a cart."""
import json
import logging
import re
import time

log = logging.getLogger("rip_radar")

# the public web key Target's own pages send (read fresh from a Target page when one is seen)
TARGET_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
REDSKY = "https://redsky.target.com/redsky_aggregations/v1/web"
RE_TARGET_KEY = re.compile(r'apiKey\\?"\s*:\s*\\?"([0-9a-f]{40})')


def target_key_in(html):
    m = RE_TARGET_KEY.search(html or "")
    return m.group(1) if m else ""


def _walk(o, depth=0):
    if depth > 30:
        return
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, depth + 1)


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _store_name(d):
    for k in ("location_name", "store_name", "name"):
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k].strip()
    s = d.get("store") if isinstance(d.get("store"), dict) else {}
    for k in ("location_name", "store_name", "name"):
        if isinstance(s.get(k), str) and s[k].strip():
            return s[k].strip()
    return ""


def parse_target_fulfillment(*payloads):
    """Target stock JSON (product_fulfillment_v1 / fiats_v1) ->
    {"online": int|None, "online_status": str, "stores": [(name, qty)], "sold_out": bool}"""
    out = {"online": None, "online_status": "", "stores": [], "sold_out": False, "distance": {}}
    seen = set()
    for data in payloads:
        for d in _walk(data):
            ship = d.get("shipping_options")
            if isinstance(ship, dict):
                q = _num(ship.get("available_to_promise_quantity"))
                if q is not None:
                    out["online"] = max(q, out["online"] or 0)
                out["online_status"] = out["online_status"] or str(ship.get("availability_status") or "")
            if d.get("is_out_of_stock_in_all_store_locations") is True and out["online"] in (None, 0):
                out["sold_out"] = True
            if "location_available_to_promise_quantity" in d:
                name, q = _store_name(d), _num(d.get("location_available_to_promise_quantity"))
                if name and q is not None and name not in seen:
                    seen.add(name)
                    out["stores"].append((name, q))
                    dist = d.get("distance")
                    if dist is None and isinstance(d.get("store"), dict):
                        dist = d["store"].get("distance")
                    try:
                        out["distance"][name] = float(dist)
                    except (TypeError, ValueError):
                        pass
    out["stores"].sort(key=lambda x: -x[1])
    return out


LAST_ERROR = {"text": ""}           # why the last Target stock request failed (shown in look-up replies / Sources)

# Target's bot protection (PerimeterX: HTTP 435 + a captcha script) flags a home connection that asks too often.
# Every Target stock request goes through this guard: an hourly cap shared by every feature, and a pause (2 h, then
# longer) the moment Target pushes back. The app never tries to get past the check - it waits it out.
GUARD = {"pause_until": 0, "pauses": 0, "recent": [], "per_hour": 60}


def target_paused():
    return time.time() < GUARD["pause_until"]


def _guard_ok(cost=1, urgent=False):
    now = time.time()
    if target_paused():
        LAST_ERROR["text"] = (f"paused - Target's bot check blocked stock requests; trying again at "
                              f"{time.strftime('%I:%M %p', time.localtime(GUARD['pause_until'])).lstrip('0')}")
        return False
    GUARD["recent"] = [t for t in GUARD["recent"] if now - t < 3600]
    if not urgent and len(GUARD["recent"]) + cost > GUARD["per_hour"]:
        LAST_ERROR["text"] = f"hourly limit reached ({GUARD['per_hour']} Target stock requests/hour) - waiting"
        return False
    GUARD["recent"].extend([now] * cost)
    return True


def _blocked_answer(status, text):
    t = (text or "")[:600].lower()
    return status in (403, 429, 435) or "px-cdn" in t or "perimeterx" in t or "captcha" in t


def _trip(status, text):
    GUARD["pauses"] += 1
    hours = min(8, 2 * GUARD["pauses"])
    GUARD["pause_until"] = time.time() + hours * 3600
    LAST_ERROR["text"] = (f"Target's bot check (HTTP {status}) blocked stock requests - paused {hours} h, until "
                          f"{time.strftime('%I:%M %p', time.localtime(GUARD['pause_until'])).lstrip('0')}")
    log.info("Target stock: %s", LAST_ERROR["text"])


def target_stock(session, tcin, zip_code="", key="", timeout=15, miles=30, stores_only=False, browser_json=None,
                 urgent=False):
    """Exact counts for one Target item: shipping + nearby stores. None if Target didn't answer.
    browser_json(url) -> (status, text): asks from inside a hidden browser on target.com - with Target's own cookies,
    visitor id and current key, exactly like Target's product page does. Plain HTTP is only the fallback."""
    key = key or TARGET_KEY
    got = []
    if not _guard_ok(cost=1 if (stores_only or not zip_code) else 2, urgent=urgent):
        return None
    tries = [] if stores_only else [(f"{REDSKY}/product_fulfillment_v1",
                                     {"key": key, "tcin": tcin, "zip": zip_code, "channel": "WEB", "is_bot": "false",
                                      "page": f"/p/A-{tcin}"})]
    if zip_code:
        tries.append((f"{REDSKY}/fiats_v1", {"key": key, "tcin": tcin, "nearby": zip_code, "radius": miles, "limit": 30,
                                             "include_only_available_stores": "false", "requested_quantity": 1,
                                             "channel": "WEB", "page": f"/p/A-{tcin}"}))
    if browser_json:
        from urllib.parse import urlencode
        for url, params in tries:
            try:
                status, text = browser_json(url + "?" + urlencode(params))
                if status and status < 400 and text.strip().startswith("{"):
                    got.append(json.loads(text))
                elif _blocked_answer(status, text):
                    _trip(status, text)
                    return None
                else:
                    LAST_ERROR["text"] = f"{url.rsplit('/', 1)[-1]}: HTTP {status} {text[:120]!r}"
                    log.info("Target stock (browser) %s", LAST_ERROR["text"])
            except Exception as e:
                LAST_ERROR["text"] = f"browser: {type(e).__name__}: {e}"[:200]
                log.info("Target stock (browser) %s: %s", tcin, e)
        if got:
            if GUARD["pauses"] and time.time() - GUARD["pause_until"] > 6 * 3600:
                GUARD["pauses"] = 0                     # quiet for 6 h after a pause: forgive it
            return parse_target_fulfillment(*got)
    for url, params in tries:
        try:
            r = session.get(url, params=params, timeout=timeout,
                            headers={"Accept": "application/json", "Origin": "https://www.target.com",
                                     "Referer": f"https://www.target.com/p/-/A-{tcin}"})
            if r.status_code < 400:
                got.append(r.json())
            elif _blocked_answer(r.status_code, r.text):
                _trip(r.status_code, r.text)
                return None
            else:
                LAST_ERROR["text"] = f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code} {r.text[:120]!r}"
                log.info("Target stock %s", LAST_ERROR["text"])
        except Exception as e:          # network / not JSON
            LAST_ERROR["text"] = f"{type(e).__name__}: {e}"[:200]
            log.info("Target stock %s: %s", tcin, e)
    return parse_target_fulfillment(*got) if got else None


def target_stock_text(info, zip_code=""):
    """-> (stock line, stores line)"""
    if not info:
        return "", ""
    if info["online"] is not None:
        stock = f"{info['online']} available online" if info["online"] > 0 else "0 online"
    elif info["sold_out"]:
        stock = "Sold out everywhere"
    else:
        stock = info["online_status"].replace("_", " ").title()
    have = [(n, q) for n, q in info["stores"] if q > 0]
    stores = [f"{n} **{q}**" for n, q in have[:8]]
    where = f"Stores near {zip_code}" if zip_code else "Stores"
    if not info["stores"]:
        return stock, ""
    if not have:
        return stock, f"{where}: none in stock at {len(info['stores'])} stores checked"
    more = f" · +{len(have) - 8} more" if len(have) > 8 else ""
    return stock, f"{where}: " + " · ".join(stores) + more


# ---------------------------------------------------------------- other stores' product pages
STOCK_KEYS = ("availableQuantity", "available_quantity", "quantityAvailable", "inventoryQuantity",
              "available_to_promise_quantity", "onlineQuantity", "stockQuantity", "inventoryCount")
LIMIT_KEYS = ("maxOrderQuantity", "orderLimit", "purchaseLimit", "purchase_limit", "maxPurchaseQuantity",
              "max_purchase_quantity", "orderMaxQuantity")


def page_data_stock(data):
    """A count in a product page's embedded data (first product on the page), if the site includes one."""
    stock = limit = None
    for d in _walk(data):
        for k in STOCK_KEYS:
            if stock is None and k in d and _num(d[k]) is not None and not isinstance(d[k], bool):
                stock = _num(d[k])
        for k in LIMIT_KEYS:
            if limit is None and k in d and _num(d[k]) and not isinstance(d[k], bool):
                limit = _num(d[k])
        if stock is not None and limit is not None:
            break
    return stock, limit


# ---------------------------------------------------------------- Target ID watch (1.0.48)
# The same public product data Target's own product page loads (pdp_client_v1). A product ID ("TCIN") answers
# here as soon as Target sets the product up - often before it's searchable or linked anywhere.
DRAWING_WORDS = ("drawing", "sweepstake", "raffle", "lottery", "registration", "enter for a chance",
                 "chance to buy", "invite only", "invitation", "request an invite")


def target_pdp(tcin, browser_json=None, session=None, key="", store_id="3991", timeout=15):
    """-> (status, json or None). Asked from inside the hidden Target window (browser_json) when available."""
    from urllib.parse import urlencode
    params = {"key": key or TARGET_KEY, "tcin": tcin, "is_bot": "false", "store_id": store_id,
              "pricing_store_id": store_id, "has_pricing_store_id": "true", "has_financing_options": "true",
              "include_obsolete": "true", "skip_personalized": "true", "skip_variation_hierarchy": "true",
              "channel": "WEB", "page": f"/p/A-{tcin}"}
    url = f"{REDSKY}/pdp_client_v1?" + urlencode(params)
    if not _guard_ok(cost=1):
        return 0, None
    try:
        if browser_json:
            status, text = browser_json(url)
        else:
            r = session.get(url, timeout=timeout, headers={"Accept": "application/json",
                                                           "Origin": "https://www.target.com"})
            status, text = r.status_code, r.text
    except Exception as e:
        LAST_ERROR["text"] = f"pdp: {type(e).__name__}: {e}"[:200]
        return 0, None
    if _blocked_answer(status, text) and not (text or "").strip().startswith("{"):
        _trip(status, text)
        return 0, None
    try:
        return status, json.loads(text) if text and text.strip().startswith("{") else None
    except ValueError:
        return status, None


def _strip_descriptions(o):
    """Product copy (bullets, descriptions) can say 'drawing' innocently - leave it out of the drawing check."""
    if isinstance(o, dict):
        return {k: _strip_descriptions(v) for k, v in o.items()
                if k not in ("product_description", "bullet_descriptions", "downstream_description",
                             "soft_bullets", "marketing_description", "reviews", "ratings_and_reviews",
                             "questions", "wellness_merchandise_attributes")}
    if isinstance(o, list):
        return [_strip_descriptions(v) for v in o]
    return o


def parse_target_pdp(data, status=200):
    """pdp_client_v1 JSON -> {loaded, name, price, image, drawing, drawing_text, limit, street_date}."""
    out = {"loaded": False, "name": "", "price": "", "image": "", "drawing": False, "drawing_text": "",
           "limit": "", "street_date": ""}
    if not isinstance(data, dict) or status == 404:
        return out
    product = ((data.get("data") or {}).get("product")) or {}
    item = product.get("item") or {}
    title = ((item.get("product_description") or {}).get("title")) or ""
    if not title:
        return out
    import html as _html
    out["loaded"] = True
    out["name"] = _html.unescape(title)
    price = product.get("price") or {}
    out["price"] = price.get("formatted_current_price") or ""
    imgs = (item.get("enrichment") or {}).get("images") or {}
    out["image"] = imgs.get("primary_image_url") or ""
    for d in _walk(product):
        if not out["limit"] and _num(d.get("purchase_limit")):
            out["limit"] = str(_num(d.get("purchase_limit")))
        if not out["street_date"] and isinstance(d.get("street_date"), str):
            out["street_date"] = d["street_date"][:10]
    blob = json.dumps(_strip_descriptions(product)).lower()
    hit = next((w for w in DRAWING_WORDS if w in blob), "")
    if hit:
        i = blob.find(hit)
        out["drawing"], out["drawing_text"] = True, blob[max(0, i - 60): i + 80]
    return out
