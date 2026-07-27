"""
SharpScan Always-On Scanner
Runs continuously on a VM. Polls slowly outside live windows, fast during them.
Pushes phone alerts via ntfy.sh when a real gap is detected.
"""

import requests
import csv
import os
import time
from datetime import datetime, timezone

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
LOG_FILE = "sharpscan_log.csv"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GAP_ALERT_THRESHOLD = float(os.environ.get("GAP_ALERT_THRESHOLD", "0.03"))

SLOW_POLL_SECONDS = 300   # 5 min, outside live windows
FAST_POLL_SECONDS = 15    # during live windows

# Fill in real tickers/slugs. Times in UTC. Add a buffer on either side.
WATCHED_PAIRS = [
    {
        "label": "Example Game - replace me",
        "kalshi_ticker": "KXMLBGAME-PLACEHOLDER",
        "polymarket_slug": "mlb-placeholder-2026-01-01",
        "live_start": "2026-01-01T00:00:00+00:00",
        "live_end":   "2026-01-01T04:00:00+00:00",
    },
]


def in_live_window(pair, now):
    start = datetime.fromisoformat(pair["live_start"])
    end = datetime.fromisoformat(pair["live_end"])
    return start <= now <= end


def notify_phone(message):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), timeout=5)
    except Exception as e:
        print(f"Notify failed: {e}")


def get_kalshi_price(ticker):
    resp = requests.get(f"{KALSHI_BASE}/markets/{ticker}", timeout=10)
    resp.raise_for_status()
    m = resp.json()["market"]
    return {
        "ticker": m["ticker"],
        "yes_bid": float(m["yes_bid_dollars"]),
        "yes_ask": float(m["yes_ask_dollars"]),
        "mid": (float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2,
    }


def get_polymarket_price(slug):
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={"slug": slug}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None
    m = data[0]
    price = float(m["outcomePrices"][0])
    return {"question": m["question"], "mid": price}


def log_row(pair_label, kalshi_data, poly_data, spread):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp_utc", "pair", "kalshi_mid", "polymarket_mid", "spread"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            pair_label,
            kalshi_data["mid"],
            poly_data["mid"],
            round(spread, 4),
        ])


def scan_once():
    now = datetime.now(timezone.utc)
    any_live = False

    for pair in WATCHED_PAIRS:
        live = in_live_window(pair, now)
        any_live = any_live or live
        try:
            k = get_kalshi_price(pair["kalshi_ticker"])
            p = get_polymarket_price(pair["polymarket_slug"])
            if not k or not p:
                print(f"[{pair['label']}] Missing data")
                continue

            spread = abs(k["mid"] - p["mid"])
            log_row(pair["label"], k, p, spread)

            tag = " [LIVE]" if live else ""
            flag = " <-- GAP" if spread >= GAP_ALERT_THRESHOLD else ""
            print(f"{tag}[{pair['label']}] Kalshi {k['mid']:.3f} | "
                  f"Polymarket {p['mid']:.3f} | Spread {spread:.3f}{flag}")

            if spread >= GAP_ALERT_THRESHOLD:
                notify_phone(
                    f"SharpScan gap: {pair['label']}\n"
                    f"Kalshi {k['mid']:.2f} vs Polymarket {p['mid']:.2f} "
                    f"(spread {spread:.3f}){' LIVE' if live else ''}"
                )
        except Exception as e:
            print(f"[{pair['label']}] Error: {e}")

    return any_live


def main():
    print("SharpScan always-on scanner starting...")
    notify_phone("SharpScan scanner started and running.")
    while True:
        live = scan_once()
        time.sleep(FAST_POLL_SECONDS if live else SLOW_POLL_SECONDS)


if __name__ == "__main__":
    main()
