import requests
import time
import csv
import os
import re
from datetime import datetime, timezone

# ---------------- CONFIG ----------------
GAP_ALERT_THRESHOLD = float(os.environ.get("GAP_ALERT_THRESHOLD", "0.03"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
LOG_FILE = "sharpscan_log.csv"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gamma-api.polymarket.com"

STOPWORDS = {
    "the", "vs", "at", "in", "on", "to", "win", "will", "game",
    "match", "series", "championship", "final", "finals", "game1",
    "of", "a", "for", "2025", "2026"
}

# ---------------- HELPERS ----------------

def log_row(row):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["timestamp", "kalshi_ticker", "polymarket_slug",
                              "kalshi_price", "polymarket_price", "gap"])
        writer.writerow(row)


def send_alert(msg):
    print(f"[ALERT] {msg}", flush=True)
    if NTFY_TOPIC:
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), timeout=10)
        except Exception as e:
            print(f"ntfy push failed: {e}", flush=True)


def normalize_title(title):
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    tokens = [t for t in title.split() if t not in STOPWORDS and len(t) > 2]
    return set(tokens)


# ---------------- FETCHERS ----------------

def fetch_kalshi_markets():
    """Fetch all open Kalshi markets (sports category)."""
    markets = []
    cursor = None
    for _ in range(20):  # safety cap on pagination
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Kalshi fetch error: {e}", flush=True)
            break
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def fetch_polymarket_markets():
    """Fetch all active Polymarket markets."""
    markets = []
    offset = 0
    limit = 200
    for _ in range(20):
        params = {"active": "true", "closed": "false", "limit": limit, "offset": offset}
        try:
            r = requests.get(f"{POLY_BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"Polymarket fetch error: {e}", flush=True)
            break
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return markets


def kalshi_yes_price(market):
    """Return implied YES probability 0-1 from Kalshi market dict."""
    try:
        bid = market.get("yes_bid")
        ask = market.get("yes_ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2 / 100.0
        last = market.get("last_price")
        if last:
            return last / 100.0
    except Exception:
        pass
    return None


def polymarket_yes_price(market):
    """Return implied YES probability 0-1 from Polymarket market dict."""
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            import json
            prices = json.loads(prices)
        if prices and len(prices) > 0:
            return float(prices[0])
    except Exception:
        pass
    return None


# ---------------- MATCHING ----------------

def match_markets(kalshi_markets, poly_markets):
    """Fuzzy-match Kalshi and Polymarket markets by title token overlap."""
    poly_indexed = []
    for pm in poly_markets:
        title = pm.get("question") or pm.get("title") or ""
        tokens = normalize_title(title)
        if tokens:
            poly_indexed.append((tokens, pm))

    pairs = []
    for km in kalshi_markets:
        k_title = km.get("title") or km.get("subtitle") or ""
        k_tokens = normalize_title(k_title)
        if not k_tokens:
            continue
        best_match = None
        best_score = 0
        for tokens, pm in poly_indexed:
            overlap = len(k_tokens & tokens)
            union = len(k_tokens | tokens)
            score = overlap / union if union else 0
            if score > best_score:
                best_score = score
                best_match = pm
        if best_match and best_score >= 0.5:  # require strong title overlap
            pairs.append((km, best_match, best_score))
    return pairs


# ---------------- MAIN LOOP ----------------

def run_scan():
    print(f"Fetching markets @ {datetime.now(timezone.utc).isoformat()}", flush=True)
    kalshi_markets = fetch_kalshi_markets()
    poly_markets = fetch_polymarket_markets()
    print(f"Kalshi open markets: {len(kalshi_markets)} | Polymarket active markets: {len(poly_markets)}", flush=True)

    pairs = match_markets(kalshi_markets, poly_markets)
    print(f"Matched {len(pairs)} cross-venue pairs this cycle", flush=True)

    for km, pm, score in pairs:
        k_price = kalshi_yes_price(km)
        p_price = polymarket_yes_price(pm)
        if k_price is None or p_price is None:
            continue

        gap = abs(k_price - p_price)
        if gap >= GAP_ALERT_THRESHOLD:
            k_ticker = km.get("ticker", "?")
            p_slug = pm.get("slug", "?")
            msg = (f"GAP {gap:.1%} | {k_ticker} (Kalshi {k_price:.1%}) "
                   f"vs {p_slug} (Poly {p_price:.1%}) | match={score:.2f}")
            send_alert(msg)
            log_row([datetime.now(timezone.utc).isoformat(), k_ticker, p_slug,
                      f"{k_price:.4f}", f"{p_price:.4f}", f"{gap:.4f}"])


if __name__ == "__main__":
    print("SharpScan always-on scanner starting (auto-discovery mode)...", flush=True)
    if NTFY_TOPIC:
        send_alert("SharpScan scanner online — auto-discovery mode active.")
    else:
        print("WARNING: NTFY_TOPIC not set, phone alerts disabled.", flush=True)

    while True:
        try:
            run_scan()
        except Exception as e:
            print(f"Scan cycle error: {e}", flush=True)
        time.sleep(POLL_SECONDS)
