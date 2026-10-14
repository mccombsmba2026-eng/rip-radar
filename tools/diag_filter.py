"""Run a diagnostics zip's saved store pages through the app's own parser + filters.
python tools/diag_filter.py <unzipped diagnostics dir>  -> what each store source would KEEP (and what it cuts)."""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rip_radar import paths  # noqa: E402

tmp = Path(tempfile.mkdtemp())
paths.DATA_DIR, paths.WEBVIEW_DIR = tmp / "d", tmp / "w"
paths.SETTINGS_FILE, paths.STATE_FILE = tmp / "s.json", tmp / "st.json"
paths.ICS_FILE, paths.TOPPS_CSV = tmp / "a.ics", tmp / "t.csv"
from rip_radar.engine import Engine  # noqa: E402
from rip_radar.parsing import RETAIL_STORES, blocked_by, parse_retail_tiles  # noqa: E402


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(folder, show_cut=False):
    e = Engine()
    pages = Path(folder) / "pages"
    for t in e.targets():
        if t["type"] != "retail_search":
            continue
        f = pages / (slug(t["name"]) + ".html")
        if not f.exists():
            continue
        html = f.read_text(encoding="utf-8", errors="replace")
        tiles = parse_retail_tiles(html, t["url"], t["store"], t.get("live_if_price", False))
        keep = e._keep_filter(t)
        kept = [x for x in tiles if keep(f"{x['name']} {x['url']}")]
        print(f"\n=== {t['name']}: {len(tiles)} tiles -> {len(kept)} kept")
        for x in kept:
            print(f"  + {x['name'][:100]} | {x['price']} | {x['status']}")
        if show_cut:
            for x in tiles:
                if x not in kept:
                    print(f"  - {x['name'][:100]}")


if __name__ == "__main__":
    main(sys.argv[1], "--cut" in sys.argv)
