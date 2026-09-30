import time
from datetime import datetime, timedelta

from rip_radar.parsing import CT
from tests.test_engine import FakeFetch, engine  # noqa: F401  (fixture)

PAD = "x" * 21000


def tile(tcin, name, price, button):
    return (f'<div><a href="/p/x/-/A-{tcin}"><img alt="{name}" src="https://t/{tcin}.jpg"></a>'
            f'<span>{price}</span><button>{button}</button></div>')


def test_target_board_lists_in_stock_and_out_of_stock(engine):
    t = next(x for x in engine.targets() if x["name"] == "Target · Pokémon cards")
    st = engine.state.setdefault("targets", {}).setdefault(t["name"], {})
    engine.notify.send = lambda *a, **k: None
    engine.fetcher = FakeFetch(tile("94300001", "Pokémon TCG: Delta Reign Elite Trainer Box", "$49.99", "Add to cart")
                               + tile("94300002", "Pokémon TCG: 30th Booster Bundle", "$26.99", "Out of stock")
                               + tile("94300003", "Pokémon TCG: Charizard ex Premium Collection", "$39.99",
                                      "Add to cart Only 2 left") + PAD, t["url"])
    engine.check_retail_search(t, st, True)                     # first look: silent, but remembered
    parts = engine._render_store("target")
    (t1, d1), (t2, d2) = parts
    assert t1 == "🟢 In stock at Target · 2" and "Delta Reign Elite Trainer Box" in d1 and "Only 2 left" in d1
    assert t2.startswith("⚪ Out of stock") and "30th Booster Bundle" in d2


def test_walmart_board_has_todays_drawings_and_drawings_board(engine):
    later = datetime.now(CT) + timedelta(hours=3)
    engine.state.setdefault("targets", {})["Walmart · drawings"] = {"drawings": {"1|x": {
        "phase": "upcoming", "name": "Pokémon TCG: 30th Celebration Booster Bundle 2-Pack", "seen": time.time(),
        "url": "https://www.walmart.com/ip/x/1", "price": "$79.94", "start": later.isoformat(), "end": ""}}}
    parts = engine._render_store("walmart")
    assert parts[0][0] == "🎟️ Drawings & raffles right now · 1" and "30th Celebration Booster Bundle" in parts[0][1]
    assert "opens" in parts[0][1]
    (title, text), = engine._render_drawings()
    assert "Walmart" in text and "$79.94" in text


def test_sync_reposts_every_board_after_a_full_scan(engine, monkeypatch):
    calls = []
    for board, _ in engine._all_boards():
        monkeypatch.setattr(board, "tick", lambda render, force=False, repost=False, b=board: calls.append((b.channel, repost)))
    engine.notify.send = lambda *a, **k: None
    engine.request_sync()
    assert engine.snapshot()["sync_pending"] is True
    engine.sync_at -= 1                                         # the pass below starts after the request
    report = {"x": "ok"}
    # simulate the loop's end-of-pass step
    if engine.sync_at and report:
        engine.sync_at = 0
        engine._boards_tick(repost=True)
    channels = {c for c, repost in calls if repost}
    assert {"calendar", "topps_calendar", "drawings", "topps", "target", "walmart", "dicks", "amazon", "bestbuy",
            "pokemon"} <= channels


def test_board_stays_under_discords_size_limit():
    from rip_radar.notify import LiveBoard
    b = LiveBoard(lambda: {}, "target", {}, "k")
    big = "\n".join(f"• [Pokémon TCG product number {i} with a long name]({'https://www.target.com/p/x/-/A-' + str(i)})"
                    for i in range(400))
    body = b._body(lambda: [("🟢 In stock", big), ("⚪ Out of stock", big), ("more", big)])
    total = sum(len(e["title"]) + len(e["description"]) for e in body["embeds"])
    assert total <= 6000 and all(len(e["description"]) <= 4096 for e in body["embeds"])
