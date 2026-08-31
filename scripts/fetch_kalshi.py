"""
Fetches open Kalshi markets server-side (where CORS doesn't apply) and writes
them to kalshi-markets.json in the repo root. Run by the GitHub Action on a
schedule so the static site can read this file instead of calling Kalshi
directly from the browser.
"""
import json
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"
PAGE_LIMIT = 1000
MAX_PAGES = 10  # 10 x 1000 = up to 10,000 open markets

def fetch_page(cursor=None):
    url = f"{BASE}?status=open&limit={PAGE_LIMIT}"
    if cursor:
        url += f"&cursor={cursor}"
    req = urllib.request.Request(url, headers={"User-Agent": "spread-arb-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

def main():
    all_markets = []
    cursor = None
    for _ in range(MAX_PAGES):
        data = fetch_page(cursor)
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor")
        if not cursor or not markets:
            break

    # Kalshi auto-generates huge numbers of "multivariate cross-category" parlay
    # markets (tickers starting KXMVE...) that flood the default listing and
    # crowd out plain single-event markets. Drop them.
    real_markets = [
        m for m in all_markets
        if not (m.get("ticker") or "").startswith("KXMVE")
        and m.get("strike_type") != "custom"
    ]

    # Keep only the fields the frontend actually needs, to keep the file small.
    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    slim = [
        {
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "subtitle": m.get("subtitle") or m.get("yes_sub_title") or "",
            "yes_ask": to_float(m.get("yes_ask_dollars")),
            "no_ask": to_float(m.get("no_ask_dollars")),
            "close_time": m.get("close_time"),
        }
        for m in real_markets
    ]
    slim = [m for m in slim if m["yes_ask"] is not None and m["no_ask"] is not None]

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(slim),
        "markets": slim,
    }

    with open("kalshi-markets.json", "w") as f:
        json.dump(out, f)

    print(f"Fetched {len(all_markets)} total, {len(real_markets)} after dropping combos, {len(slim)} with valid prices")

if __name__ == "__main__":
    main()

