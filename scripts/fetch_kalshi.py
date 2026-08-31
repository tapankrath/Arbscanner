"""
Fetches open Kalshi events (each with its nested markets) server-side, where
CORS doesn't apply, and writes a flattened list to kalshi-markets.json in the
repo root. Run by the GitHub Action on a schedule so the static site can read
this file instead of calling Kalshi directly from the browser.

Uses /events instead of /markets: the raw /markets listing is currently
dominated by auto-generated "cross-category" combo/parlay markets (tickers
starting KXMVE...), which drowned out plain single-event markets. Real,
named events shouldn't include that synthetic noise.
"""
import json
import urllib.request
from datetime import datetime, timezone

BASE = "https://external-api.kalshi.com/trade-api/v2/events"
PAGE_LIMIT = 200
MAX_PAGES = 25  # 25 x 200 = up to 5000 events

def fetch_page(cursor=None):
    url = f"{BASE}?status=open&with_nested_markets=true&limit={PAGE_LIMIT}"
    if cursor:
        url += f"&cursor={cursor}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; spread-arb-scanner/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        body = resp.read().decode()
        data = json.loads(body)
        if not data.get("events"):
            print(f"DEBUG: HTTP {status}, body length {len(body)}")
            print(f"DEBUG: first 500 chars of response: {body[:500]}")
        return data

def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def main():
    all_events = []
    cursor = None
    for _ in range(MAX_PAGES):
        data = fetch_page(cursor)
        events = data.get("events", [])
        all_events.extend(events)
        cursor = data.get("cursor")
        if not cursor or not events:
            break

    slim = []
    combo_skipped = 0
    for ev in all_events:
        ev_title = ev.get("title", "")
        for m in ev.get("markets", []):
            ticker = m.get("ticker") or ""
            if ticker.startswith("KXMVE") or m.get("strike_type") == "custom":
                combo_skipped += 1
                continue
            yes_ask = to_float(m.get("yes_ask_dollars"))
            no_ask = to_float(m.get("no_ask_dollars"))
            if yes_ask is None or no_ask is None:
                continue
            subtitle = m.get("subtitle") or m.get("yes_sub_title") or ""
            slim.append({
                "ticker": ticker,
                "title": (ev_title + " — " + subtitle) if subtitle else (m.get("title") or ev_title),
                "subtitle": subtitle,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "close_time": m.get("close_time"),
            })

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(slim),
        "markets": slim,
    }

    with open("kalshi-markets.json", "w") as f:
        json.dump(out, f)

    print(f"Fetched {len(all_events)} events, {combo_skipped} combo markets skipped, {len(slim)} markets kept")

if __name__ == "__main__":
    main()

