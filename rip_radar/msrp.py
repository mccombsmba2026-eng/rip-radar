"""Typical retail (MSRP-level) prices, to catch reseller listings marked up over retail.

Numbers are generous upper bounds for a single unit at big-box stores. Special sets (like 30th Celebration)
often retail higher than regular sets, so these lean high on purpose: the goal is to catch listings at
1.5-3x retail, not to argue over a few dollars. Hobby boxes vary too much by product to judge, so they're skipped.
"""
import re

# (words that identify the product type, typical top retail price per unit). First match wins, so specific
# types come before general ones.
POKEMON = [
    (("ultra-premium collection", "ultra premium collection", " upc"), 149.99),
    (("elite trainer box", " etb"), 69.99),
    (("booster display", "booster box", "36 pack", "36-pack", "36 booster"), 179.99),
    (("booster bundle",), 39.99),
    (("mini tin",), 12.99),
    (("premium collection", "special collection", "super-premium collection"), 79.99),
    (("knock out collection", "tech sticker"), 14.99),
    (("binder collection", "poster collection", "pin collection"), 34.99),
    (("build & battle", "build and battle"), 24.99),
    (("battle deck", "league battle deck", "theme deck", "deck"), 29.99),
    (("3-pack blister", "3 pack blister", "blister", "3-pack", "3 pack"), 17.99),
    (("sleeved booster", "booster pack", "checklane"), 5.99),
    (("tin",), 29.99),
    (("surprise box", "collection box", "collection"), 49.99),
]
SPORTS = [
    (("hobby", "jumbo", "case", "breaker"), None),      # too variable to judge
    (("mega box", "mega"), 69.99),
    (("blaster",), 39.99),
    (("value box", "value pack"), 34.99),
    (("hanger",), 29.99),
    (("fat pack", "cello", "rack pack"), 14.99),
    (("tin",), 39.99),
    (("retail box", "box"), 39.99),
]
RE_MULT = [re.compile(p, re.I) for p in (
    r"\b(\d{1,2})\s*[- ]\s*(?:count|ct)\b",          # "10-Count Display", "4 ct"
    r"\b(\d{1,2})\s*[- ]\s*pack\s+bundle\b",          # "2-Pack Bundle"
    r"\b(?:set|lot|bundle|case) of (\d{1,2})\b",      # "Set of 2"
    r"(?<![a-z0-9])x\s?(\d{1,2})\b",                  # "x2"
)]


def _units(name):
    for rx in RE_MULT:
        m = rx.search(name)
        if m and 1 < int(m.group(1)) <= 24:
            return int(m.group(1))
    return 1


def typical_price(name, kind):
    """(total typical retail for the listing, per-unit price, units, product type) or None if unknown."""
    t = " " + (name or "").lower().replace("—", " ").replace("–", " ") + " "
    table = POKEMON if kind == "pokemon" else SPORTS
    for words, price in table:
        if any(w in t for w in words):
            if price is None:
                return None
            units = _units(t)
            return round(price * units, 2), price, units, words[0].strip()
    return None


def price_check(name, price_text, kind):
    """-> ("ok" | "over" | "way_over" | "unknown", info dict).
    over: >20% above typical retail. way_over: >60% above (reseller territory - no ping)."""
    try:
        price = float(str(price_text).replace("$", "").replace(",", ""))
    except ValueError:
        return "unknown", {}
    ref = typical_price(name, kind)
    if not ref or price <= 0:
        return "unknown", {"price": price}
    total, unit, units, ptype = ref
    ratio = price / total
    info = {"price": price, "msrp": total, "ratio": ratio, "units": units, "type": ptype}
    if ratio > 1.6:
        return "way_over", info
    if ratio > 1.2:
        return "over", info
    return "ok", info


def msrp_text(info):
    if not info.get("msrp"):
        return ""
    each = f" ({info['units']} × ~${info['msrp'] / info['units']:.2f})" if info.get("units", 1) > 1 else ""
    return f"~${info['msrp']:.2f}{each}"
