"""
Runs on a schedule (see .github/workflows/watch-and-alert.yml). Builds a
DYNAMIC watchlist — no manual entry — from live Kalshi x Polymarket matches
closing within WATCH_DAYS days, using the same matching logic as the web app
(title-token similarity + numeric-strike compatibility). Tracks margin over
time in watchlist-state.json and pushes an ntfy.sh notification when a
trigger fires. Entries that close or stop qualifying are moved to "archive"
rather than deleted, so there's a record of what was tracked.

Reads the Kalshi snapshot already maintained by fetch_kalshi.py rather than
re-fetching it (that runs on its own 20-minute schedule). Fetches Polymarket
live every run (cheap — a couple of GET requests).
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

WATCH_DAYS = 7          # dynamic watchlist window
SIM_THRESHOLD = 0.35    # same title-similarity floor as the web app
TARGET_MARGIN = 0.03    # alert once when margin first crosses above this
CAP_MARGIN = 0.20       # alert once if margin exceeds this — likely a mismatch, verify
MOVE_THRESHOLD = 0.02   # alert once if margin moves at least this many points from first-seen
K_FEE_PCT = 0.015
P_FEE_PCT = 0.005

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = "watchlist-state.json"
KALSHI_SNAPSHOT_FILE = "kalshi-markets.json"

STOPWORDS = {
    'will', 'the', 'a', 'an', 'is', 'are', 'be', 'by', 'of', 'in', 'on', 'after', 'before',
    'win', 'to', 'for', 'this', 'that', 'or', 'and', 'with', 'meeting', 'market', 'above',
    'below', 'at', 'end', 'from', 'than', 'close', 'have', 'has', 'not', 'no', 'yes', 'get',
}


def tokenize(text):
    text = re.sub(r'[^a-z0-9%.\s]', ' ', (text or '').lower())
    return [t for t in text.split() if len(t) > 1 and t not in STOPWORDS]


def material_numbers(text):
    nums = []
    for s in re.findall(r'\d[\d,]*\.?\d*', text or ''):
        try:
            n = float(s.replace(',', ''))
        except ValueError:
            continue
        if 1900 <= n <= 2100 and n == int(n):
            continue  # bare year, not a strike/threshold
        nums.append(n)
    return nums


def numbers_compatible(a_text, b_text):
    a, b = material_numbers(a_text), material_numbers(b_text)
    if not a or not b:
        return True
    return any(abs(x - y) <= max(1, x * 0.02) for x in a for y in b)


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; spread-watcher/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_kalshi_snapshot():
    with open(KALSHI_SNAPSHOT_FILE) as f:
        return json.load(f).get("markets", [])


def load_polymarket():
    vol_url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=600&order=volume24hr&ascending=false"
    soon_url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=600&order=endDate&ascending=true"
    vol, soon = fetch_json(vol_url), fetch_json(soon_url)

    seen, merged = set(), []
    for m in (soon or []) + (vol or []):
        mid = m.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        merged.append(m)

    out = []
    for m in merged:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
        except Exception:
            outcomes = []
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
        except Exception:
            prices = []
        yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), -1)
        no_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "no"), -1)
        yes_ask = float(prices[yes_idx]) if 0 <= yes_idx < len(prices) else None
        no_ask = float(prices[no_idx]) if 0 <= no_idx < len(prices) else None
        if yes_ask is None or no_ask is None:
            continue
        out.append({
            "id": m.get("id"), "title": m.get("question"), "slug": m.get("slug"),
            "endDate": m.get("endDate"), "yesAsk": yes_ask, "noAsk": no_ask,
        })
    return out


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def close_time(k_close, p_close):
    kd, pd = parse_date(k_close), parse_date(p_close)
    if kd and pd:
        return min(kd, pd)
    return kd or pd


def send_ntfy(title, message, tags=None, priority=None):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC secret not set — skipping push:", title)
        return
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    if tags:
        req.add_header("Tags", tags)
    if priority:
        req.add_header("Priority", priority)
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("ntfy push failed:", e)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"active": {}, "archive": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def main():
    now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() + WATCH_DAYS * 86400

    kalshi_markets = load_kalshi_snapshot()
    poly_markets = load_polymarket()

    kalshi_tokens = [set(tokenize((m.get("title") or "") + " " + (m.get("subtitle") or ""))) for m in kalshi_markets]
    index = {}
    for i, toks in enumerate(kalshi_tokens):
        for t in toks:
            index.setdefault(t, []).append(i)

    state = load_state()
    active, archive = state.get("active", {}), state.get("archive", {})
    seen_this_run = set()

    for pm in poly_markets:
        p_toks = set(tokenize(pm["title"]))
        if not p_toks:
            continue
        candidate_idx = set()
        for t in p_toks:
            candidate_idx.update(index.get(t, []))

        for i in candidate_idx:
            k_toks = kalshi_tokens[i]
            if not k_toks:
                continue
            inter = len(p_toks & k_toks)
            union = len(p_toks) + len(k_toks) - inter
            sim = inter / union if union else 0
            if sim < SIM_THRESHOLD:
                continue

            km = kalshi_markets[i]
            if not numbers_compatible(pm["title"], (km.get("title") or "") + " " + (km.get("subtitle") or "")):
                continue

            ct = close_time(km.get("close_time"), pm.get("endDate"))
            if not ct or ct.timestamp() > cutoff_ts or ct.timestamp() < now.timestamp():
                continue  # outside the dynamic window, or already past close

            best_margin, best_dir = None, None
            for label, a, b in [
                ("Kalshi No + Polymarket Yes", km.get("no_ask"), pm.get("yesAsk")),
                ("Kalshi Yes + Polymarket No", km.get("yes_ask"), pm.get("noAsk")),
            ]:
                if a is None or b is None:
                    continue
                margin = 1 - (a + b) - (a * K_FEE_PCT + b * P_FEE_PCT)
                if best_margin is None or margin > best_margin:
                    best_margin, best_dir = margin, label
            if best_margin is None:
                continue

            key = f'{km.get("ticker")}|{pm.get("id")}'
            seen_this_run.add(key)

            entry = active.get(key)
            if entry is None:
                entry = {
                    "kalshiTicker": km.get("ticker"), "polyId": pm.get("id"),
                    "title": f'{km.get("title")} ↔ {pm.get("title")}',
                    "firstSeenAt": now.isoformat(), "firstSeenMargin": best_margin,
                    "closeTime": ct.isoformat(),
                    "alerted": {"target": False, "cap": False, "move": False},
                }
                active[key] = entry

            entry["lastCheckedAt"] = now.isoformat()
            entry["lastMargin"] = best_margin
            entry["direction"] = best_dir

            if not entry["alerted"]["target"] and best_margin >= TARGET_MARGIN:
                send_ntfy("Target margin reached", f'{entry["title"]}\n{best_dir}: {best_margin*100:.2f}% margin',
                           tags="chart_with_upwards_trend")
                entry["alerted"]["target"] = True

            if not entry["alerted"]["cap"] and best_margin >= CAP_MARGIN:
                send_ntfy("⚠ Margin above cap — verify before trusting",
                           f'{entry["title"]}\n{best_dir}: {best_margin*100:.2f}% — unusually large, likely a mismatch. Check resolution terms before trading.',
                           tags="warning", priority="high")
                entry["alerted"]["cap"] = True

            move = abs(best_margin - entry["firstSeenMargin"])
            if not entry["alerted"]["move"] and move >= MOVE_THRESHOLD:
                direction_word = "up" if best_margin > entry["firstSeenMargin"] else "down"
                send_ntfy("Margin moved", f'{entry["title"]}\nMoved {move*100:.2f} pts {direction_word} since first seen ({entry["firstSeenMargin"]*100:.2f}% → {best_margin*100:.2f}%)',
                           tags="chart_with_upwards_trend" if direction_word == "up" else "chart_with_downwards_trend")
                entry["alerted"]["move"] = True

    for key in list(active.keys()):
        if key not in seen_this_run:
            entry = active.pop(key)
            ct = parse_date(entry.get("closeTime"))
            entry["archiveReason"] = "closed" if (ct and ct.timestamp() < now.timestamp()) else "no_longer_matched"
            entry["archivedAt"] = now.isoformat()
            archive[key] = entry

    state["active"], state["archive"] = active, archive
    save_state(state)
    print(f"Active: {len(active)}, archived total: {len(archive)}, checked this run: {len(seen_this_run)}")


if __name__ == "__main__":
    main()
