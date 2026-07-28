import requests
import time
import csv
import os
import re
import json
from datetime import datetime, timezone
from itertools import combinations

# ---------------- CONFIG ----------------
GAP_ALERT_THRESHOLD = float(os.environ.get("GAP_ALERT_THRESHOLD", "0.03"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.25"))
DEBUG_MATCHING = os.environ.get("DEBUG_MATCHING", "true").lower() == "true"
LOG_FILE = "sharpscan_log.csv"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gamma-api.polymarket.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")  # required for FanDuel/sportsbook data

# Sports pulled from The Odds API. Trim this list if you want fewer
# requests (free tier is 500/month, and each sport = 1 request/poll).
ODDS_API_SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "mma_mixed_martial_arts",
]

STOPWORDS = {
    "the", "vs", "at", "in", "on", "to", "win", "will", "game",
    "match", "series", "championship", "final", "finals", "game1",
    "of", "a", "for", "2025", "2026",
}

POLITICAL_KEYWORDS = (
    "president", "election", "senate", "congress", "governor",
    "fed ", "federal reserve", "impeach", "supreme court",
)

SPORTS_TICKER_PREFIXES = (
    "KXMLBGAME", "KXNBAGAME", "KXNFLGAME", "KXNHLGAME",
    "KXNCAAFGAME", "KXNCAABGAME", "KXSOCCER", "KXUFC",
    "KXATP", "KXWTA", "KXGOLF",
)

# ---------------- HELPERS ----------------

def log_row(row):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["timestamp", "venue_a", "id_a", "price_a",
                              "venue_b", "id_b", "price_b", "gap"])
        writer.writerow(row)


def send_alert(msg):
    print(f"[ALERT] {msg}", flush=True)
    if NTFY_TOPIC:
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), timeout=10)
        except Exception as e:
            print(f"ntfy push failed: {e}", flush=True)


def normalize_title(title):
    title = (title or "").lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    tokens = [t for t in title.split() if t not in STOPWORDS and len(t) > 2]
    return set(tokens)


def american_to_prob(odds):
    """Convert American odds (e.g. +150, -200) to implied probability 0-1."""
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return -odds / (-odds + 100.0)


# ---------------- UNIFIED MARKET RECORD ----------------
# Every venue gets normalized into: {"venue", "id", "title", "price"}
# price is always an implied probability (0-1) for the named outcome.

def is_kalshi_sports_market(km):
    ticker = km.get("ticker", "") or km.get("event_ticker", "")
    return any(ticker.startswith(p) for p in SPORTS_TICKER_PREFIXES)


def kalshi_title(km):
    for field in ("title", "subtitle", "yes_sub_title", "ticker"):
        val = km.get(field)
        if val:
            return val
    return ""


def kalshi_yes_price(market):
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


def fetch_kalshi():
    markets = []
    cursor = None
    for _ in range(20):
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

    sports_only = [m for m in markets if is_kalshi_sports_market(m)]
    print(f"Kalshi: {len(markets)} total open -> {len(sports_only)} sports game markets", flush=True)

    records = []
    for m in sports_only:
        price = kalshi_yes_price(m)
        if price is None:
            continue
        records.append({
            "venue": "kalshi",
            "id": m.get("ticker", "?"),
            "title": kalshi_title(m),
            "price": price,
        })
    return records


def poly_title(pm):
    for field in ("question", "title", "slug"):
        val = pm.get(field)
        if val:
            return val
    return ""


def is_poly_sports_market(pm):
    text = (poly_title(pm) or "").lower()
    if any(kw in text for kw in POLITICAL_KEYWORDS):
        return False
    tags = pm.get("tags") or []
    tag_text = " ".join(str(t) for t in tags).lower()
    if "sports" in tag_text or "sport" in tag_text:
        return True
    return "tags" not in pm


def polymarket_yes_price(market):
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if prices and len(prices) > 0:
            return float(prices[0])
    except Exception:
        pass
    return None


def fetch_polymarket():
    markets = []
    offset = 0
    limit = 200
    for _ in range(30):
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

    sports_only = [m for m in markets if is_poly_sports_market(m)]
    print(f"Polymarket: {len(markets)} total active -> {len(sports_only)} sports candidates", flush=True)

    records = []
    for m in sports_only:
        price = polymarket_yes_price(m)
        if price is None:
            continue
        records.append({
            "venue": "polymarket",
            "id": m.get("slug", "?"),
            "title": poly_title(m),
            "price": price,
        })
    return records


def fetch_fanduel():
    """Fetch FanDuel h2h (moneyline) odds via The Odds API."""
    if not ODDS_API_KEY:
        print("ODDS_API_KEY not set -- skipping FanDuel/sportsbook fetch.", flush=True)
        return []

    records = []
    for sport in ODDS_API_SPORTS:
        try:
            r = requests.get(
                f"{ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "us",
                    "markets": "h2h",
                    "bookmakers": "fanduel",
                    "oddsFormat": "american",
                },
                timeout=15,
            )
            r.raise_for_status()
            games = r.json()
        except Exception as e:
            print(f"Odds API fetch error ({sport}): {e}", flush=True)
            continue

        for g in games:
            home = g.get("home_team", "")
            away = g.get("away_team", "")
            game_id = g.get("id", "?")
            for bm in g.get("bookmakers", []):
                if bm.get("key") != "fanduel":
                    continue
                for mk in bm.get("markets", []):
                    if mk.get("key") != "h2h":
                        continue
                    for outcome in mk.get("outcomes", []):
                        team = outcome.get("name", "")
                        prob = american_to_prob(outcome.get("price"))
                        if prob is None:
                            continue
                        records.append({
                            "venue": "fanduel",
                            "id": f"{game_id}:{team}",
                            "title": f"{away} at {home} - {team} wins",
                            "price": prob,
                        })
    print(f"FanDuel: {len(records)} outcome prices fetched across {len(ODDS_API_SPORTS)} sports", flush=True)
    return records


# ---------------- MATCHING ----------------

def match_and_compare(all_records):
    """Group records across venues by title overlap, compare every pair
    within a matched group, and return alert-worthy gaps."""
    indexed = []
    for rec in all_records:
        tokens = normalize_title(rec["title"])
        if tokens:
            indexed.append((tokens, rec))

    n = len(indexed)
    visited = [False] * n
    groups = []

    for i in range(n):
        if visited[i]:
            continue
        group = [indexed[i][1]]
        visited[i] = True
        for j in range(i + 1, n):
            if visited[j]:
                continue
            tokens_i = indexed[i][0]
            tokens_j = indexed[j][0]
            overlap = len(tokens_i & tokens_j)
            union = len(tokens_i | tokens_j)
            score = overlap / union if union else 0
            if score >= MATCH_THRESHOLD:
                group.append(indexed[j][1])
                visited[j] = True
        if len(group) > 1:
            groups.append(group)

    alerts = []
    for group in groups:
        for a, b in combinations(group, 2):
            if a["venue"] == b["venue"]:
                continue  # only cross-venue gaps matter
            gap = abs(a["price"] - b["price"])
            if gap >= GAP_ALERT_THRESHOLD:
                alerts.append((a, b, gap))

    if DEBUG_MATCHING:
        print(f"Formed {len(groups)} cross-venue title groups this cycle", flush=True)

    return alerts


# ---------------- MAIN LOOP ----------------

def run_scan():
    print(f"Fetching markets @ {datetime.now(timezone.utc).isoformat()}", flush=True)
    all_records = []
    all_records.extend(fetch_kalshi())
    all_records.extend(fetch_polymarket())
    all_records.extend(fetch_fanduel())

    print(f"Total sports records across all venues: {len(all_records)}", flush=True)

    alerts = match_and_compare(all_records)
    print(f"Found {len(alerts)} cross-venue gaps >= {GAP_ALERT_THRESHOLD:.1%}", flush=True)

    for a, b, gap in alerts:
        msg = (f"GAP {gap:.1%} | {a['venue']}:{a['id']} ({a['price']:.1%}) "
               f"vs {b['venue']}:{b['id']} ({b['price']:.1%}) | \"{a['title'][:60]}\"")
        send_alert(msg)
        log_row([
            datetime.now(timezone.utc).isoformat(),
            a["venue"], a["id"], f"{a['price']:.4f}",
            b["venue"], b["id"], f"{b['price']:.4f}",
            f"{gap:.4f}",
        ])


if __name__ == "__main__":
    print("SharpScan always-on scanner starting (multi-venue mode: Kalshi, Polymarket, FanDuel)...", flush=True)
    if NTFY_TOPIC:
        send_alert("SharpScan scanner online -- multi-venue mode active.")
    else:
        print("WARNING: NTFY_TOPIC not set, phone alerts disabled.", flush=True)
    if not ODDS_API_KEY:
        print("WARNING: ODDS_API_KEY not set, FanDuel data will be skipped entirely.", flush=True)

    while True:
        try:
            run_scan()
        except Exception as e:
            print(f"Scan cycle error: {e}", flush=True)
        time.sleep(POLL_SECONDS)
